"""工具层：把第三方搜索客户端封装成可控、可测、能降级兜底的 @tool。"""
from __future__ import annotations

import time

from langchain.tools import tool

# Tavily 原始返回里 content 很长，还带 images/raw_content；整包塞进上下文会直接
# 推高 token 并稀释模型注意力，所以只保留决策需要的「标题 | 链接 + 短摘要」。
SNIPPET_LIMIT = 220
# 5 条降到 3 条：实测第 4、5 条摘要几乎不被模型引用，却照样进上下文计费
DEFAULT_MAX_RESULTS = 3


def format_search_results(raw, limit: int = DEFAULT_MAX_RESULTS) -> str:
    """把搜索返回压成精简文本。纯函数，不联网也能单测。"""
    if isinstance(raw, str):
        return raw[: SNIPPET_LIMIT * limit]

    results = raw.get("results") or [] if isinstance(raw, dict) else []

    lines: list[str] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        snippet = " ".join(str(item.get("content") or "").split())[:SNIPPET_LIMIT]
        if not (title or url):
            continue
        lines.append(f"- {title} | {url}\n  {snippet}")

    return "\n".join(lines) if lines else "没有找到相关结果。"


def search_with_retry(client, query: str, *, retries: int = 2, timeout: float = 15.0):
    """带退避重试的检索，返回 (给模型看的文本, 统计 dict)。

    timeout 是「重试总时长上限」，超过就不再重试；不是 socket 级超时。
    工具异常不往外抛——让模型基于常识继续，并明确要求它把 references 留空。
    """
    started = time.perf_counter()
    stats = {"attempts": 0, "ok": False, "hits": 0, "elapsed_ms": 0}
    last_error = ""

    for attempt in range(1, retries + 2):
        stats["attempts"] = attempt
        try:
            raw = client.invoke(query)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            if time.perf_counter() - started >= timeout:
                break
            time.sleep(min(0.8 * attempt, 2.0))
            continue

        text = format_search_results(raw)
        stats["ok"] = True
        stats["hits"] = sum(1 for line in text.splitlines() if line.startswith("- "))
        stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return text, stats

    stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return (
        f"搜索失败（尝试 {stats['attempts']} 次）：{last_error}。"
        "请基于常识规划行程，并把 references 留空列表。",
        stats,
    )


def build_search_tool(max_results: int = DEFAULT_MAX_RESULTS, retries: int = 2, timeout: float = 15.0):
    """构造联网搜索工具。依赖 TAVILY_API_KEY（由 travel.config 在启动时校验）。"""
    from langchain_tavily import TavilySearch

    client = TavilySearch(max_results=max_results, topic="general")

    @tool
    def web_search(query: str) -> str:
        """检索真实旅行攻略、门票价格、闭馆时间与交通信息，返回精简后的标题、链接和摘要。

        Args:
            query: 检索词，例如 "杭州 3天 人文 景点 门票 闭馆 时间"
        """
        text, _ = search_with_retry(client, query, retries=retries, timeout=timeout)
        return text

    return web_search