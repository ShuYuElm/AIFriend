"""Web API：页面、聊天、历史、状态、SSE 总线。"""

import asyncio

from fastapi.testclient import TestClient

from app.adapters.web import EventBus
from app.config import ROOT, Settings
from app.main import create_app


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_base_url="http://localhost",
        llm_api_key="",
        llm_model="mock",
        tz="Asia/Shanghai",
        db_path=tmp_path / "web.db",
        persona_path=ROOT / "config" / "persona.yaml",
        life_path=ROOT / "config" / "life.yaml",
        profile_path=ROOT / "config" / "user_profile.yaml",
        seed_path=ROOT / "config" / "seed_memories.yaml",
        world_path=tmp_path / "no-world.yaml",
    )


def test_index_and_static(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "phone" in page.text

        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200
        assert client.get("/static/../app/main.py").status_code == 404


def test_chat_then_history(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        res = client.post("/api/chat", json={"text": "在吗"}).json()
        assert res["ok"] is True
        assert 1 <= len(res["bubbles"]) <= 3
        assert all("delay_ms" in b and b["delay_ms"] > 0 for b in res["bubbles"])

        history = client.get("/api/history?limit=10").json()
        roles = [m["role"] for m in history["messages"]]
        assert "user" in roles and "assistant" in roles


def test_empty_message_rejected(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.post("/api/chat", json={"text": "   "}).json()["ok"] is False


def test_state_and_health(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        state = client.get("/api/state").json()
        assert state["name"]
        assert isinstance(state["memories"], list)
        assert isinstance(state["experiences"], list)

        health = client.get("/api/health").json()
        assert health["ok"] is True
        assert health["mock"] is True
        assert health["model"] == "mock"


def test_event_bus_delivers():
    bus = EventBus()
    queue = bus.subscribe()

    async def run():
        await bus.publish("bubble", {"text": "hi"})
        return await asyncio.wait_for(queue.get(), timeout=1)

    event, data = asyncio.run(run())
    assert event == "bubble"
    assert data["text"] == "hi"
    assert "event: bubble" in EventBus.format("bubble", {"a": 1})
    bus.unsubscribe(queue)
    assert bus.subscriber_count == 0
