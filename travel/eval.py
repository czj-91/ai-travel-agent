"""输出质量校验：把「这份行程合不合理」变成可自动执行的规则。

不依赖模型、不联网，因此可以离线单测，也可以在评测脚本里复用。
"""
from __future__ import annotations

MAX_DAY_MINUTES = 600
# 中间天的游玩时长下限：首末天通常是到达/返程，排不满是正常的
MIN_DAY_MINUTES = 120


def evaluate_report(report, expect: dict | None = None) -> list[str]:
    """返回违规项列表；空列表表示通过。"""
    expect = expect or {}
    problems: list[str] = []

    if report.days != len(report.plan):
        problems.append(f"days={report.days} 与 plan 天数 {len(report.plan)} 不一致")

    day_numbers = [d.day for d in report.plan]
    if day_numbers != list(range(1, len(report.plan) + 1)):
        problems.append(f"天序号不连续或乱序：{day_numbers}")

    for index, day in enumerate(report.plan):
        if len(day.spots) < 2:
            problems.append(f"第 {day.day} 天只有 {len(day.spots)} 个游玩点，少于 2 个")
        total = sum(s.minutes for s in day.spots)
        if total > MAX_DAY_MINUTES:
            problems.append(f"第 {day.day} 天游玩时长 {total} 分钟，超过 {MAX_DAY_MINUTES} 分钟上限")
        if 0 < index < len(report.plan) - 1 and total < MIN_DAY_MINUTES:
            problems.append(f"第 {day.day} 天游玩时长只有 {total} 分钟，中间天空转（下限 {MIN_DAY_MINUTES} 分钟）")
        if not day.base_area.strip():
            problems.append(f"第 {day.day} 天没有填 base_area，无法判断通勤是否合理")

    names = [s.name for d in report.plan for s in d.spots]
    duplicated = {n for n in names if names.count(n) > 1}
    if duplicated:
        problems.append(f"景点跨天重复：{'、'.join(sorted(duplicated))}")

    day_sum = round(sum(d.day_cost_yuan for d in report.plan), 2)
    if day_sum > 0 and report.budget_total_yuan < day_sum * 0.8:
        problems.append(
            f"总预算 {report.budget_total_yuan} 明显低于各天花费合计 {day_sum}，数字自相矛盾"
        )

    bad_refs = [r for r in report.references if not str(r).startswith("http")]
    if bad_refs:
        problems.append(f"存在非法来源链接：{bad_refs[:2]}")
    if expect.get("require_sources") and not report.references:
        problems.append("要求给出参考来源，但 references 为空")

    if expect.get("days") and report.days != int(expect["days"]):
        problems.append(f"用户要求 {expect['days']} 天，实际只排了 {report.days} 天")

    if expect.get("max_budget") and report.budget_total_yuan > float(expect["max_budget"]) * 1.1:
        problems.append(
            f"人均预算 {report.budget_total_yuan:.0f} 元超过上限 {expect['max_budget']} 元（容差 10%）"
        )

    if expect.get("destination") and expect["destination"] not in report.destination:
        problems.append(f"目的地不是期望的 {expect['destination']}，实际是 {report.destination}")

    # 用户点名要的东西必须真的出现在行程里：否则「通过率 100%」只证明格式对，不证明需求被满足
    haystack = " ".join(
        [report.summary, report.traveler_profile, report.best_season_note]
        + [f"{day.theme} {day.base_area} {' '.join(day.meals)}" for day in report.plan]
        + [f"{spot.name} {spot.why}" for spot in report.flattened_spots()]
    ).lower()
    for keyword in expect.get("must_include") or []:
        text = str(keyword).lower().strip()
        if text and text not in haystack:
            problems.append(f"用户点名要的「{keyword}」没有出现在行程里")

    if not report.summary.strip():
        problems.append("summary 为空")

    return problems
