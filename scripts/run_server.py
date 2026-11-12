"""本地一键起网页服务：不用手动设环境变量。

    python scripts/run_server.py            # 默认 http://127.0.0.1:8080

为什么需要这个脚本：本机 Windows Smart App Control 会拦未签名的 `_uuid_utils.pyd`，
导致 `import langchain_core` 直接失败。`devtools/uuid_shim/` 是一个纯 Python 的
`uuid_utils.compat.uuid7` 替身，只在本地开发时插到 sys.path 最前面。
该目录已 gitignore，不会进仓库；正解是容器化（Linux 镜像没有 SAC）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "devtools" / "uuid_shim"

os.environ.setdefault("PYTHONUTF8", "1")
if SHIM.is_dir():
    sys.path.insert(0, str(SHIM))
    print(f"[dev] 已启用本地 uuid_utils 替身：{SHIM}")
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT") or 8080)
    print(f"旅行规划 Agent 网页版：http://127.0.0.1:{port}")
    uvicorn.run("service:app", host="127.0.0.1", port=port, app_dir=str(ROOT))
