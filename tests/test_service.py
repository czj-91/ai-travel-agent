"""服务层测试：用 TestClient 直接打接口，不需要真的起 uvicorn。

python tests/test_service.py
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

import service
from travel.schemas import DayPlan, Spot, TripPlan


def _sample_plan() -> TripPlan:
    return TripPlan(
        destination="杭州",
        days=1,
        traveler_profile="1 人，喜欢人文",
        plan=[
            DayPlan(
                day=1,
                theme="西湖人文",
                base_area="孤山—北山街",
                spots=[
                    Spot(name="浙江省博物馆", why="馆藏", minutes=120, ticket_yuan=0),
                    Spot(name="西泠印社", why="金石", minutes=60, ticket_yuan=0),
                ],
                transport="地铁+步行",
                day_cost_yuan=60,
            )
        ],
        budget_total_yuan=60,
        risks=["周一闭馆"],
        references=["https://example.com/a"],
        summary="一天足够。",
    )


class _FakeGraph:
    def __init__(self, structured=_sample_plan()):
        self._structured = structured

    def invoke(self, payload, config):
        return {
            "messages": [{"usage_metadata": {"input_tokens": 100, "output_tokens": 40}}],
            "structured_response": self._structured,
        }


client = TestClient(service.app)


# graph 与 settings 必须成对替换：plan_trip 会读 settings.travel_model 判断能不能收图
def _use_fake_agent(structured=_sample_plan(), model="deepseek-chat"):
    service._graph = _FakeGraph(structured)
    service._settings = SimpleNamespace(travel_model=model)
    # 限流是进程内状态，测试之间必须清零，否则第 11 次请求会被 429 挡掉
    service.reset_rate_limiter()


PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="


def test_health_ok_with_fake_env():
    saved = dict(os.environ)
    try:
        service._graph = _FakeGraph()
        os.environ.update({"DEEPSEEK_API_KEY": "sk-test", "TAVILY_API_KEY": "tvly-test"})
        os.environ.pop("TRAVEL_MODEL", None)
        body = client.get("/health").json()
        assert body["status"] == "ok", body
        assert body["model"] == "deepseek-chat"
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert body["status"] == "ok", body
    assert body["model"] == "deepseek-chat"


def test_health_reports_missing_config():
    saved = os.environ.pop("DEEPSEEK_API_KEY", None)  # CI 上根本没有 .env，pop 必须给默认值
    try:
        body = client.get("/health").json()
        assert body["status"] == "config_missing"
        assert "DEEPSEEK_API_KEY" in body["detail"]
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved


def test_plan_returns_structured_json():
    _use_fake_agent()
    resp = client.post("/plan", json={"request": "杭州 1天 人文", "session_id": "t-1"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["session_id"] == "t-1"
    assert data["input_tokens"] == 100 and data["output_tokens"] == 40
    assert data["plan"]["destination"] == "杭州"
    assert len(data["plan"]["plan"]) == 1
    assert data["plan"]["plan"][0]["spots"][0]["name"] == "浙江省博物馆"


def test_plan_rejects_too_short_request():
    _use_fake_agent()
    assert client.post("/plan", json={"request": "x"}).status_code == 422


def test_plan_502_when_model_ignores_schema():
    _use_fake_agent(structured=None)
    resp = client.post("/plan", json={"request": "杭州 1天"})
    assert resp.status_code == 502
    assert "schema" in resp.json()["detail"]


def test_plan_requires_token_when_configured():
    saved = os.environ.get("SERVICE_TOKEN")
    os.environ["SERVICE_TOKEN"] = "secret-abc"
    try:
        _use_fake_agent()
        assert client.post("/plan", json={"request": "杭州 1天"}).status_code == 401
        ok = client.post("/plan", json={"request": "杭州 1天"}, headers={"X-Service-Token": "secret-abc"})
        assert ok.status_code == 200, ok.text
    finally:
        os.environ.pop("SERVICE_TOKEN", None)
        if saved:
            os.environ["SERVICE_TOKEN"] = saved


def test_export_builds_files_without_calling_the_model():
    resp = client.post("/export", json={"plan": _sample_plan().model_dump(), "start_date": "2026-12-01"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_date"] == "2026-12-01" and body["start_date_guessed"] is False
    assert {item["format"] for item in body["files"]} == {"markdown", "ics"}
    markdown = next(item["content"] for item in body["files"] if item["format"] == "markdown")
    assert "# 杭州 1 天行程（2026-12-01 起）" in markdown and "| 09:00–11:00 |" in markdown


def test_export_flags_a_guessed_start_date():
    resp = client.post("/export", json={"plan": _sample_plan().model_dump(), "formats": ["ics"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_date_guessed"] is True
    assert body["files"][0]["name"].startswith("行程-杭州-")


def test_export_rejects_bad_payloads():
    assert client.post("/export", json={"plan": {"foo": 1}}).status_code == 422
    assert (
        client.post("/export", json={"plan": _sample_plan().model_dump(), "formats": ["pdf"]}).status_code
        == 422
    )
    assert (
        client.post("/export", json={"plan": _sample_plan().model_dump(), "formats": []}).status_code == 422
    )
    assert (
        client.post("/export", json={"plan": _sample_plan().model_dump(), "start_date": "下周"}).status_code
        == 422
    )


def test_index_page_is_served():
    resp = client.get("/")
    assert resp.status_code == 200, resp.text
    assert "旅行规划" in resp.text and 'id="request"' in resp.text
    # 网页版必须零 CDN：样式与脚本都得从本地 /static 拿
    assert "/static/app.css" in resp.text and "/static/app.js" in resp.text
    import re

    assert not re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', resp.text), (
        "引入了外部 CDN，断网就打不开"
    )
    for asset in ("/static/app.css", "/static/app.js"):
        got = client.get(asset)
        assert got.status_code == 200 and len(got.text) > 400, asset
    assert client.get("/static/none.css").status_code == 404


def test_plan_rejects_blank_request_but_strips_the_useful_one():
    captured = {}

    class _Recorder(_FakeGraph):
        def invoke(self, payload, config):
            captured["text"] = payload["messages"][0][1]
            return super().invoke(payload, config)

    _use_fake_agent()
    service._graph = _Recorder()
    assert client.post("/plan", json={"request": "   "}).status_code == 422, (
        "两个空格曾能绕过 min_length 直接进模型"
    )
    resp = client.post("/plan", json={"request": "  杭州 1天  "})
    assert resp.status_code == 200, resp.text
    assert captured["text"] == "杭州 1天", "首尾空白不该原样送进模型"


def test_plan_passes_uploaded_image_as_content_block():
    captured = {}

    class _Recorder(_FakeGraph):
        def invoke(self, payload, config):
            captured["content"] = payload["messages"][0][1]
            return super().invoke(payload, config)

    _use_fake_agent(model="qwen3.5-plus")
    service._graph = _Recorder()
    resp = client.post("/plan", json={"request": "这个景点值得去吗", "image": PNG_DATA_URL})
    assert resp.status_code == 200, resp.text
    content = captured["content"]
    assert isinstance(content, list) and len(content) == 2, "图文要组成 content block 列表"
    assert content[0]["type"] == "image" and content[0]["mime_type"] == "image/png"
    assert content[1] == {"type": "text", "text": "这个景点值得去吗"}


def test_plan_rejects_image_on_text_model_with_readable_error():
    _use_fake_agent(model="deepseek-chat")
    resp = client.post("/plan", json={"request": "这个景点值得去吗", "image": PNG_DATA_URL})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "不支持图片输入" in detail and "qwen" in detail, detail


def test_plan_rejects_unusable_image_payloads():
    _use_fake_agent(model="qwen3.5-plus")
    assert (
        client.post(
            "/plan", json={"request": "看图", "image": "data:image/png;base64,@@not base64@@"}
        ).status_code
        == 422
    )
    too_small = client.post("/plan", json={"request": "看图", "image": "data:text/plain;base64,aGVsbG8="})
    assert too_small.status_code == 422 and "不支持的图片格式" in too_small.json()["detail"], too_small.text


class _FakeState:
    def __init__(self, messages):
        self.values = {"messages": messages, "structured_response": _sample_plan()}


class _FakeStreamGraph:
    """只实现 stream_plan 用到的三个方法，不碰真实模型。"""

    def __init__(self):
        self.calls = 0

    def get_state(self, config):
        return _FakeState([])

    def stream(self, payload, config, stream_mode=None):
        from langchain_core.messages import AIMessageChunk, ToolMessage

        self.calls += 1
        yield (
            AIMessageChunk(
                content="",
                tool_call_chunks=[{"name": "poi_lookup", "args": "{}", "id": "c1", "index": 0}],
            ),
            {},
        )
        yield AIMessageChunk(content="正在安排第二天"), {}
        yield ToolMessage(content="本地知识库命中 3 条", tool_call_id="c1", name="poi_lookup"), {}

    def invoke(self, payload, config):
        from types import SimpleNamespace

        return {
            "messages": [
                SimpleNamespace(usage_metadata={"input_tokens": 500, "output_tokens": 90}, content="x")
            ],
            "structured_response": _sample_plan(),
        }


def test_rate_limit_returns_429_with_retry_after():
    from travel.security import RateLimiter

    _use_fake_agent()
    saved = service._limiter
    service._limiter = RateLimiter(limit=2, window_seconds=60)
    try:
        assert client.post("/plan", json={"request": "杭州 1天"}).status_code == 200
        assert client.post("/plan", json={"request": "杭州 1天"}).status_code == 200
        third = client.post("/plan", json={"request": "杭州 1天"})
        assert third.status_code == 429, third.text
        assert "Retry-After" in third.headers
        assert "每分钟上限 2" in third.json()["detail"]
    finally:
        service._limiter = saved


def test_plan_stream_emits_sse_events():
    service._graph = _FakeStreamGraph()
    service._settings = SimpleNamespace(travel_model="deepseek-chat")
    service.reset_rate_limiter()

    resp = client.post("/plan/stream", json={"request": "杭州 1天 人文"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    for fragment in ('"type": "start"', '"type": "tool_call"', '"type": "token"', '"type": "plan"', "[DONE]"):
        assert fragment in body, f"缺少事件 {fragment}"
    assert resp.headers.get("cache-control") == "no-cache"


def test_plan_stream_rejects_blank_request():
    service._graph = _FakeStreamGraph()
    service._settings = SimpleNamespace(travel_model="deepseek-chat")
    service.reset_rate_limiter()
    assert client.post("/plan/stream", json={"request": "   "}).status_code == 422


def test_cors_blocks_unlisted_origin():
    _use_fake_agent()
    bad = client.options(
        "/plan", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in {k.lower() for k in bad.headers}


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:160]}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
