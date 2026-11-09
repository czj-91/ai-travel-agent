"""端到端评测：跑 evals/cases.jsonl，输出通过率、失败原因和耗时/token 统计。

    python evals/run_eval.py                 # 跑全部用例（会真实调用模型与搜索）
    python evals/run_eval.py --limit 2       # 先跑 2 条看效果
    python evals/run_eval.py --dry-run       # 不联网，只校验用例文件与规则本身

评测规则见 travel/eval.py（纯函数，可离线单测）。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel.eval import evaluate_report  # noqa: E402
from travel.graph import build_agent, plan_trip  # noqa: E402

CASES = os.path.join(os.path.dirname(__file__), "cases.jsonl")


def load_cases(limit: int | None = None) -> list[dict]:
    with open(CASES, encoding="utf-8") as fh:
        cases = [json.loads(line) for line in fh if line.strip()]
    return cases[:limit] if limit else cases


def main(argv: list[str]) -> int:
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    dry = "--dry-run" in argv

    cases = load_cases(limit)
    print(f"用例数 = {len(cases)}（dry_run={dry}）")

    if dry:
        problems = [c for c in cases if not c.get("request") or not c.get("expect", {}).get("destination")]
        print("用例文件校验:", "通过" if not problems else f"{len(problems)} 条缺字段")
        return 0 if not problems else 1

    graph, settings = build_agent()
    run_id = time.strftime('%m%d-%H%M%S')  # 每次评测独立 thread，避免复用上一轮的会话状态
    passed, total_tokens, total_ms = 0, 0, 0

    for idx, case in enumerate(cases, 1):
        report, stats, thread = plan_trip(graph, case["request"], session_id=f"eval-{run_id}-{idx}")
        total_tokens += stats.total_tokens
        total_ms += stats.elapsed_ms

        if report is None:
            print(f"  [{idx}] FAIL 未按 schema 返回 | {stats.as_line()}")
            continue

        problems = evaluate_report(report, case["expect"])
        if problems:
            print(f"  [{idx}] FAIL {case['expect']['destination']} | {stats.as_line()}")
            for p in problems:
                print(f"        - {p}")
        else:
            passed += 1
            print(f"  [{idx}] PASS {report.destination} {report.days}天 | {stats.as_line()}")

    rate = passed / len(cases) if cases else 0
    print(
        f"\n通过率 {passed}/{len(cases)} = {rate:.0%} | 模型={settings.travel_model} "
        f"| project={settings.langsmith_project} | 累计 {total_tokens} token / {total_ms} ms"
    )
    print(f"逐条 trace 可在 LangSmith 里按 session=eval-{run_id}-<n> 过滤查看。")
    return 0 if rate == 1 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))