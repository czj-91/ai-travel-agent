"""Agent 组装：模型 + 工具 + 结构化输出 + 会话记忆 + 上下文压缩。

langgraph dev / Studio 挂载的就是本模块里的 agent 对象。
"""
from __future__ import annotations

import sqlite3
import uuid

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from .config import Settings, load_settings
from .observability import RunStats, Timer, usage_from_messages
from .prompts import SYSTEM_PROMPT
from .schemas import DayPlan, Spot, TripPlan
from .tools import build_search_tool

# checkpointer 反序列化自定义类型必须显式白名单，否则新版 langgraph 会直接拒绝：
# "Deserializing unregistered type travel.schemas.TripPlan from checkpoint"
ALLOWED_MODULES = [
    ("travel.schemas", TripPlan.__name__),
    ("travel.schemas", DayPlan.__name__),
    ("travel.schemas", Spot.__name__),
]


def build_chat_model(settings: Settings):
    """模型分级：qwen 系列走百炼 OpenAI 兼容端点，其余走各自官方 SDK。"""
    if settings.travel_model.startswith("qwen"):
        return init_chat_model(
            settings.travel_model,
            model_provider="openai",
            base_url=settings.dashscope_base_url,
            api_key=settings.dashscope_api_key,
        )
    return init_chat_model(settings.travel_model)


def _build_checkpointer(settings: Settings, persist: bool):
    if not persist:
        return InMemorySaver()
    connection = sqlite3.connect(settings.db_path, check_same_thread=False)
    serde = JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODULES)
    checkpointer = SqliteSaver(connection, serde=serde)
    checkpointer.setup()
    return checkpointer


def build_agent(persist: bool = True, settings: Settings | None = None):
    """返回 (graph, settings)。persist=True 时用 SQLite 保存会话状态。"""
    settings = settings or load_settings()
    model = build_chat_model(settings)

    graph = create_agent(
        model=model,
        tools=[
            build_search_tool(
                retries=settings.search_retries,
                timeout=settings.search_timeout,
            )
        ],
        middleware=[
            SummarizationMiddleware(
                model=model,
                trigger=("messages", 20),
                keep=("messages", 5),
            )
        ],
        checkpointer=_build_checkpointer(settings, persist),
        system_prompt=SYSTEM_PROMPT,
        response_format=TripPlan,
    )
    return graph, settings


def plan_trip(graph, request: str, session_id: str | None = None):
    """跑一次规划，返回 (TripPlan, RunStats, thread_id)。同一 session_id 会延续记忆。"""
    thread = session_id or f"cli-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread}}
    with Timer() as timer:
        result = graph.invoke({"messages": [("user", request)]}, config)
    stats = usage_from_messages(result)
    stats.elapsed_ms = timer.elapsed_ms
    return result.get("structured_response"), stats, thread


agent, settings = build_agent()