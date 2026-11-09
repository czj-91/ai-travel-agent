"""离线单元测试：不联网、不调模型，只验证纯逻辑与工具行为。

两种跑法：
    python tests/test_offline.py     # 无需 pytest
    pytest tests/                    # 装了 pytest 时
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel.config import MissingEnvError, load_settings, require_env  # noqa: E402
from travel.eval import evaluate_report  # noqa: E402
from travel.observability import RunStats, usage_from_messages  # noqa: E402
from travel.schemas import DayPlan, Spot, TripPlan  # noqa: E402
from travel.tools import format_search_results, search_with_retry  # noqa: E402

SAMPLE = {
    "query": "杭州 3天 人文 攻略",
    "results": [
        {"title": "杭州博物馆参观指南", "url": "https://example.com/a", "content": "周一闭馆 " * 80},
        {"title": "西湖步行路线", "url": "https://example.com/b", "content": "断桥到雷峰塔。"},
        {"title": "", "url": "", "content": "脏数据，应被忽略"},
    ],
    "images": [{"url": "https://example.com/x.png"}] * 20,
}


def _report(**over):
    base = dict(
        destination="杭州",
        days=2,
        traveler_profile="2 人，喜欢人文，不吃辣",
        plan=[
            DayPlan(
                day=1,
                theme="西湖人文",
                base_area="西湖景区",
                spots=[
                    Spot(name="浙江省博物馆", why="馆藏丰富", minutes=120, ticket_yuan=0),
                    Spot(name="雷峰塔", why="看全景", minutes=60, ticket_yuan=40),
                ],
                meals=["楼外楼西湖醋鱼"],
                transport="地铁+步行",
                day_cost_yuan=90,
            ),
            DayPlan(
                day=2,
                theme="运河慢游",
                base_area="拱宸桥",
                spots=[
                    Spot(name="桥西直街", why="免费老街", minutes=90, ticket_yuan=0),
                    Spot(name="刀剪剑博物馆", why="亲子友好", minutes=60, ticket_yuan=0),
                ],
                transport="公交",
                day_cost_yuan=30,
            ),
        ],
        budget_total_yuan=120,
        risks=["浙江省博物馆需提前预约"],
        references=["https://example.com/a"],
        summary="两天足够，节奏偏松。",
    )
    base.update(over)
    return TripPlan(**base)


def test_format_results_drops_heavy_fields():
    out = format_search_results(SAMPLE)
    assert "images" not in out and "脏数据" not in out
    assert "https://example.com/a" in out and "https://example.com/b" in out
    assert len(out) < 1200, f"裁剪后仍然过长：{len(out)}"


def test_format_respects_limit_and_edge_cases():
    assert len(format_search_results(SAMPLE, limit=1).splitlines()) == 2
    assert format_search_results({"results": []}) == "没有找到相关结果。"
    assert format_search_results(None) == "没有找到相关结果。"
    assert format_search_results("纯字符串") == "纯字符串"


class _BoomClient:
    def __init__(self):
        self.calls = 0

    def invoke(self, query):
        self.calls += 1
        raise RuntimeError("network down")


class _OkClient:
    def invoke(self, query):
        return SAMPLE


def test_search_retry_stops_and_degrades():
    client = _BoomClient()
    text, stats = search_with_retry(client, "q", retries=1, timeout=0.0)
    assert client.calls == 1, "超过总时长上限后不该继续重试"
    assert stats["ok"] is False and "搜索失败" in text and "references 留空" in text


def test_search_success_reports_hits():
    text, stats = search_with_retry(_OkClient(), "q", retries=2, timeout=5)
    assert stats["ok"] and stats["attempts"] == 1 and stats["hits"] == 2


def test_evaluate_report_accepts_good_plan():
    assert evaluate_report(_report()) == []


def test_evaluate_report_catches_real_defects():
    problems = evaluate_report(
        _report(
            plan=[
                DayPlan(
                    day=2,
                    theme="赶场",
                    base_area="",
                    spots=[
                        Spot(name="A", why="a", minutes=300, ticket_yuan=0),
                        Spot(name="B", why="b", minutes=300, ticket_yuan=0),
                        Spot(name="B", why="b", minutes=60, ticket_yuan=0),
                    ],
                    transport="打车",
                    day_cost_yuan=500,
                )
            ],
            budget_total_yuan=1,
            references=["ftp://bad"],
            summary="",
        ),
        expect={"require_sources": True, "destination": "杭州"},
    )
    assert any("天数" in p for p in problems)
    assert any("base_area" in p for p in problems)
    assert any("超过" in p for p in problems)
    assert any("重复" in p for p in problems)
    assert any("自相矛盾" in p for p in problems)
    assert any("非法来源" in p for p in problems)
    assert any("summary 为空" in p for p in problems)


def test_evaluate_report_checks_days_and_budget():
    problems = evaluate_report(_report(), expect={"days": 5, "max_budget": 100})
    assert any("用户要求 5 天" in p for p in problems)
    assert any("超过上限" in p for p in problems)
    assert evaluate_report(_report(), expect={"days": 2, "max_budget": 200}) == []


def test_usage_and_cost():
    class Msg:
        usage_metadata = {"input_tokens": 1200, "output_tokens": 300}

    stats = usage_from_messages({"messages": [Msg(), Msg()]})
    assert (stats.input_tokens, stats.output_tokens, stats.total_tokens) == (2400, 600, 3000)
    assert stats.cost_yuan(12, 24) == 0.0432  # 2400/1M*12 + 600/1M*24
    assert RunStats(elapsed_ms=10, input_tokens=1).total_tokens == 1


def test_require_env_and_model_gating():
    saved = dict(os.environ)
    try:
        try:
            require_env("DEFINITELY_MISSING_KEY", "示例地址")
        except MissingEnvError as exc:
            assert "DEFINITELY_MISSING_KEY" in str(exc) and ".env" in str(exc)
        else:
            raise AssertionError("缺 key 时应当抛 MissingEnvError")

        os.environ.update({"DEEPSEEK_API_KEY": "sk-test", "TAVILY_API_KEY": "tvly-test"})
        os.environ.pop("TRAVEL_MODEL", None)
        assert load_settings().travel_model == "deepseek-chat"

        # qwen 模式下必须强制校验 base_url，否则 SDK 会静默回退到 api.openai.com
        os.environ.update({"TRAVEL_MODEL": "qwen3.5-plus", "DASHSCOPE_API_KEY": "sk-d"})
        os.environ.pop("DASHSCOPE_BASE_URL", None)
        try:
            load_settings()
        except MissingEnvError as exc:
            assert "DASHSCOPE_BASE_URL" in str(exc)
        else:
            raise AssertionError("缺 DASHSCOPE_BASE_URL 时应当报错")
    finally:
        os.environ.clear()
        os.environ.update(saved)


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)