"""端到端评测：跑 evals/cases.jsonl，输出通过率、失败原因、耗时与 token。

    python evals/run_eval.py                      # 全部用例
    python evals/run_eval.py --limit 2 --record   # 先跑 2 条并写入看板数据
    python evals/run_eval.py --prompt-version v1  # 换提示词版本

评测规则见 travel/eval.py（纯函数，可离线单测）。run_cases() 可被 ab_test 复用。
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


def run_cases(cases: list[dict], prompt_version: str | None = None, verbose: bool = True) -> dict:
    """跑一批用例，返回汇总结果（通过率、token、耗时、每条明细与失败原因）。"""
    graph, settings = build_agent(prompt_version=prompt_version)
    run_id = time.strftime("%m%d-%H%M%S")
    tag = prompt_version or os.getenv("PROMPT_VERSION") or "v2"

    details, problems_all = [], []
    passed = total_tokens = total_ms = 0
    for idx, case in enumerate(cases, 1):
        report, stats, thread = plan_trip(graph, settings, case["request"], session_id=f"eval-{tag}-{run_id}-{idx}")
        total_tokens += stats.total_tokens
        total_ms += stats.elapsed_ms

        if report is None:
            problems = ["模型未按 schema 返回"]
        else:
            problems = evaluate_report(report, case["expect"])

        ok = not problems
        passed += int(ok)
        problems_all.extend(problems)
        if verbose:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{idx}] {mark} {case['expect']['destination']} | {stats.as_line()}")
            for p in problems:
                print(f"        - {p}")
        details.append(
            {
                "request": case["request"],
                "passed": ok,
                "tokens": stats.total_tokens,
                "elapsed_ms": stats.elapsed_ms,
                "problems": problems,
            }
        )

    return {
        "prompt_version": tag,
        "model": settings.travel_model,
        "cases": len(cases),
        "passed": passed,
        "total_tokens": total_tokens,
        "total_ms": total_ms,
        "problems": problems_all,
        "details": details,
    }


def main(argv: list[str]) -> int:
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    version = argv[argv.index("--prompt-version") + 1] if "--prompt-version" in argv else None
    record = "--record" in argv

    if "--dry-run" in argv:
        cases = load_cases(limit)
        bad = [c for c in cases if not c.get("request") or not c.get("expect", {}).get("destination")]
        print(f"用例数 = {len(cases)}，字段校验：{'通过' if not bad else f'{len(bad)} 条缺字段'}")
        return 0 if not bad else 1

    cases = load_cases(limit)
    print(f"提示词版本 = {version or 'v2(默认)'} | 用例数 = {len(cases)}")
    summary = run_cases(cases, version)

    rate = summary["passed"] / summary["cases"] if summary["cases"] else 0
    print(
        f"\n通过率 {summary['passed']}/{summary['cases']} = {rate:.0%} | 模型={summary['model']} "
        f"| 累计 {summary['total_tokens']:,} token / {summary['total_ms']:,} ms"
    )

    if record:
        from evals.history import append_record, render_dashboard  # noqa: E402

        append_record(summary)
        render_dashboard()
        print("已写入 reports/history.jsonl 并刷新 reports/dashboard.md")

    return 0 if rate == 1 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))