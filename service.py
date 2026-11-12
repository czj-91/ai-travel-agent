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

import json
import logging
import os
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse  # 0.162 起顶层不再导出，必须从 responses 拿
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from travel import export as exporter
from travel.config import MissingEnvError, load_settings
from travel.graph import build_agent, cost_yuan, plan_trip, provenance_from_messages, stream_plan
from travel.multimodal import ImageInputError, check_image_model, parse_data_image
from travel.schemas import TripPlan
from travel.security import RateLimiter, origins_from_env

_logger = logging.getLogger("ai_travel.service")

app = FastAPI(title="AI Travel Agent", version="0.5.0")

# 跨源默认只放行本机页面；allow_credentials 关掉：鉴权走 X-Service-Token，不靠 cookie
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins_from_env(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Service-Token"],
)

# 一次 /plan 要烧上万 token，没有限流就等于把额度交给网页刷新按钮
_limiter = RateLimiter(
    limit=int(os.getenv("RATE_LIMIT_PER_MIN") or 10),
    window_seconds=int(os.getenv("RATE_WINDOW_SECONDS") or 60),
)
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


def reset_rate_limiter() -> None:
    """清空限流窗口：测试必须能隔离状态，运维需要手动解闸。"""
    _limiter.reset()


def _client_key(request: Request) -> str:
    """反代后面优先取 X-Forwarded-For 的第一个地址，否则用直连 IP。"""
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def limit_requests(request: Request):
    allowed, retry_after = _limiter.check(_client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"请求过快，请 {retry_after} 秒后重试（每分钟上限 {_limiter.limit} 次）",
            headers={"Retry-After": str(int(retry_after) or 1)},
        )
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
    cost_yuan: float | None = None
    provenance: dict | None = None
    plan: dict


class ExportRequest(BaseModel):
    plan: dict = Field(description="/plan 返回的 plan 原文（TripPlan JSON）")
    start_date: str | None = Field(default=None, description="YYYY-MM-DD 出发日期；不填按明天并标注为推测")
    formats: list[str] = Field(
        default_factory=lambda: ["markdown", "ics"], description="markdown / ics / txt"
    )


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


@app.post(
    "/export", response_model=ExportResponse, dependencies=[Depends(require_token), Depends(limit_requests)]
)
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


@app.post(
    "/plan", response_model=PlanResponse, dependencies=[Depends(require_token), Depends(limit_requests)]
)
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
        report, stats, thread, messages = plan_trip(
            graph,
            settings,
            payload.request,
            payload.session_id,
            image_block=image_block,
            return_messages=True,
        )
        result_provenance = provenance_from_messages(messages)
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
        cost_yuan=cost_yuan(stats, settings),
        provenance=result_provenance,
        plan=report.model_dump(),
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/plan/stream", dependencies=[Depends(require_token), Depends(limit_requests)])
def plan_stream(payload: PlanRequest):
    """流式规划：边跑边推 token 与工具事件，最后推完整 plan。

    同步 /plan 要让人对着白屏等 27 秒，Agent 产品的体验关键就在这条流上。
    """
    try:
        graph, settings = get_agent()
    except MissingEnvError as exc:
        raise HTTPException(status_code=503, detail=str(exc).splitlines()[0]) from exc

    image_block = None
    if payload.image:
        try:
            image_block = parse_data_image(payload.image)
        except ImageInputError as exc:
            raise HTTPException(status_code=422, detail=f"图片不可用：{exc}") from exc

    def events():
        try:
            for event in stream_plan(graph, settings, payload.request, payload.session_id, image_block):
                yield _sse(event)
        except ValueError as exc:
            yield _sse({"type": "error", "detail": str(exc)})
        except Exception as exc:
            # 对外只给可读结论，但服务端必须留下完整堆栈，否则线上失败无从排查
            _logger.exception("流式规划失败")
            yield _sse({"type": "error", "detail": f"规划失败：{type(exc).__name__}"})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
