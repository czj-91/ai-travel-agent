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


def test_rag_keyword_recall_offline():
    from travel.rag import PoiIndex

    idx = PoiIndex(use_embeddings=False)
    assert idx.embed_backend == "keyword"
    hits = idx.search("适合老人慢走 有电瓶车", top_k=3)
    assert hits and hits[0]["row"]["name"] == "颐和园"
    city_hits = idx.search("白色建筑 海景", city="大理", top_k=2)
    assert all(h["row"]["city"] == "大理" for h in city_hits)
    assert city_hits[0]["row"]["name"].startswith("理想邦")


def test_rag_ttl_marks_stale_rows():
    from travel.rag import days_since

    assert days_since("2025-11-03", today="2026-11-09") == 371
    assert days_since("2026-10-12", today="2026-11-09") == 28
    assert days_since("坏日期", today="2026-11-09") > 9_000

    from travel.rag import PoiIndex

    idx = PoiIndex(use_embeddings=False, ttl_days=180)
    stale = [h for h in idx.search("西泠印社", top_k=3) if h["stale"]]
    assert stale, "2025-11 核实的条目应被标为过期"
    text = idx.format_hits(stale)
    assert "可能过期" in text and "web_search 复核" in text


def test_rag_tool_returns_text_or_no_match():
    from travel.rag import PoiIndex, build_poi_tool

    tool_fn = build_poi_tool(PoiIndex(use_embeddings=False))
    out = tool_fn.invoke({"query": "兵马俑 预约", "city": "西安"})
    assert "120" in out and "预约" in out
    empty = tool_fn.invoke({"query": "火星基地", "city": "杭州"})
    assert "没有匹配结果" in empty

def test_rag_embedding_rejects_off_topic():
    """语义召回必须拦住跑题查询，否则模型会误以为已查到、不再调用 web_search。"""
    from travel.rag import PoiIndex, build_poi_tool

    tool_fn = build_poi_tool(PoiIndex(use_embeddings=True))
    for q in ("上海迪士尼门票", "火星基地", "东京晴空塔"):
        assert "没有匹配结果" in tool_fn.invoke({"query": q, "city": ""}), f"跑题查询未拦住: {q}"
    assert "120" in tool_fn.invoke({"query": "兵马俑 预约", "city": "西安"})


def test_rag_keyword_fallback_keeps_top1_relevant():
    """降级路径只保证 top1 相关，不保证拦跑题（词面匹配的固有局限）。"""
    from travel.rag import PoiIndex, build_poi_tool

    tool_fn = build_poi_tool(PoiIndex(use_embeddings=False))
    assert tool_fn.name == "poi_lookup"
    assert "颐和园" in tool_fn.invoke({"query": "适合老人慢走 有电瓶车", "city": ""})
    assert "120" in tool_fn.invoke({"query": "兵马俑 预约", "city": "西安"})
    assert "没有匹配结果" in tool_fn.invoke({"query": "上海迪士尼门票", "city": ""})

def test_multimodal_encode_and_errors(tmp_dir: str | None = None):
    import base64
    import tempfile
    from pathlib import Path

    from travel.multimodal import ImageInputError, build_content, encode_image

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )
    with tempfile.TemporaryDirectory() as work:
        good = Path(work) / "a.png"
        good.write_bytes(png)
        block = encode_image(good)
        assert block["type"] == "image" and block["mime_type"] == "image/png"
        assert base64.b64decode(block["base64"]) == png

        content = build_content("这个景点值得去吗", good)
        assert isinstance(content, list) and content[1] == {"type": "text", "text": "这个景点值得去吗"}
        assert build_content("纯文本") == "纯文本"

        empty = Path(work) / "e.png"
        empty.write_bytes(b"")
        try:
            encode_image(empty)
        except ImageInputError as exc:
            assert "空文件" in str(exc)
        else:
            raise AssertionError("空文件应被拒绝")

        try:
            encode_image(Path(work) / "not-here.png")
        except ImageInputError as exc:
            assert "不存在" in str(exc)
        else:
            raise AssertionError("不存在的文件应被拒绝")

        bad_ext = Path(work) / "a.txt"
        bad_ext.write_text("hello")
        try:
            encode_image(bad_ext)
        except ImageInputError as exc:
            assert "不支持的图片格式" in str(exc)
        else:
            raise AssertionError("非图片格式应被拒绝")


def test_plan_trip_rejects_image_on_text_model():
    from travel.graph import plan_trip

    class _Never:
        def invoke(self, *a, **k):
            raise AssertionError("不该被调用")

    from travel.config import Settings

    text_only = Settings(
        travel_model="deepseek-chat", deepseek_api_key="sk-x", dashscope_api_key=None,
        dashscope_base_url=None, tavily_api_key="tvly-x", langsmith_project="t",
        db_path="x", search_timeout=1, search_retries=0,
    )
    try:
        plan_trip(_Never(), "看图规划", image_path="whatever.png", settings=text_only)
    except ValueError as exc:
        assert "TRAVEL_MODEL" in str(exc)
    else:
        raise AssertionError("文本模型带图应直接拒绝")

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