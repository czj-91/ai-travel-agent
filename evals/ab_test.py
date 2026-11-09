"""Prompt A/B：同一批用例跑多个提示词版本，比通过率、token 与耗时。

    python evals/ab_test.py --limit 3
    python evals/ab_test.py --limit 3 --record

结论写进 reports/dashboard.md 的趋势表，避免「感觉 v2 更好」这种没有数据的判断。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evals.run_eval import load_cases, run_cases  # noqa: E402


def main(argv: list[str]) -> int:
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 3
    record = "--record" in argv
    versions = [v.strip() for v in (argv[argv.index("--versions") + 1].split(",") if "--versions" in argv else ["v1", "v2"])]

    cases = load_cases(limit)
    results = []
    for version in versions:
        print(f"\n=== 版本 {version}（{len(cases)} 条用例）===")
        summary = run_cases(cases, version)
        rate = summary["passed"] / summary["cases"] if summary["cases"] else 0
        avg_tokens = round(summary["total_tokens"] / max(1, summary["cases"]))
        avg_ms = round(summary["total_ms"] / max(1, summary["cases"]))
        results.append((version, rate, avg_tokens, avg_ms, summary))
        print(f"  -> 通过率 {rate:.0%} | 平均 {avg_tokens:,} token | 平均 {avg_ms:,} ms")

    print("\n=== 对比 ===")
    print("| 版本 | 通过率 | 平均 token | 平均耗时 |")
    print("| --- | --- | --- | --- |")
    for version, rate, avg_tokens, avg_ms, _ in results:
        print(f"| {version} | {rate:.0%} | {avg_tokens:,} | {avg_ms:,} ms |")

    if len(results) >= 2:
        (base_v, base_r, base_t, base_m, _), (new_v, new_r, new_t, new_m, _) = results[0], results[-1]
        d_rate = new_r - base_r
        d_tok = (new_t - base_t) / base_t if base_t else 0
        d_ms = (new_m - base_m) / base_m if base_m else 0
        print(f"\n{new_v} 相对 {base_v}：通过率 {d_rate:+.0%} | token {d_tok:+.0%} | 耗时 {d_ms:+.0%}")
        if d_rate > 0:
            print("  结论：质量提升，采用新版；成本变化见上表。")
        elif d_rate < 0:
            print("  结论：新版通过率下降，先定位失败类别（见 reports/dashboard.md），不要只看成本就换版。")
        else:
            if d_tok < 0:
                print(f"  结论：质量持平、成本下降 {abs(d_tok):.0%}，采用新版。")
            else:
                print("  结论：质量与成本都没有优势，保留旧版。")
        print("  注意：样本量小（每版 " + str(results[0][4]["cases"]) + " 条）时通过率差异可能来自模型随机性，"
              "重要决策请扩样到 10+ 条并重复 2 轮。")

    if record:
        from evals.history import append_record, render_dashboard  # noqa: E402

        for version, _, _, _, summary in results:
            append_record(summary)
        render_dashboard()
        print("已写入看板数据")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))