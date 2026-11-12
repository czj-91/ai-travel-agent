"""安全基线：外部内容隔离、速率限制、来源白名单。

三件事都只做最小可用实现，但必须是**默认生效**的：
1. 工具返回的网页/天气/知识库内容属于不可信外部数据，可能被塞进「忽略以上指令」这类
   注入文本。这里统一包一层带来源标签的定界符，并把内容里出现的闭合标签拆掉，
   防止「内容自己结束定界符」这种越狱。
2. 单次 /plan 会烧掉上万 token，必须有速率限制，否则一个页面刷新就能刷爆额度。
3. 跨源请求要显式白名单，不能默认 allow_origins=["*"] + 允许凭证。
"""

from __future__ import annotations

import os
import re
import time

MAX_EXTERNAL_CHARS = 4000  # 单条外部内容进上下文的硬上限
_CLOSING_TAG = re.compile(r"</\s*external_data\s*>", re.IGNORECASE)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def wrap_untrusted(content: str, source: str) -> str:
    """把外部内容包进 <external_data source="...">，并中和越狱尝试。"""
    text = _CONTROL_CHARS.sub(" ", str(content or ""))
    text = _CLOSING_TAG.sub("[removed-tag]", text)  # 内容不能自己关掉定界符
    text = text.strip()
    truncated = len(text) > MAX_EXTERNAL_CHARS
    if truncated:
        text = text[:MAX_EXTERNAL_CHARS] + "…"
    return f'<external_data source="{source}">\n{text}\n</external_data>' + (
        "\n[内容已截断]" if truncated else ""
    )


def origins_from_env(default: str = "http://127.0.0.1:8080,http://localhost:8080") -> list[str]:
    value = (os.getenv("ALLOWED_ORIGINS") or default).strip()
    return [item.strip() for item in value.split(",") if item.strip()]


class RateLimiter:
    """按 key（一般是客户端 IP）的固定窗口计数。单进程内存版，够用且可单测。

    多进程/多实例部署必须换成共享存储（Redis 等），这里显式不做假象。
    """

    def __init__(self, limit: int = 10, window_seconds: int = 60, clock=time.monotonic):
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self.clock = clock
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> tuple[bool, float]:
        """返回 (是否放行, 建议重试秒数)。"""
        now = self.clock()
        cutoff = now - self.window
        stamps = [stamp for stamp in self._hits.get(key, []) if stamp > cutoff]
        if len(stamps) >= self.limit:
            retry = max(0.0, self.window - (now - stamps[0]))
            self._hits[key] = stamps
            return False, round(retry, 1)
        stamps.append(now)
        self._hits[key] = stamps
        return True, 0.0

    def reset(self) -> None:
        """清空窗口。测试之间必须隔离状态，运维也需要手动解闸。"""
        self._hits.clear()

    def remaining(self, key: str) -> int:
        now = self.clock()
        stamps = [stamp for stamp in self._hits.get(key, []) if stamp > now - self.window]
        return max(0, self.limit - len(stamps))
