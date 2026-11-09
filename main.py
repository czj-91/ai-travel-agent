"""命令行入口：说出出行需求，拿回一份结构化行程方案。

用法：
    python main.py 杭州 3天 2人 喜欢人文 不吃辣 预算人均1500
    python main.py --session trip1 杭州 3天        # 同一 session 会延续记忆
"""
from __future__ import annotations

import sys

from travel.graph import build_agent, plan_trip


def render(report) -> str:
    lines = [
        f"目的地：{report.destination} | {report.days} 天 | 人均预算 {report.budget_total_yuan:.0f} 元（不含住宿与往返大交通）",
        f"需求复述：{report.traveler_profile}",
    ]
    if report.best_season_note:
        lines.append(f"出行提示：{report.best_season_note}")
    lines.append("")

    for day in report.plan:
        minutes = sum(s.minutes for s in day.spots)
        lines.append(f"第 {day.day} 天 · {day.theme}（{day.base_area}，约 {minutes/60:.1f} 小时，{day.day_cost_yuan:.0f} 元/人）")
        for i, spot in enumerate(day.spots, 1):
            ticket = "免费" if spot.ticket_yuan == 0 else f"{spot.ticket_yuan:.0f} 元"
            lines.append(f"   {i}. {spot.name}（{spot.minutes} 分钟 / {ticket}）—— {spot.why}")
        if day.meals:
            lines.append(f"   吃：{'；'.join(day.meals)}")
        lines.append(f"   行：{day.transport}")
        lines.append("")

    if report.risks:
        lines.append("需要注意：")
        lines.extend(f"   - {r}" for r in report.risks)
        lines.append("")
    if report.references:
        lines.append("参考来源：")
        lines.extend(f"   - {u}" for u in report.references)
        lines.append("")
    lines.append(f"总结：{report.summary}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    session_id, image_path = None, None
    while len(argv) >= 3 and argv[1].startswith("--"):
        if argv[1] == "--session":
            session_id, argv = argv[2], argv[3:]
        elif argv[1] == "--image":
            image_path, argv = argv[2], argv[3:]
        else:
            break

    request = " ".join(argv[1:]).strip() or input("这次想去哪、几天、几个人、有什么偏好？").strip()
    if not request:
        print("没有输入需求，退出。")
        return 1

    graph, settings = build_agent()
    print(f"模型={settings.travel_model} | 工具=poi_lookup / weather_forecast / web_search | LangSmith project={settings.langsmith_project}")
    print(f"需求：{request}\n正在检索攻略并编排行程，请稍候…\n")

    try:
        report, stats, thread = plan_trip(graph, settings, request, session_id, image_path)
    except ValueError as exc:
        print(f"输入有问题：{exc}")
        return 1
    if report is None:
        print("模型没有按 schema 返回，本次结果不可结构化消费。")
        return 2

    print(render(report))
    print(f"\n[session={thread}] {stats.as_line()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))