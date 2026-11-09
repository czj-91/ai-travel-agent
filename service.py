"""HTTP 服务层：把 agent 暴露成稳定接口，替掉 langgraph dev 的临时隧道域名。

启动：
    uvicorn service:app --host 127.0.0.1 --port 8000

接口：
    GET  /health   存活与配置检查（不构造 agent，缺密钥也能返回可读结论）
    POST /plan     {"request": "...", "session_id": 可选} -> TripPlan JSON + 统计

安全：
    默认只监听 127.0.0.1。若要对外提供，请在 .env 设置 SERVICE_TOKEN，
    客户端必须带请求头 X-Service-Token，否则 401。
"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from travel.config import MissingEnvError, load_settings
from travel.graph import build_agent, plan_trip

app = FastAPI(title="AI Travel Agent", version="0.4.0")
_graph = None
_settings = None


def get_agent():
    """延迟构建：导入 app 时不要求密钥齐全，/health 才能独立自检。"""
    global _graph, _settings
    if _graph is None:
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


class PlanResponse(BaseModel):
    session_id: str
    elapsed_ms: int
    input_tokens: int
    output_tokens: int
    plan: dict


@app.get("/health")
def health():
    try:
        settings = load_settings()
    except MissingEnvError as exc:
        return {"status": "config_missing", "detail": str(exc).splitlines()[0]}
    return {"status": "ok", "model": settings.travel_model, "project": settings.langsmith_project}


@app.post("/plan", response_model=PlanResponse, dependencies=[Depends(require_token)])
def plan(payload: PlanRequest):
    try:
        graph, settings = get_agent()
    except MissingEnvError as exc:
        raise HTTPException(status_code=503, detail=str(exc).splitlines()[0]) from exc

    try:
        report, stats, thread = plan_trip(graph, settings, payload.request, payload.session_id)
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