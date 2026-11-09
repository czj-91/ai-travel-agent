"""LangGraph Studio 入口。

只有这里保留「导入即建图」的副作用：langgraph dev 需要一个模块级 graph 对象。
业务代码请 import travel.graph 里的 build_agent()/plan_trip()，不要 import 本模块。

platform_persistence=True：不挂自定义 checkpointer。langgraph dev 会因为图里带
SqliteSaver 这类自定义 checkpointer 直接抛 GraphLoadError 拒绝启动（持久化由平台接管）。
"""
from travel.graph import build_agent

agent, settings = build_agent(platform_persistence=True)