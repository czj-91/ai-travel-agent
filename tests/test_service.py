"""服务层测试：用 TestClient 直接打接口，不需要真的起 uvicorn。

    python tests/test_service.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

import service  # noqa: E402
from travel.schemas import DayPlan, Spot, TripPlan  # noqa: E402


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


def test_health_ok_with_fake_env():
    service._graph = _FakeGraph()
    os.environ.update({"DEEPSEEK_API_KEY": "sk-test", "TAVILY_API_KEY": "tvly-test"})
    os.environ.pop("TRAVEL_MODEL", None)
    body = client.get("/health").json()
    assert body["status"] == "ok", body
    assert body["model"] == "deepseek-chat"


def test_health_reports_missing_config():
    saved = os.environ.pop("DEEPSEEK_API_KEY")
    try:
        body = client.get("/health").json()
        assert body["status"] == "config_missing"
        assert "DEEPSEEK_API_KEY" in body["detail"]
    finally:
        os.environ["DEEPSEEK_API_KEY"] = saved


def test_plan_returns_structured_json():
    service._graph = _FakeGraph()
    resp = client.post("/plan", json={"request": "杭州 1天 人文", "session_id": "t-1"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["session_id"] == "t-1"
    assert data["input_tokens"] == 100 and data["output_tokens"] == 40
    assert data["plan"]["destination"] == "杭州"
    assert len(data["plan"]["plan"]) == 1
    assert data["plan"]["plan"][0]["spots"][0]["name"] == "浙江省博物馆"


def test_plan_rejects_too_short_request():
    service._graph = _FakeGraph()
    assert client.post("/plan", json={"request": "x"}).status_code == 422


def test_plan_502_when_model_ignores_schema():
    service._graph = _FakeGraph(structured=None)
    resp = client.post("/plan", json={"request": "杭州 1天"})
    assert resp.status_code == 502
    assert "schema" in resp.json()["detail"]


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