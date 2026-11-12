"""渲染评测看板：python evals/report.py"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evals.history import DASHBOARD, render_dashboard

if __name__ == "__main__":
    print(render_dashboard())
    print(f"已写入 {DASHBOARD}")
