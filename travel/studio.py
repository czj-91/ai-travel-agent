"""LangGraph Studio 入口。

只有这里保留「导入即建图」的副作用：langgraph dev 需要一个模块级 graph 对象。
业务代码请 import travel.graph 里的 build_agent()/plan_trip()，不要 import 本模块，
否则缺少密钥时会在导入阶段就报错，也会白白构造一次 agent。
"""
from travel.graph import build_agent

agent, settings = build_agent()