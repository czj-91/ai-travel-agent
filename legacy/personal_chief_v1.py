# 说明：这是课程 v1 阶段的单文件版本（原 personal_chief.py），主题已切换到旅行规划，
# 主实现见 ../travel/。保留在此仅作演进记录。
# 注意本文件里的 base_url=os.getenv(...) 未做校验：留空会让 OpenAI SDK 静默回退到
# api.openai.com，travel/config.py 已修复该问题并加了单测。
import os

# 1.加载环境变量
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_tavily import TavilySearch

load_dotenv()

# 2.使用tavily作为web搜索工具
web_search = TavilySearch(max_results=5, topic="general")

# 3.多模态模型
model = init_chat_model(
    model="qwen3.5-plus",  # 这是一个支持图片、文本、视频多模态的模型
    model_provider="openai",
    base_url=os.getenv("DASHSCOPE_BASE_URL"),
    api_key=os.getenv("DASHSCOPE_API_KEY"),
)

# 4.系统提示词
system_prompt = """
你是一名私人厨师。收到用户提供的食材照片或清单后，请按以下流程操作:
1.识别和评估食材：若用户提供照片，首先辨识所有可见食材。基于食材的外观状态，评估其新鲜度与可用量，整理出一份“当前可用食材清单”。
2.智能食谱检索：优先调用 web_search 工具，以“可用食材清单”为核心关键字，查找可行菜谱。
3.多维度评估与排序：从营养价值和制作难度两个维度对检索到的候选食谱进行量化打分，并根据得分排序，制作简单且营养丰富的排名靠前。
4.结构化方案输出：把排序后的食谱整理为一份结构清晰的建议报告，要包含食谱信息、得分、推荐理由、食谱的参考图片，帮助用户快速做出决策。

请严格按照流程，优先调用 web_search 工具搜索食谱，搜索不到的情况下才能自己发挥。
"""

# 5.创建Agent
agent = create_agent(
    model=model,  # 模型
    tools=[web_search],  # 工具
    system_prompt=system_prompt,  # 系统提示词
)

# 6.本地部署：uv add langgraph-cli[inmem]，新增 langgraph.json，
#   然后 $env:PYTHONUTF8=1（解决 gbk 编码报错）+ uv run langgraph dev --tunnel --no-reload
#   http://127.0.0.1:2024/docs 看自动生成的接口；
#   Studio: https://smith.langchain.com/studio/?baseUrl=<每次启动变化的隧道地址>
