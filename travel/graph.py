"""Agent 组装：模型 + 工具 + 结构化输出 + 会话记忆 + 上下文压缩。

langgraph dev / Studio 挂载的就是本模块里的 agent 对象。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
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
from .observability import RunStats, Timer, usage_from_messages
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


# 结构化工具在 LangChain 里按 schema 类名注册（实测 238 个分片 / 348 字符 args 流式到达），
# 据此可以推「正在写第几天」的进度事件。
RESPONSE_TOOL_NAME = TripPlan.__name__
WRITE_REPORT_EVERY = 320  # 每累计这么多字符推一次，避免两百多个分片把流刷屏


def provenance_from_messages(messages) -> dict:
    """从**实际发生的工具结果**归纳依据来源，而不是让模型自述它用了什么。

    模型自述会漏、会编；工具消息是服务端事实。面板因此只报这些数。
    """
    summary = {"local_kb": 0, "web": 0, "weather_days": 0, "stale": 0, "calls": 0}
    for message in messages or []:
        if not isinstance(message, _ToolMessage):
            continue
        name = getattr(message, "name", "") or ""
        body = str(getattr(message, "content", ""))
        hits = sum(1 for line in body.splitlines() if line.startswith("- "))
        if hits == 0:
            continue
        summary["calls"] += 1
        if name == "poi_lookup":
            summary["local_kb"] += hits
        elif name == "web_search":
            summary["web"] += hits
        elif name == "weather_forecast":
            summary["weather_days"] += hits
        if "可能过期" in body:
            summary["stale"] += 1
    return summary


def cost_yuan(stats: RunStats, settings) -> float | None:
    """按配置的单价估算本次花费；没配单价就返回 None，绝不瞎猜价格。"""
    price_in = float(getattr(settings, "price_in_per_m", 0) or 0)
    price_out = float(getattr(settings, "price_out_per_m", 0) or 0)
    if price_in <= 0 and price_out <= 0:
        return None
    return stats.cost_yuan(price_in, price_out)


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
    return_messages: bool = False,
):
    """跑一次规划，返回 (TripPlan, RunStats, thread_id)；return_messages=True 时额外返回本轮消息。

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
    messages = result.get("messages") or []
    if return_messages:
        return report, stats, thread, messages
    return report, stats, thread


def stream_plan(graph, settings, request: str, session_id: str | None = None, image_block=None):
    """流式规划：逐段产出事件字典，最后给出结构化结果与本轮统计。

    事件类型：start / tool_call / tool_result(含 ms、hits、chars) / writing / plan(含 cost_yuan) / error。

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

    pending_names: dict[int, str] = {}
    tool_clock: dict[str, list[float]] = {}
    written = last_report = 0
    written_text = ""

    with Timer() as timer:
        yield {"type": "start", "session_id": thread, "model": settings.travel_model}
        for chunk, meta in graph.stream({"messages": [("user", content)]}, config, stream_mode="messages"):
            name = getattr(chunk, "name", None) or (
                meta.get("ls_agent_type") if isinstance(meta, dict) else None
            )
            if isinstance(chunk, _ToolMessage):
                body = str(getattr(chunk, "content", ""))
                started = tool_clock.get(name) or []
                elapsed_ms = round((time.perf_counter() - started.pop(0)) * 1000) if started else None
                yield {
                    "type": "tool_result",
                    "name": name,
                    # 结构化工具不是真工具，是最终方案本身；标出来让前端能区分展示
                    "kind": "response" if name == RESPONSE_TOOL_NAME else "tool",
                    "ms": elapsed_ms,
                    # 命中条数：POI 与搜索是「- 」开头的行，天气是「- 日期」行
                    "hits": sum(1 for line in body.splitlines() if line.startswith("- ")),
                    "chars": len(body),
                    "preview": body[:160],
                }
            elif isinstance(chunk, _AIMessageChunk):
                for call in (
                    getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None) or []
                ):
                    idx = call.get("index", 0) if isinstance(call, dict) else 0
                    call_name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
                    if call_name:
                        pending_names[idx] = call_name
                        tool_clock.setdefault(call_name, []).append(time.perf_counter())
                        if call_name == RESPONSE_TOOL_NAME:
                            # 结构化工具可能失败后重试（实测苏州请求出现两次 response），
                            # 每次新尝试重新计数，否则 chars/days_seen 会跨两次尝试累加，报出虚高的进度
                            written = last_report = 0
                            written_text = ""
                        yield {"type": "tool_call", "name": call_name}
                    # 结构化工具的 args 是分片流式到达的：累计它就能推出「正在写第几天」，
                    # 比只有一条转圈进度条有用得多
                    args_bit = (call.get("args") or "") if isinstance(call, dict) else ""
                    if args_bit and pending_names.get(idx) == RESPONSE_TOOL_NAME:
                        written += len(args_bit)
                        written_text += args_bit
                        if written - last_report >= WRITE_REPORT_EVERY:
                            last_report = written
                            yield {
                                "type": "writing",
                                "target": "plan",
                                "chars": written,
                                "days_seen": written_text.count(chr(34) + "day" + chr(34)),
                            }
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
        "cost_yuan": cost_yuan(stats, settings),
        "provenance": provenance_from_messages(messages),
    }
