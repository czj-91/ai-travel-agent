"""HTTP 服务层：把 agent 暴露成稳定接口，替掉 langgraph dev 的临时隧道域名。

启动：
    uvicorn service:app --host 127.0.0.1 --port 8000

接口：
    GET  /health   存活与配置检查
    POST /plan     {"request": "杭州 3天 2人 喜欢人文", "session_id": 可选}
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from travel.config import MissingEnvError, load_settings
from travel.graph import build_agent, plan_trip

app = FastAPI(title="AI Travel Agent", version="0.3.0")
_graph = None


def get_graph():
    """延迟构建：导入 app 时不要求密钥齐全，便于 /health 单独检查。"""
    global _graph
    if _graph is None:
        _graph, _ = build_agent()
    return _graph


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


@app.post("/plan", response_model=PlanResponse)
def plan(payload: PlanRequest):
    try:
        report, stats, thread = plan_trip(get_graph(), payload.request, payload.session_id)
    except MissingEnvError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {str(exc)[:200]}") from exc

    if report is None:
        raise HTTPException(status_code=502, detail="模型未按 schema 返回，请检查 response_format 与工具输出")

    return PlanResponse(
        session_id=thread,
        elapsed_ms=stats.elapsed_ms,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        plan=report.model_dump(),
    )