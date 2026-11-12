"""命令行入口：说出出行需求，拿回一份结构化行程方案，顺手导出能带走的文件。

用法：
    python main.py 杭州 3天 2人 喜欢人文 不吃辣 预算人均1500
    python main.py --session trip1 杭州 3天        # 同一 session 延续记忆
    python main.py --image 截图.png 这个景点值得去吗  # 需 TRAVEL_MODEL=qwen3.5-plus
    python main.py --export md,ics --start 2026-12-01 大理 4天 自驾
"""

from __future__ import annotations

import sys

from travel.export import FORMATS, render, write_exports
from travel.graph import build_agent, plan_trip

OPTIONS = {
    "--session": "session",
    "--image": "image",
    "--export": "export",
    "--start": "start",
    "--out": "out",
}
FORMAT_ALIASES = {
    "md": "markdown",
    "markdown": "markdown",
    "ics": "ics",
    "calendar": "ics",
    "txt": "txt",
    "text": "txt",
}


def parse_args(argv: list[str]) -> tuple[dict, str]:
    """只认已知选项；写错的选项必须报错，不能被当成「出行需求」发给模型。"""
    flags: dict[str, str] = {}
    words: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in OPTIONS:
            if index + 1 >= len(argv):
                raise ValueError(f"{token} 后面缺参数")
            flags[OPTIONS[token]] = argv[index + 1]
            index += 2
        elif token.startswith("-"):
            raise ValueError(f"未知选项 {token}，可用：{', '.join(OPTIONS)}")
        else:
            words.append(token)
            index += 1
    return flags, " ".join(words).strip()


def parse_formats(raw: str) -> tuple[str, ...]:
    """--export md,ics -> ('markdown', 'ics')；别名收敛到 travel.export 的三种格式。"""
    names: list[str] = []
    for item in raw.split(","):
        key = item.strip().lower()
        if not key:
            continue
        if key not in FORMAT_ALIASES:
            raise ValueError(f"未知导出格式 {item}，可用：{', '.join(FORMAT_ALIASES)}")
        if FORMAT_ALIASES[key] not in names:
            names.append(FORMAT_ALIASES[key])
    if not names:
        raise ValueError(f"--export 至少给一种格式：{', '.join(FORMATS)}")
    return tuple(names)


def main(argv: list[str]) -> int:
    try:
        flags, request = parse_args(argv)
        formats = parse_formats(flags["export"]) if flags.get("export") else ()
    except ValueError as exc:
        print(f"参数有问题：{exc}")
        return 1

    request = request or input("这次想去哪、几天、几个人、有什么偏好？").strip()
    if not request:
        print("没有输入需求，退出。")
        return 1

    graph, settings = build_agent()
    print(
        f"模型={settings.travel_model} | 工具=poi_lookup / weather_forecast / web_search | LangSmith project={settings.langsmith_project}"
    )
    print(f"需求：{request}\n正在检索攻略并编排行程，请稍候…\n")

    try:
        report, stats, thread = plan_trip(graph, settings, request, flags.get("session"), flags.get("image"))
    except ValueError as exc:
        print(f"输入有问题：{exc}")
        return 1
    if report is None:
        print("模型没有按 schema 返回，本次结果不可结构化消费。")
        return 2

    print(render(report, "txt", flags.get("start")))
    print(f"\n[session={thread}] {stats.as_line()}")

    if formats:
        try:
            paths, when, guessed = write_exports(report, formats, flags.get("start"), flags.get("out"))
        except ValueError as exc:
            print(f"导出失败：{exc}")
            return 1
        print("\n已导出：")
        for path in paths:
            print(f"   {path}")
        if guessed:
            print(f"   起始日按 {when:%Y-%m-%d}（明天）排的；用 --start 2026-12-01 指定真实出发日更准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
