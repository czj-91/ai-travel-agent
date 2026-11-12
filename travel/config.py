"""运行时配置：缺 key 立刻报错，绝不静默回退到别的默认端点。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env 必须保持纯 ASCII：langgraph dev 用系统默认编码（中文 Windows 是 GBK）读它，
# 出现中文会直接 UnicodeDecodeError 崩掉。
load_dotenv(BASE_DIR / ".env")


class MissingEnvError(RuntimeError):
    """缺少必需的环境变量。"""


def require_env(name: str, hint: str = "") -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        tip = f" 获取方式：{hint}" if hint else ""
        raise MissingEnvError(
            f"环境变量 {name} 未设置或为空。{tip}\n"
            " 请在项目根目录 .env 中补上（参考 .env.example）；"
            "改完 .env 需要重启 kernel 或重跑进程才会生效。"
        )
    return value


@dataclass(frozen=True)
class Settings:
    travel_model: str
    deepseek_api_key: str | None
    dashscope_api_key: str | None
    dashscope_base_url: str | None
    tavily_api_key: str
    langsmith_project: str
    db_path: str
    search_timeout: float
    search_retries: int
    # 单价给默认值：新增配置项不该让既有构造点全部崩掉
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0


def load_settings() -> Settings:
    travel_model = (os.getenv("TRAVEL_MODEL") or "deepseek-chat").strip()
    deepseek = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    dashscope = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    base_url = (os.getenv("DASHSCOPE_BASE_URL") or "").strip()

    if travel_model.startswith("qwen"):
        dashscope = require_env("DASHSCOPE_API_KEY", "https://bailian.console.aliyun.com")
        # 必须显式校验：留空会让 OpenAI SDK 静默回退到 api.openai.com，
        # 表现为几十秒后抛 Connection error，极难定位（本项目真实踩过这个坑）。
        base_url = require_env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    else:
        deepseek = require_env("DEEPSEEK_API_KEY", "https://platform.deepseek.com/api_keys")

    return Settings(
        travel_model=travel_model,
        deepseek_api_key=deepseek or None,
        dashscope_api_key=dashscope or None,
        dashscope_base_url=base_url or None,
        tavily_api_key=require_env("TAVILY_API_KEY", "https://app.tavily.com/home"),
        langsmith_project=(os.getenv("LANGSMITH_PROJECT") or "ai-travel").strip(),
        db_path=str(BASE_DIR / "resources" / "checkpoint.db"),
        search_timeout=float(os.getenv("SEARCH_TIMEOUT") or 15),
        search_retries=int(os.getenv("SEARCH_RETRIES") or 2),
        # 单价按「每 100 万 token 的货币金额」配置，留空即不估算成本
        price_in_per_m=float(os.getenv("PRICE_IN_PER_M") or 0),
        price_out_per_m=float(os.getenv("PRICE_OUT_PER_M") or 0),
    )
