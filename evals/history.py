"""评测历史与看板：把每次跑分变成可回溯的数据，而不是截图。"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HISTORY = os.path.join(BASE_DIR, "reports", "history.jsonl")
DASHBOARD = os.path.join(BASE_DIR, "reports", "dashboard.md")

CATEGORIES = {
    "天数": "天数不符",
    "超过上限": "预算超支",
    "超过 600 分钟": "单日超载",
    "重复": "景点重复",
    "自相矛盾": "预算不自洽",
    "非法来源": "来源链接非法",
    "base_area": "缺活动区域",
    "references 为空": "缺参考来源",
    "summary 为空": "缺总结",
}


def classify(problem: str) -> str:
    for key, label in CATEGORIES.items():
        if key in problem:
            return label
    if "schema" in problem or "structured" in problem:
        return "未按 schema 返回"
    return "其他"


def append_record(record: dict) -> None:
    record.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_history() -> list[dict]:
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def render_dashboard() -> str:
    records = load_history()
    lines = [
        "# 评测看板",
        "",
        "> 由 `python evals/report.py` 生成，数据来自 `reports/history.jsonl`。",
        "",
        "## 趋势",
        "",
        "| 时间 | 版本 | 用例 | 通过 | 通过率 | token | 平均耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        rate = r["passed"] / r["cases"] if r["cases"] else 0
        avg_ms = round(r["total_ms"] / r["cases"]) if r["cases"] else 0
        lines.append(
            f"| {r['ts']} | {r.get('prompt_version','-')} | {r['cases']} | {r['passed']} "
            f"| {rate:.0%} | {r['total_tokens']:,} | {avg_ms:,} ms |"
        )

    counter: Counter[str] = Counter()
    for r in records:
        for problem in r.get("problems", []):
            counter[classify(problem)] += 1

    lines += ["", "## 失败类别分布", ""]
    if counter:
        lines += ["| 类别 | 次数 |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in counter.most_common()]
    else:
        lines.append("暂无失败记录。")

    latest = records[-1] if records else None
    lines += ["", "## 最近一次明细", ""]
    if latest:
        lines.append(f"- 版本：{latest.get('prompt_version','-')} | 模型：{latest.get('model','-')}")
        # summary 里 cases 是用例数量（int），明细在 details —— 之前误当成列表导致 TypeError
        for case in latest.get("details", []):
            mark = "✅" if case["passed"] else "❌"
            lines.append(f"  - {mark} {case['request'][:28]}｜{case['tokens']:,} token｜{case['elapsed_ms']:,} ms")
            for problem in case.get("problems", []):
                lines.append(f"      - {problem}")
    else:
        lines.append("还没有跑过评测。执行 `python evals/run_eval.py --record`。")

    text = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(DASHBOARD), exist_ok=True)
    with open(DASHBOARD, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text