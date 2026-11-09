"""三个 API Key 的冒烟测试：只打印结果摘要，不打印 key。"""
import os
import warnings

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_community.chat_models.tongyi import ChatTongyi
from langchain_tavily import TavilySearch

warnings.filterwarnings("ignore", message=r".*langchain-community.*")

load_dotenv()


def key_state(name):
    value = os.environ.get(name)
    if not value:
        return f"{name}: 未加载（检查 .env 是否在本目录、行首有没有 #）"
    return f"{name}: 已加载 len={len(value)} 前缀={value[:5]}"


def check(name, fn):
    try:
        print(f"  OK  {name}: {str(fn())[:70]}")
    except Exception as exc:
        low = str(exc).lower()
        hint = ""
        if any(k in low for k in ("failed to establish", "timed out", "connection", "proxy", "unreachable", "ssl")):
            hint = "  -> 网络不通/需要代理，不是 key 的问题"
        elif any(k in low for k in ("401", "403", "invalidapikey", "invalid api key", "unauthorized", "incorrect api key")):
            hint = "  -> 鉴权失败：核对 key 与地域是否匹配（北京/新加坡不能混用）"
        elif any(k in low for k in ("429", "quota", "insufficient", "free plan")):
            hint = "  -> 额度问题：免费额度用尽或该能力未开通"
        print(f"  ERR {name}: {type(exc).__name__}: {str(exc)[:150]}{hint}")


print("--- 环境变量 ---")
for key in ("TAVILY_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
    print(" ", key_state(key))

print("--- 实发请求 ---")
check("Tavily 搜索", lambda: TavilySearch(max_results=2, topic="general").invoke("蒸蚌是什么梗？"))
check("百炼 ChatTongyi", lambda: ChatTongyi(model="qwen-plus").invoke("只回复两个字：收到").content)
check("百炼 OpenAI兼容", lambda: init_chat_model(
    "qwen-plus", model_provider="openai",
    openai_api_base=os.environ["DASHSCOPE_BASE_URL"],
    openai_api_key=os.environ["DASHSCOPE_API_KEY"],
).invoke("只回复两个字：收到").content)
check("DeepSeek", lambda: init_chat_model("deepseek-chat").invoke("只回复两个字：收到").content)