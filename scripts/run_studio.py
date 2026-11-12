"""启动 LangGraph 本地服务（配合 LangSmith Studio 可视化调试）。

为什么要绕一下：项目目录从 "ai open" 改名成 "ai travel" 之后，uv 生成的
.venv/Scripts/*.exe 垫片里记录的还是旧绝对路径，直接执行会报
`error: uv trampoline failed to canonicalize script path`。
这里直接用当前解释器调用 CLI 入口，绕开垫片。

用法：
    python scripts/run_studio.py            # 只监听本机，浏览器里的 Studio 直连 127.0.0.1
    python scripts/run_studio.py --tunnel   # 需要别的设备访问时给一个公网隧道地址
"""

from __future__ import annotations

import sys

from langgraph_cli.cli import cli

if __name__ == "__main__":
    sys.argv = ["langgraph", "dev", "--no-reload", *sys.argv[1:]]
    cli()
