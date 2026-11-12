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

from travel.eval import evaluate_report
from travel.graph import build_agent, plan_trip
from travel.judge import average_score, build_judge, fact_check_vs_kb
from travel.observability import RunStats

CASES = os.path.join(os.path.dirname(__file__), "cases.jsonl")


def load_cases(limit: int | None = None) -> list[dict]:
    with open(CASES, encoding="utf-8") as fh:
        cases = [json.loads(line) for line in fh if line.strip()]
    return cases[:limit] if limit else cases


def run_case(
    graph, settings, case: dict, index: int, tag: str, run_id: str, use_judge: bool = False
) -> tuple[dict, bool, RunStats]:
    """跑单条用例，返回 (明细, 是否通过, 统计)。

    模型或网络异常按「本条失败」记账，不让一条用例炸掉整轮：实测 DeepSeek 余额不足时
    402 直接把第 2 条之后的跑分全打断，看板连一轮完整数据都收不到。
    """
    request, expect = case["request"], case.get("expect") or {}
    started = time.perf_counter()
    stats = RunStats()
    facts: list[str] = []
    judge_avg = None
    try:
        report, stats, _ = plan_trip(graph, settings, request, session_id=f"eval-{tag}-{run_id}-{index}")
        problems = evaluate_report(report, expect) if report is not None else ["模型未按 schema 返回"]
        if report is not None:
            # 事实一致率是独立指标，不并入 problems：混进去会把两种失败原因搅成一团
            facts = fact_check_vs_kb(report, _kb_index())
            if use_judge:
                judge_avg = average_score(_judge(request, report.model_dump_json()))
    except Exception as exc:
        stats = RunStats(elapsed_ms=round((time.perf_counter() - started) * 1000))
        problems = [f"调用失败：{type(exc).__name__}: {str(exc)[:120]}"]
    detail = {
        "request": request,
        "passed": not problems,
        "tokens": stats.total_tokens,
        "elapsed_ms": stats.elapsed_ms,
        "problems": problems,
        "fact_mismatches": facts,
        "judge_avg": judge_avg,
    }
    return detail, not problems, stats


_JUDGE = None
_KB = None


def _judge(request: str, plan_json: str):
    global _JUDGE
    if _JUDGE is None:
        _JUDGE = build_judge()
    return _JUDGE(request, plan_json)


def _kb_index():
    """事实核对用的知识库索引：固定走关键词召回，结果可复现、不依赖外部 embedding。"""
    global _KB
    if _KB is None:
        from travel.rag import PoiIndex

        _KB = PoiIndex(use_embeddings=False)
    return _KB


def run_cases(
    cases: list[dict], prompt_version: str | None = None, verbose: bool = True, use_judge: bool = False
) -> dict:
    """跑一批用例，返回汇总结果（通过率、token、耗时、每条明细与失败原因）。"""
    graph, settings = build_agent(prompt_version=prompt_version)
    run_id = time.strftime("%m%d-%H%M%S")
    tag = prompt_version or os.getenv("PROMPT_VERSION") or "v2"

    details, problems_all = [], []
    passed = total_tokens = total_ms = 0
    for index, case in enumerate(cases, 1):
        detail, ok, stats = run_case(graph, settings, case, index, tag, run_id, use_judge)
        total_tokens += stats.total_tokens
        total_ms += stats.elapsed_ms
        passed += int(ok)
        problems_all.extend(detail["problems"])
        if verbose:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{index}] {mark} {case['expect']['destination']} | {stats.as_line()}")
            for problem in detail["problems"]:
                print(f"        - {problem}")
        details.append(detail)

    return {
        "prompt_version": tag,
        "fact_mismatches": sum(len(d["fact_mismatches"]) for d in details),
        "judge_scores": [d["judge_avg"] for d in details if d.get("judge_avg") is not None],
        "model": settings.travel_model,
        "cases": len(cases),
        "passed": passed,
        "total_tokens": total_tokens,
        "total_ms": total_ms,
        "problems": problems_all,
        "details": details,
    }


def run_cases_parallel(
    cases: list[dict],
    prompt_version: str | None = None,
    use_judge: bool = False,
    parallel: int = 4,
    verbose: bool = True,
) -> dict:
    """并发跑用例：每条用例的 thread_id 本来就互相独立，串行只是浪费。

    实测串行 20 条约 8.3 分钟；并发 4 能压到 2-3 分钟。
    注意 graph 与 checkpointer 是多线程共享的，SQLite 连接开了 check_same_thread=False，
    写入由 sqlite 自身串行化保证，因此这里只并行「等模型」的部分。
    """
    from concurrent.futures import ThreadPoolExecutor

    graph, settings = build_agent(prompt_version=prompt_version)
    run_id = time.strftime("%m%d-%H%M%S")
    tag = prompt_version or os.getenv("PROMPT_VERSION") or "v2"

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {
            index: pool.submit(run_case, graph, settings, case, index, tag, run_id, use_judge)
            for index, case in enumerate(cases, 1)
        }
        collected = {index: future.result() for index, future in futures.items()}

    details, problems_all = [], []
    passed = total_tokens = total_ms = 0
    for index in sorted(collected):
        detail, ok, stats = collected[index]
        total_tokens += stats.total_tokens
        total_ms += stats.elapsed_ms
        passed += int(ok)
        problems_all.extend(detail["problems"])
        details.append(detail)
        if verbose:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{index}] {mark} {cases[index - 1]['expect']['destination']} | {stats.as_line()}")
            for problem in detail["problems"]:
                print(f"        - {problem}")

    return {
        "prompt_version": tag,
        "fact_mismatches": sum(len(d["fact_mismatches"]) for d in details),
        "judge_scores": [d["judge_avg"] for d in details if d.get("judge_avg") is not None],
        "model": settings.travel_model,
        "cases": len(cases),
        "passed": passed,
        "total_tokens": total_tokens,
        "total_ms": total_ms,
        "parallel": parallel,
        "problems": problems_all,
        "details": details,
    }


def main(argv: list[str]) -> int:
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    version = argv[argv.index("--prompt-version") + 1] if "--prompt-version" in argv else None
    record = "--record" in argv
    use_judge = "--judge" in argv
    parallel = int(argv[argv.index("--parallel") + 1]) if "--parallel" in argv else 1

    if "--dry-run" in argv:
        cases = load_cases(limit)
        bad = [c for c in cases if not c.get("request") or not c.get("expect", {}).get("destination")]
        print(f"用例数 = {len(cases)}，字段校验：{'通过' if not bad else f'{len(bad)} 条缺字段'}")
        return 0 if not bad else 1

    cases = load_cases(limit)
    print(f"提示词版本 = {version or 'v2(默认)'} | 用例数 = {len(cases)}")
    if parallel > 1:
        summary = run_cases_parallel(cases, version, use_judge=use_judge, parallel=parallel)
    else:
        summary = run_cases(cases, version, use_judge=use_judge)

    rate = summary["passed"] / summary["cases"] if summary["cases"] else 0
    planned = [d for d in summary["details"] if d.get("fact_mismatches") is not None]
    print(f"  与本地知识库门票价不一致 = {summary['fact_mismatches']} 条（{len(planned)} 条方案参与核对）")
    scores = summary.get("judge_scores") or []
    if scores:
        print(f"  LLM-as-judge 均分 = {sum(scores) / len(scores):.2f} / 5（{len(scores)} 条已判分）")
    print(
        f"\n通过率 {summary['passed']}/{summary['cases']} = {rate:.0%} | 模型={summary['model']} "
        f"| 累计 {summary['total_tokens']:,} token / {summary['total_ms']:,} ms"
    )

    if record:
        from evals.history import append_record, render_dashboard

        append_record(summary)
        render_dashboard()
        print("已写入 reports/history.jsonl 并刷新 reports/dashboard.md")

    return 0 if rate == 1 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
