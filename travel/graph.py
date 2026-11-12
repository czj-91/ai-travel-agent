"""Agent 组装：模型 + 工具 + 结构化输出 + 会话记忆 + 上下文压缩。

langgraph dev / Studio 挂载的就是本模块里的 agent 对象。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessageChunk as _AIMessageChunk
from langchain_core.messages import ToolMessage as _ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from .config import Settings, load_settings
from .multimodal import build_content, check_image_model
from .observability import Timer, usage_from_messages
from .prompts import get_prompt
from .rag import PoiIndex, build_poi_tool
from .schemas import DayPlan, Spot, TripPlan
from .tools import build_search_tool
from .weather import build_weather_tool

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


# build_agent() 会被评测、A/B、测试反复调用；每次都新建 sqlite 连接却没人关，
# 文件句柄只会越堆越多。按 db_path 复用同一个 saver（SqliteSaver 内部自带写锁）。
_CHECKPOINTERS: dict[str, SqliteSaver] = {}
_CHECKPOINTER_LOCK = threading.Lock()


def _build_checkpointer(settings: Settings, persist: bool):
    if not persist:
        return InMemorySaver()
    with _CHECKPOINTER_LOCK:
        saved = _CHECKPOINTERS.get(settings.db_path)
        if saved is None:
            connection = sqlite3.connect(settings.db_path, check_same_thread=False)
            serde = JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODULES)
            saved = SqliteSaver(connection, serde=serde)
            saved.setup()
            _CHECKPOINTERS[settings.db_path] = saved
        return saved


def build_agent(
    persist: bool = True,
    settings: Settings | None = None,
    prompt_version: str | None = None,
    platform_persistence: bool = False,
):
    """返回 (graph, settings)。persist=True 时用 SQLite 保存会话状态。

    platform_persistence=True 时**完全不传 checkpointer**：langgraph dev / LangSmith
    平台自己管持久化，带着自定义 checkpointer 会直接 GraphLoadError 启动失败。
    """
    settings = settings or load_settings()
    model = build_chat_model(settings)

    kwargs = {} if platform_persistence else {"checkpointer": _build_checkpointer(settings, persist)}
    graph = create_agent(
        model=model,
        tools=[
            build_poi_tool(PoiIndex()),
            build_search_tool(retries=settings.search_retries, timeout=settings.search_timeout),
            build_weather_tool(),
        ],
        middleware=[
            SummarizationMiddleware(
                model=model,
                trigger=("messages", 20),
                keep=("messages", 5),
            )
        ],
        **kwargs,
        system_prompt=get_prompt(prompt_version),
        response_format=TripPlan,
    )
    return graph, settings


def _first_json_object(text: str) -> str | None:
    """扫描出第一个花括号配平的 JSON 片段（跳过字符串内的括号与转义）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _message_count(graph, config) -> int:
    """取当前 thread 已有的消息条数，用于把统计范围限制在本轮。"""
    try:
        state = graph.get_state(config)
    except Exception:
        return 0
    return len((getattr(state, "values", None) or {}).get("messages") or [])


def parse_structured_from_text(messages) -> TripPlan | None:
    """兜底：模型有时把结果写在文本里而没有调用结构化工具。

    从最后几条消息里抓第一个 JSON 对象并按 schema 校验；解析失败返回 None，
    绝不返回半成品，避免脏数据进入下游。
    """
    import json

    for message in reversed(list(messages or [])):
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        if not isinstance(content, str):
            continue
        candidate = _first_json_object(content)
        if not candidate:
            continue
        try:
            return TripPlan.model_validate(json.loads(candidate))
        except Exception:
            continue
    return None


def plan_trip(
    graph,
    settings: Settings,
    request: str,
    session_id: str | None = None,
    image_path: str | None = None,
    image_block: dict | None = None,
):
    """跑一次规划，返回 (TripPlan, RunStats, thread_id)。同一 session_id 会延续记忆。

    带图片时必须用多模态模型（qwen-vl / qwen3.5-plus 这类），否则直接拒绝，
    不要让请求打到模型后再报一个看不懂的 400。

    结构化工具没被调用时走两级兜底：先从文本里解析 JSON，再在同一 thread 上
    显式提醒一次。A/B 实测：工具越多、提示词越长，模型越容易只回文本不调工具。
    """
    check_image_model(settings.travel_model, bool(image_path or image_block))

    content = build_content(request, image_path, image_block)
    thread = session_id or f"cli-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread}}

    before = _message_count(graph, config)
    with Timer() as timer:
        result = graph.invoke({"messages": [("user", content)]}, config)
        report = result.get("structured_response") or parse_structured_from_text(result.get("messages", []))
        if report is None:
            result = graph.invoke(
                {
                    "messages": [
                        ("user", "请按 schema 输出最终结果，必须调用 TripPlan 结构化工具，不要只回文本。")
                    ]
                },
                config,
            )
            report = result.get("structured_response") or parse_structured_from_text(
                result.get("messages", [])
            )
    # 只统计本轮新增的消息：整个 thread 历史都算会把多轮会话的成本虚报成单次成本
    stats = usage_from_messages({"messages": (result.get("messages") or [])[before:]})
    stats.elapsed_ms = timer.elapsed_ms
    return report, stats, thread


def stream_plan(graph, settings, request: str, session_id: str | None = None, image_block=None):
    """流式规划：逐段产出事件字典，最后给出结构化结果与本轮统计。

    事件类型：start / tool_call / tool_result / plan / error。

    为什么是同步生成器而不是 async：本项目 checkpointer 是 `SqliteSaver`，
    它明确不支持 async（`astream` 会抛 NotImplementedError）。用同步 `graph.stream`
    由 Starlette 放进线程池执行，代价是一个请求占一个线程槽——真要高并发得换
    AsyncSqliteSaver 或 Postgres，这条已写进 README 的取舍说明。
    """
    check_image_model(settings.travel_model, bool(image_block))
    content = build_content(request, image_block=image_block)
    thread = session_id or f"cli-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread}}
    before = _message_count(graph, config)

    with Timer() as timer:
        yield {"type": "start", "session_id": thread}
        for chunk, meta in graph.stream({"messages": [("user", content)]}, config, stream_mode="messages"):
            name = getattr(chunk, "name", None) or (
                meta.get("ls_agent_type") if isinstance(meta, dict) else None
            )
            if isinstance(chunk, _ToolMessage):
                yield {
                    "type": "tool_result",
                    "name": name,
                    "preview": str(getattr(chunk, "content", ""))[:160],
                }
            elif isinstance(chunk, _AIMessageChunk):
                for call in (
                    getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None) or []
                ):
                    call_name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
                    if call_name:
                        yield {"type": "tool_call", "name": call_name}
                text = getattr(chunk, "content", "")
                if isinstance(text, list):
                    text = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part) for part in text
                    )
                if text:
                    yield {"type": "token", "text": str(text)}

        result = graph.invoke(None, config)
        messages = (result or {}).get("messages") or []
        report = (result or {}).get("structured_response") or parse_structured_from_text(messages)
        if report is None:
            graph.invoke(
                {
                    "messages": [
                        ("user", "请按 schema 输出最终结果，必须调用 TripPlan 结构化工具，不要只回文本。")
                    ]
                },
                config,
            )
            result = graph.invoke(None, config)
            messages = (result or {}).get("messages") or []
            report = (result or {}).get("structured_response") or parse_structured_from_text(messages)

    stats = usage_from_messages({"messages": messages[before:]})
    stats.elapsed_ms = timer.elapsed_ms
    if report is None:
        yield {"type": "error", "detail": "模型未按 schema 返回"}
        return
    yield {
        "type": "plan",
        "plan": json.loads(report.model_dump_json()),
        "session_id": thread,
        "elapsed_ms": stats.elapsed_ms,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
    }
