"""裸模型 vs 本系统：同一批需求，两个臂跑同一套断言、事实核对与 judge。

回答面试里那句「这跟直接问 ChatGPT 有什么区别」——不能靠感觉，要有同口径对比。
两个臂唯一的差别是**有没有工具/知识库/天气**，判定标准完全共用。

    python evals/compare.py --limit 6
    python evals/compare.py --limit 6 --judge
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evals.run_eval import _judge, _kb_index, load_cases, run_cases
from travel.eval import evaluate_report
from travel.judge import average_score, fact_check_vs_kb
from travel.observability import RunStats, usage_from_messages

BARE_SYSTEM = (
    "你是旅行规划师。根据用户需求直接给出行程方案，只输出一个 JSON 对象，不要提问、不要解释。"
    "字段：destination, days, traveler_profile, best_season_note, "
    "plan[{day, theme, base_area, spots[{name, why, minutes, ticket_yuan}], meals, transport, day_cost_yuan}], "
    "budget_total_yuan, risks, references, summary。"
    "ticket_yuan 与 references 若无法确认就填 0 和空数组，不要编造。"
)


def parse_bare_output(response):
    """把裸模型输出转成 TripPlan，失败时给出**具体原因**。

    实测裸模型能吐出结构看着像样的 JSON，但 risks 写成整段字符串、references 留空——
    笼统记一句「没按 schema 输出」会把「格式对了但类型不对」这种有价值的差异抹平。
    """
    import json as _json

    from travel.graph import _first_json_object
    from travel.schemas import TripPlan

    text = response.content if isinstance(response.content, str) else str(response.content)
    candidate = _first_json_object(text)
    if not candidate:
        return None, "输出里找不到 JSON 对象"
    try:
        payload = _json.loads(candidate)
    except Exception as exc:
        return None, f"JSON 解析失败：{type(exc).__name__}"
    try:
        return TripPlan.model_validate(payload), None
    except Exception as exc:
        summary = str(exc).replace("\n", " ")[:160]
        return None, f"JSON 可解析但字段不合 schema：{summary}"


def build_bare_structured(model_name: str = "deepseek-chat"):
    """裸模型 + with_structured_output：只换「保证格式的手段」，仍然没有工具/知识库/天气。

    没有这个臂，裸模型 0/6 的结论会被误读成「模型能力不行」；
    实际上失败几乎全集中在 meals 字段类型上——那是**约束手段**的差异，不是知识差异。
    """
    from langchain.chat_models import init_chat_model

    from travel.schemas import TripPlan

    # include_raw=True：结构化输出直接返回对象时会丢掉 usage_metadata 与解析错误，
    # 上一版 C 臂「0 token、6/6 失败」就是这个测量缺陷，不是模型能力差异
    return init_chat_model(model_name).with_structured_output(TripPlan, include_raw=True)


def _extract_report(response):
    """兼容三种返回：AIMessage（自由文本）、TripPlan 对象、include_raw 的字典。"""
    from travel.schemas import TripPlan

    if isinstance(response, TripPlan):
        return response, None
    if isinstance(response, dict) and "parsed" in response:
        if response.get("parsed") is not None:
            return response["parsed"], None
        errors = response.get("parsing_errors") or []
        return None, "结构化输出解析失败：" + (str(errors[0])[:140] if errors else "无错误详情")
    return parse_bare_output(response)


def bare_plan_case(model, case: dict, use_judge: bool) -> dict:
    """单次模型调用，无工具、无知识库、无实时天气。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    started = time.perf_counter()
    stats = RunStats()
    report = None
    problems = ["调用失败"]
    try:
        response = model.invoke([SystemMessage(content=BARE_SYSTEM), HumanMessage(content=case["request"])])
        report, parse_error = _extract_report(response)
        stats = usage_from_messages({"messages": [response]})
        if (
            isinstance(response, dict)
            and isinstance(response.get("raw"), object)
            and hasattr(response["raw"], "usage_metadata")
        ):
            stats = usage_from_messages({"messages": [response["raw"]]})
        problems = [parse_error] if report is None else evaluate_report(report, case.get("expect") or {})
    except Exception as exc:
        problems = [f"调用失败：{type(exc).__name__}: {str(exc)[:120]}"]
    stats.elapsed_ms = round((time.perf_counter() - started) * 1000)

    facts: list[str] = []
    judge_avg = None
    if report is not None:
        facts = fact_check_vs_kb(report, _kb_index())
        if use_judge:
            judge_avg = average_score(_judge(case["request"], report.model_dump_json()))
    return {
        "request": case["request"],
        "passed": not problems and report is not None,
        "problems": problems + facts,
        "fact_mismatches": facts,
        "judge_avg": judge_avg,
        "tokens": stats.total_tokens,
        "elapsed_ms": stats.elapsed_ms,
    }


def summarize(rows: list[dict], label: str) -> dict:
    scores = [r["judge_avg"] for r in rows if r.get("judge_avg") is not None]
    return {
        "arm": label,
        "cases": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "rule_violations": sum(len([p for p in r["problems"] if "知识库" not in p]) for r in rows),
        "fact_mismatches": sum(len(r["fact_mismatches"]) for r in rows),
        "judge_avg": round(sum(scores) / len(scores), 2) if scores else None,
        "tokens": sum(r["tokens"] for r in rows),
        "avg_ms": round(sum(r["elapsed_ms"] for r in rows) / max(1, len(rows))),
    }


def main(argv: list[str]) -> int:
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 6
    use_judge = "--judge" in argv
    cases = load_cases(limit)
    print(f"对比用例数 = {len(cases)} | judge = {use_judge}\n")

    from langchain.chat_models import init_chat_model

    model = init_chat_model("deepseek-chat")

    print("=== 臂 A：裸模型（无工具/无知识库/无天气）===")
    bare_rows = []
    for index, case in enumerate(cases, 1):
        row = bare_plan_case(model, case, use_judge)
        bare_rows.append(row)
        mark = "PASS" if row["passed"] else "FAIL"
        print(
            f"  [{index}] {mark} {case['expect']['destination']} | {row['tokens']:,} token | {row['elapsed_ms']:,} ms"
        )
        for problem in row["problems"]:
            print(f"        - {problem}")

    print("\n=== 臂 C：裸模型 + 结构化输出机制（仍无工具/知识库/天气）===")
    structured_model = build_bare_structured()
    structured_rows = []
    for index, case in enumerate(cases, 1):
        row = bare_plan_case(structured_model, case, use_judge)
        structured_rows.append(row)
        mark = "PASS" if row["passed"] else "FAIL"
        print(
            f"  [{index}] {mark} {case['expect']['destination']} | {row['tokens']:,} token | {row['elapsed_ms']:,} ms"
        )
        for problem in row["problems"]:
            print(f"        - {problem}")

    print("\n=== 臂 B：本系统（RAG + 天气 + 结构化 + 兜底）===")
    agent_summary = run_cases(cases, use_judge=use_judge)
    agent_rows = agent_summary["details"]

    table = [
        summarize(bare_rows, "裸模型（prompt 约束）"),
        summarize(structured_rows, "裸模型+结构化输出"),
        summarize(agent_rows, "本系统（RAG+天气）"),
    ]
    print("\n=== 对比 ===")
    print("| 臂 | 用例 | 通过 | 规则违规 | 与知识库不一致 | judge 均分 | token | 平均耗时 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in table:
        print(
            f"| {row['arm']} | {row['cases']} | {row['passed']} | {row['rule_violations']} "
            f"| {row['fact_mismatches']} | {row['judge_avg']} | {row['tokens']:,} | {row['avg_ms']:,} ms |"
        )

    out = os.path.join(os.path.dirname(__file__), "..", "reports", "compare.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "cases": len(cases),
                "judge": use_judge,
                "arms": table,
                "details": {"bare": bare_rows, "bare_structured": structured_rows, "agent": agent_rows},
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n已写入 {os.path.abspath(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
