"""运行统计：耗时与 token 用量，用来算成本、做趋势对比。"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RunStats:
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost_yuan(self, price_in_per_m: float, price_out_per_m: float) -> float:
        """按每百万 token 单价估算本次花费。"""
        return round(
            self.input_tokens / 1_000_000 * price_in_per_m
            + self.output_tokens / 1_000_000 * price_out_per_m,
            6,
        )

    def as_line(self) -> str:
        return (
            f"耗时 {self.elapsed_ms} ms | token 入 {self.input_tokens} "
            f"出 {self.output_tokens} 合计 {self.total_tokens}"
        )


def usage_from_messages(result: dict) -> RunStats:
    """累加消息里的 usage_metadata。纯函数，可用假消息离线测。"""
    stats = RunStats()
    for message in (result or {}).get("messages", []):
        usage = getattr(message, "usage_metadata", None) or (message.get("usage_metadata") if isinstance(message, dict) else None)
        if not usage:
            continue
        stats.input_tokens += int(usage.get("input_tokens") or 0)
        stats.output_tokens += int(usage.get("output_tokens") or 0)
    return stats


class Timer:
    def __enter__(self):
        self._started = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed_ms = round((time.perf_counter() - self._started) * 1000)
        return False