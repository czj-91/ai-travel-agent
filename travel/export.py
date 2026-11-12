r"""行程导出：把 TripPlan 变成能带走的东西（终端文本 / Markdown / ics 日历）。

只用标准库，不联网、不调模型，因此全部可以离线单测。ics 走 RFC 5545：
每行 ≤75 字节（中文按字节折行，不能切断字符）、文本值里的 , ; \ 与换行必须转义。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

from .schemas import Spot, TripPlan

DAY_START = (9, 0)  # 默认每天 09:00 开始排
LUNCH_AT_HOUR = 12
LUNCH_MINUTES = 60
EXPORT_DIR = Path(__file__).resolve().parent.parent / "outputs"
FORMATS = ("txt", "markdown", "ics")
FILE_SUFFIX = {"txt": ".txt", "markdown": ".md", "ics": ".ics"}


def resolve_start_date(start_date: str | date | None, today: date | None = None) -> tuple[date, bool]:
    """没给起始日期就按「明天出发」猜一个，同时告诉调用方这是猜的，别假装知道。"""
    base = today or date.today()
    if isinstance(start_date, date):
        return start_date, False
    text = (start_date or "").strip()
    if text:
        try:
            return date.fromisoformat(text), False
        except ValueError as exc:
            raise ValueError(f"起始日期格式不对：{text}，应为 YYYY-MM-DD") from exc
    return base + timedelta(days=1), True


def day_schedule(
    spots: list[Spot], start: datetime, lunch_at_hour: int = LUNCH_AT_HOUR, lunch_minutes: int = LUNCH_MINUTES
):
    """按停留时长顺排时段，到点插一次午休。纯函数，输出 (开始, 结束, spot) 列表。"""
    clock = start
    lunch_taken = False
    slots = []
    for spot in spots:
        if not lunch_taken and clock.hour >= lunch_at_hour:
            clock += timedelta(minutes=lunch_minutes)
            lunch_taken = True
        end = clock + timedelta(minutes=spot.minutes)
        slots.append((clock, end, spot))
        clock = end
    return slots


def _start_of_day(start_date: date, day_number: int) -> datetime:
    return datetime(start_date.year, start_date.month, start_date.day, *DAY_START) + timedelta(
        days=max(day_number, 1) - 1
    )


def _ticket(spot: Spot) -> str:
    return "免费" if not spot.ticket_yuan else f"{spot.ticket_yuan:.0f} 元"


def _day_minutes(day) -> int:
    return sum(spot.minutes for spot in day.spots)


def _footer_lines(report: TripPlan) -> list[str]:
    lines: list[str] = []
    if report.risks:
        lines.append("需要注意：")
        lines += [f"   - {risk}" for risk in report.risks]
        lines.append("")
    if report.references:
        lines.append("参考来源：")
        lines += [f"   - {ref}" for ref in report.references]
        lines.append("")
    lines.append(f"总结：{report.summary}")
    return lines


def to_text(report: TripPlan, start_date: str | date | None = None) -> str:
    """终端友好版：CLI 直接打印用，缩进而非 Markdown 语法。"""
    when, _ = resolve_start_date(start_date)
    lines = [
        f"目的地：{report.destination} | {report.days} 天 | 人均预算 {report.budget_total_yuan:.0f} 元（不含住宿与往返大交通）",
        f"需求复述：{report.traveler_profile}",
    ]
    if report.best_season_note:
        lines.append(f"出行提示：{report.best_season_note}")
    lines.append("")

    for day in report.plan:
        head = f"第 {day.day} 天 · {day.theme}（{day.base_area}，约 {_day_minutes(day) / 60:.1f} 小时，{day.day_cost_yuan:.0f} 元/人）"
        if start_date is not None:
            dated = (when + timedelta(days=day.day - 1)).strftime("%m-%d")
            head = f"第 {day.day} 天 {dated} · {day.theme}（{day.base_area}，约 {_day_minutes(day) / 60:.1f} 小时，{day.day_cost_yuan:.0f} 元/人）"
        lines.append(head)
        for index, (begin, end, spot) in enumerate(day_schedule(day.spots, _start_of_day(when, day.day)), 1):
            lines.append(
                f"   {index}. {begin:%H:%M}–{end:%H:%M} {spot.name}（{spot.minutes} 分钟 / {_ticket(spot)}）—— {spot.why}"
            )
        if day.meals:
            lines.append(f"   吃：{'；'.join(day.meals)}")
        lines.append(f"   行：{day.transport}")
        lines.append("")

    lines += _footer_lines(report)
    return "\n".join(lines)


def _cell(text: str) -> str:
    """Markdown 表格单元格：竖线会截断列，换行同理。"""
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def to_markdown(report: TripPlan, start_date: str | date | None = None) -> str:
    """分享/打印版：按天时序表 + 花费 + 风险 + 来源。"""
    when, _ = resolve_start_date(start_date)
    lines = [
        f"# {report.destination} {report.days} 天行程（{when:%Y-%m-%d} 起）",
        "",
        f"- 需求复述：{report.traveler_profile}",
        f"- 人均花费：{report.budget_total_yuan:.0f} 元（不含住宿与往返大交通）",
    ]
    if report.best_season_note:
        lines.append(f"- 出行提示：{report.best_season_note}")
    lines += ["", f"> {report.summary}", ""]

    for day in report.plan:
        date_label = (when + timedelta(days=day.day - 1)).strftime("%Y-%m-%d")
        lines.append(
            f"## 第 {day.day} 天 · {day.theme}｜{date_label}｜{day.base_area}｜约 {_day_minutes(day) / 60:.1f} 小时｜{day.day_cost_yuan:.0f} 元/人"
        )
        lines += ["", "| 时间 | 游玩点 | 时长 | 门票 | 为什么去 |", "| --- | --- | --- | --- | --- |"]
        for begin, end, spot in day_schedule(day.spots, _start_of_day(when, day.day)):
            lines.append(
                f"| {begin:%H:%M}–{end:%H:%M} | {_cell(spot.name)} | {spot.minutes} 分钟 | {_ticket(spot)} | {_cell(spot.why)} |"
            )
        if day.meals:
            lines.append(f"| — | 吃 | — | — | {_cell('；'.join(day.meals))} |")
        lines += ["", f"通勤：{day.transport}", ""]

    if report.risks:
        lines += ["## 需要注意", ""] + [f"- {risk}" for risk in report.risks] + [""]
    if report.references:
        lines += ["## 参考来源", ""] + [f"- {ref}" for ref in report.references] + [""]
    return "\n".join(lines)


def _escape_ics(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 折行：首行 75 字节，续行 1 个空格 + 74 字节；按字符累计字节，不切断中文。"""
    chunks: list[str] = []
    current, used, budget = "", 0, 75
    for char in line:
        size = len(char.encode("utf-8"))
        if used + size > budget:
            chunks.append(current)
            current, used, budget = "", 0, 74
        current += char
        used += size
    chunks.append(current)
    return "\r\n ".join(chunks)


def _utc_stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%SZ")


def to_ics(report: TripPlan, start_date: str | date | None = None, stamp: str | None = None) -> str:
    """日历版：每个游玩点一个事件（浮动本地时间，不带 TZID，导入手机/桌面日历按本地时区解析）。"""
    when, _ = resolve_start_date(start_date)
    name = f"{report.destination} {report.days} 天行程"
    body: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ai-travel//TripPlan Export//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold(f"X-WR-CALNAME:{_escape_ics(name)}"),
    ]
    events = 0
    for day in report.plan:
        for index, (begin, end, spot) in enumerate(day_schedule(day.spots, _start_of_day(when, day.day)), 1):
            digest = hashlib.sha1(f"{report.destination}|{day.day}|{index}|{spot.name}".encode()).hexdigest()[
                :16
            ]
            description = (
                f"{spot.why}\n第 {day.day} 天 · {day.theme}｜{day.base_area}\n"
                f"门票：{_ticket(spot)}｜当天人均 {day.day_cost_yuan:.0f} 元\n通勤：{day.transport}"
            )
            body += [
                "BEGIN:VEVENT",
                f"UID:{digest}@ai-travel",
                f"DTSTAMP:{stamp or _utc_stamp()}",
                f"DTSTART:{begin:%Y%m%dT%H%M%S}",
                f"DTEND:{end:%Y%m%dT%H%M%S}",
                _fold(f"SUMMARY:{_escape_ics(f'D{day.day} {spot.name}')}"),
                _fold(f"LOCATION:{_escape_ics(f'{report.destination} · {day.base_area}')}"),
                _fold(f"DESCRIPTION:{_escape_ics(description)}"),
                _fold(f"CATEGORIES:{_escape_ics(day.theme)}"),
                "STATUS:CONFIRMED",
                "TRANSP:OPAQUE",
                "END:VEVENT",
            ]
            events += 1
    body.append("END:VCALENDAR")
    if not events:
        raise ValueError("行程里没有任何游玩点，无法生成日历事件。")
    return "\r\n".join(body) + "\r\n"


def render(report: TripPlan, fmt: str, start_date: str | date | None = None) -> str:
    if fmt == "markdown":
        return to_markdown(report, start_date)
    if fmt == "ics":
        return to_ics(report, start_date)
    return to_text(report, start_date)


def safe_stem(report: TripPlan, when: date) -> str:
    """文件名去掉路径非法字符：目的地常被模型写成「杭州·西湖」甚至带斜杠。"""
    raw = f"行程-{report.destination}-{when:%Y%m%d}"
    return "".join("_" if char in '\\/:*?"<>|' else char for char in raw)


def write_exports(
    report: TripPlan,
    formats: tuple[str, ...] = ("markdown", "ics"),
    start_date: str | date | None = None,
    out_dir: Path | None = None,
) -> tuple[list[Path], date, bool]:
    """落盘到 outputs/，返回 (文件列表, 实际起始日, 起始日是不是猜的)。"""
    when, guessed = resolve_start_date(start_date)
    target = Path(out_dir or EXPORT_DIR)
    target.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for fmt in formats:
        if fmt not in FORMATS:
            raise ValueError(f"未知导出格式 {fmt}，可选 {', '.join(FORMATS)}")
        path = target / f"{safe_stem(report, when)}{FILE_SUFFIX[fmt]}"
        path.write_text(render(report, fmt, when), encoding="utf-8", newline="")
        paths.append(path)
    return paths, when, guessed
