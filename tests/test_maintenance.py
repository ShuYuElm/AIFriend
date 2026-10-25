"""维护性功能：经历生命周期、启动补偿、备份、导出。"""

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.chat import ChatEngine
from app.config import ROOT, Settings
from app.db import Database
from app.experiences import ExperienceQueue
from app.life import load_life
from app.lorebook import Lorebook
from app.main import create_app
from app.memory import EXTRACT_MARKER
from app.persona import PersonaStore
from app.profile import ProfileStore

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 25, 15, 0, tzinfo=timezone(timedelta(hours=8)))


class FakeProvider:
    name = "fake"
    model = "fake"

    def __init__(self) -> None:
        self.last_messages: list[dict[str, str]] = []

    async def chat(self, messages, **kwargs):
        system = messages[0]["content"] if messages else ""
        if EXTRACT_MARKER in system:
            return "[]"
        self.last_messages = messages
        return "他这几天在赶论文，说周五要交，还聊到周末想出去走走。"


def _queue(tmp_path) -> tuple[ExperienceQueue, Database]:
    db = Database(tmp_path / "exp.db")
    life = load_life(ROOT / "config" / "life.yaml")
    return ExperienceQueue(db, life, TZ), db


# ------------------------------------------------------------------ 经历

async def test_expire_old_drops_stale_pending(tmp_path):
    queue, db = _queue(tmp_path)
    old = (NOW - timedelta(hours=30)).isoformat()
    fresh = (NOW - timedelta(hours=1)).isoformat()
    await db.add_experience("五天前看的剧", old, old, None)
    await db.add_experience("刚发生的事", fresh, fresh, None)

    assert await queue.expire_old(NOW) == 1
    assert [exp.content for exp in await queue.due(NOW)] == ["刚发生的事"]
    db.close()


async def test_backfill_fills_missing_days_only(tmp_path):
    queue, db = _queue(tmp_path)
    two_days_ago = (NOW - timedelta(days=2)).date().isoformat()
    await db.add_experience("已有的事", f"{two_days_ago}T10:00:00", f"{two_days_ago}T12:00:00", None)

    made = await queue.backfill_missed_days(NOW, days=4)
    assert made == 3  # 前 4 天里，2 天前已有，今天不补

    for offset in (1, 3, 4):
        day = (NOW - timedelta(days=offset)).date().isoformat()
        assert await db.experiences_for_day(day), f"{day} 没补上"
    assert len(await db.experiences_for_day(two_days_ago)) == 1  # 不重复
    db.close()


async def test_backfilled_experiences_are_expired_not_today(tmp_path):
    queue, db = _queue(tmp_path)
    await queue.backfill_missed_days(NOW, days=2)
    await queue.expire_old(NOW)

    assert await queue.due(NOW) == []  # 不会被当成"今天的事"
    recent = await queue.recent_unshared(NOW, days=5)
    assert len(recent) == 2
    assert recent[0].occurred_at > recent[1].occurred_at  # 新的在前
    db.close()


# ------------------------------------------------------------------ 启动补偿

def _engine(tmp_path, provider):
    settings = Settings(
        llm_base_url="http://localhost",
        llm_api_key="",
        llm_model="fake",
        tz=TZ,
        db_path=tmp_path / "chat.db",
        persona_path=ROOT / "config" / "persona.yaml",
        life_path=ROOT / "config" / "life.yaml",
        profile_path=ROOT / "config" / "user_profile.yaml",
        seed_path=ROOT / "config" / "seed_memories.yaml",
        world_path=tmp_path / "no-world.yaml",
    )
    db = Database(settings.db_path)
    store = PersonaStore(settings.persona_path)
    engine = ChatEngine(
        settings=settings,
        provider=provider,
        db=db,
        persona_store=store,
        profile_store=ProfileStore(settings.profile_path),
        lorebook=Lorebook.from_book(store.load().book),
        life=load_life(settings.life_path),
        clock=lambda: NOW,
    )
    return engine, db


async def test_startup_catches_up_missed_rollup(tmp_path):
    engine, db = _engine(tmp_path, FakeProvider())
    for index in range(6):
        await db.insert_message("user", f"第{index}句", "cli", f"2026-10-24T1{index}:00:00")
        await db.insert_message("assistant", f"回{index}", "cli", f"2026-10-24T1{index}:30:00")
    await db.set_state("msgs_since_rollup", 12)

    await engine.startup()

    assert len(await db.rollups(5)) == 1
    assert int(await db.get_state("msgs_since_rollup", 0)) == 0
    db.close()


# ------------------------------------------------------------------ 备份与导出

async def test_backup_writes_copy_and_prunes(tmp_path):
    db = Database(tmp_path / "b.db")
    await db.insert_message("user", "备份前的一句话", "cli", "2026-10-25T10:00:00")

    backups = tmp_path / "backups"
    backups.mkdir()
    for index in range(3):
        (backups / f"aigirl-2020010{index}-000000.db").write_bytes(b"old")

    saved = await db.backup(backups, keep=2)
    assert saved.exists()
    assert len(sorted(backups.glob("aigirl-*.db"))) == 2

    conn = sqlite3.connect(saved)
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert count == 1
    db.close()


def test_export_returns_markdown(tmp_path):
    settings = Settings(
        llm_base_url="http://localhost",
        llm_api_key="",
        llm_model="mock",
        tz=TZ,
        db_path=tmp_path / "web.db",
        persona_path=ROOT / "config" / "persona.yaml",
        life_path=ROOT / "config" / "life.yaml",
        profile_path=ROOT / "config" / "user_profile.yaml",
        seed_path=tmp_path / "no-seed.yaml",
        world_path=tmp_path / "no-world.yaml",
    )
    with TestClient(create_app(settings)) as client:
        client.post("/api/chat", json={"text": "在吗"})
        res = client.get("/api/export")

        assert res.status_code == 200
        assert "text/markdown" in res.headers["content-type"]
        assert "attachment" in res.headers["content-disposition"]
        assert "在吗" in res.text
        assert "她记得的事" in res.text or "## " in res.text


async def test_long_gap_brings_up_recent_days(tmp_path):
    provider = FakeProvider()
    engine, db = _engine(tmp_path, provider)
    await engine.startup()  # 启动会补上前几天

    three_days_ago = (NOW - timedelta(days=3)).isoformat()
    await db.insert_message("user", "上次说到这", "cli", three_days_ago)
    await db.insert_message("assistant", "嗯", "cli", three_days_ago)

    await engine.handle("我回来了")

    p5 = provider.last_messages[-2]["content"]
    assert "这几天你过的日子" in p5
    assert "别当成今天" in p5
    db.close()


async def test_short_gap_does_not_bring_up_past_days(tmp_path):
    provider = FakeProvider()
    engine, db = _engine(tmp_path, provider)
    await engine.startup()

    await db.insert_message("user", "刚说完", "cli", (NOW - timedelta(minutes=10)).isoformat())

    await engine.handle("在吗")

    p5 = provider.last_messages[-2]["content"]
    assert "这几天你过的日子" not in p5
    db.close()