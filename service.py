"""HTTP 服务层：把 agent 暴露成稳定接口，替掉 langgraph dev 的临时隧道域名。

启动：
    python -m uvicorn service:app --host 127.0.0.1 --port 8080
    # 用 python -m 而不是裸 uvicorn.exe（Windows Smart App Control 会拦入口脚本）；
    # 端口避开 8000：Hyper-V/WinNAT 常把它列进 TCP 保留区间，bind 直接失败（README 踩坑 18）。

接口：
    GET  /health   存活与配置检查（不构造 agent，缺密钥也能返回可读结论）
    POST /plan     {"request": "...", "session_id": 可选} -> TripPlan JSON + 统计
    GET  /         本地网页版：填需求出行程，再下载 Markdown / ics（浏览器直接打开根路径）
    POST /export   {"plan": <TripPlan JSON>, "start_date": 可选} -> 导出文本（不跑模型，零成本）

静态资源：/static/app.css、/static/app.js（web/ 目录，零构建、零外部 CDN）

安全：
    默认只监听 127.0.0.1。若要对外提供，请在 .env 设置 SERVICE_TOKEN，
    客户端必须带请求头 X-Service-Token，否则 401。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse  # 0.162 起顶层不再导出，必须从 responses 拿
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from travel import export as exporter
from travel.config import MissingEnvError, load_settings
from travel.graph import build_agent, plan_trip
from travel.multimodal import ImageInputError, check_image_model, parse_data_image
from travel.schemas import TripPlan

app = FastAPI(title="AI Travel Agent", version="0.4.0")
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
INDEX_PATH = WEB_DIR / "index.html"
# 网页版只用本地静态文件：不引 CDN，断网也能开（内网/离线环境是常态）
if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
_graph = None
_settings = None
_agent_lock = threading.Lock()


def get_agent():
    """延迟构建：导入 app 时不要求密钥齐全，/health 才能独立自检。"""
    global _graph, _settings
    if _graph is not None:
        return _graph, _settings
    with _agent_lock:
        if _graph is None:  # 无锁时并发首请求会各建一遍图：两份 SQLite 会话库、两份模型客户端
            _graph, _settings = build_agent()
    return _graph, _settings


def require_token(x_service_token: str | None = Header(default=None)):
    token = (os.getenv("SERVICE_TOKEN") or "").strip()
    if token and x_service_token != token:
        raise HTTPException(status_code=401, detail="缺少或错误的 X-Service-Token")
    return True


class PlanRequest(BaseModel):
    request: str = Field(min_length=2, max_length=600, description="出行需求原文")
    session_id: str | None = Field(default=None, description="同一 session_id 延续多轮")
    image: str | None = Field(
        default=None,
        max_length=12_000_000,
        description="可选，图片 data URL 或纯 base64；需 TRAVEL_MODEL 为多模态模型（qwen*）",
    )

    @field_validator("request")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """两个空格也能过 min_length=2，然后原样打进模型——这里必须挡住。"""
        text = value.strip()
        if len(text) < 2:
            raise ValueError("request 不能只有空白字符：请至少写明目的地与天数")
        return text


class PlanResponse(BaseModel):
    session_id: str
    elapsed_ms: int
    input_tokens: int
    output_tokens: int
    plan: dict


class ExportRequest(BaseModel):
    plan: dict = Field(description="/plan 返回的 plan 原文（TripPlan JSON）")
    start_date: str | None = Field(default=None, description="YYYY-MM-DD 出发日期；不填按明天并标注为推测")
    formats: list[str] = Field(default_factory=lambda: ["markdown", "ics"], description="markdown / ics / txt")


class ExportedFile(BaseModel):
    name: str
    format: str
    content: str


class ExportResponse(BaseModel):
    start_date: str
    start_date_guessed: bool
    files: list[ExportedFile]


@app.get("/health")
def health():
    try:
        settings = load_settings()
    except MissingEnvError as exc:
        return {"status": "config_missing", "detail": str(exc).splitlines()[0]}
    return {"status": "ok", "model": settings.travel_model, "project": settings.langsmith_project}


@app.get("/", include_in_schema=False)
def index():
    """本地网页版入口，只服务一个静态文件，不引入任何前端依赖。"""
    if not INDEX_PATH.is_file():
        raise HTTPException(status_code=404, detail=f"缺少网页文件：{INDEX_PATH}")
    return FileResponse(INDEX_PATH, media_type="text/html")


@app.post("/export", response_model=ExportResponse, dependencies=[Depends(require_token)])
def export(payload: ExportRequest):
    if not payload.formats:
        raise HTTPException(status_code=422, detail="formats 不能为空")
    unknown = [fmt for fmt in payload.formats if fmt not in exporter.FORMATS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知导出格式 {unknown}，可选 {list(exporter.FORMATS)}")
    try:
        report = TripPlan.model_validate(payload.plan)
        when, guessed = exporter.resolve_start_date(payload.start_date)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"plan/参数不合期望结构：{type(exc).__name__}") from exc

    return ExportResponse(
        start_date=when.isoformat(),
        start_date_guessed=guessed,
        files=[
            ExportedFile(
                name=exporter.safe_stem(report, when) + exporter.FILE_SUFFIX[fmt],
                format=fmt,
                content=exporter.render(report, fmt, when),
            )
            for fmt in payload.formats
        ],
    )


@app.post("/plan", response_model=PlanResponse, dependencies=[Depends(require_token)])
def plan(payload: PlanRequest):
    try:
        graph, settings = get_agent()
    except MissingEnvError as exc:
        raise HTTPException(status_code=503, detail=str(exc).splitlines()[0]) from exc

    image_block = None
    if payload.image:
        try:
            image_block = parse_data_image(payload.image)
        except ImageInputError as exc:
            # 只回可读结论，不把整段 base64 或本地路径写进响应
            raise HTTPException(status_code=422, detail=f"图片不可用：{exc}") from exc
        try:
            check_image_model(settings.travel_model, True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        report, stats, thread = plan_trip(graph, settings, payload.request, payload.session_id, image_block=image_block)
    except Exception as exc:
        # 不把内部异常原文透给调用方，只给类型与截断信息，完整堆栈留在服务端日志
        raise HTTPException(status_code=500, detail=f"规划失败：{type(exc).__name__}") from exc

    if report is None:
        raise HTTPException(status_code=502, detail="模型未按 schema 返回，请检查 response_format 与工具输出")

    return PlanResponse(
        session_id=thread,
        elapsed_ms=stats.elapsed_ms,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        plan=report.model_dump(),
    )
