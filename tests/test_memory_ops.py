"""自动日记、遗忘、原话回读。"""

from datetime import datetime, timedelta

from app.db import Database
from app.llm import MockProvider
from app.memory import Memory

NOW = datetime(2026, 10, 21, 10, 0)


class SummaryProvider:
    name = "fake"
    model = "fake"

    async def chat(self, messages, **kwargs):
        return "昨天他赶论文赶到很晚，说周五要交初稿，还聊到周末想出去走走。"


async def _seed_day(db, day: str, count: int = 4) -> None:
    for i in range(count):
        stamp = f"{day}T{10 + i:02d}:00:00"
        await db.insert_message("user", f"第{i}句", "cli", stamp)
        await db.insert_message("assistant", f"回{i}", "cli", stamp)


# ------------------------------------------------------------------ 日记

async def test_summarize_day_writes_once(tmp_path):
    db = Database(tmp_path / "m.db")
    await _seed_day(db, "2026-10-20")
    memory = Memory(db, SummaryProvider(), "Asia/Shanghai")

    assert await memory.summarize_day("2026-10-20") is True
    assert await memory.summarize_day("2026-10-20") is False  # 不重复写

    rows = await db.summaries(3)
    assert len(rows) == 1 and "论文" in rows[0]["content"]
    db.close()


async def test_summarize_day_skips_thin_day(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.insert_message("user", "只有一句", "cli", "2026-10-20T10:00:00")
    memory = Memory(db, SummaryProvider(), "Asia/Shanghai")

    assert await memory.summarize_day("2026-10-20") is False
    db.close()


# ------------------------------------------------------------------ 遗忘

async def test_forget_stale_archives_old_low_value(tmp_path):
    db = Database(tmp_path / "m.db")
    old = (NOW - timedelta(days=120)).isoformat()
    fresh = (NOW - timedelta(days=2)).isoformat()
    for content, importance, created in (
        ("随口说过的一句", 2, old),
        ("他的生日", 5, old),
        ("最近说的事", 2, fresh),
    ):
        await db.add_memory(
            kind="fact", topic=None, content=content, importance=importance,
            confidence=0.9, event_time=None, entities=[], source_msg_id=None, now=created,
        )
    await db.add_memory(
        kind="promise", topic=None, content="答应他的事", importance=1,
        confidence=0.9, event_time=None, entities=[], source_msg_id=None, now=old,
        due_at=None,
    )

    memory = Memory(db, MockProvider(), "Asia/Shanghai")
    assert await memory.forget_stale(days=90, max_importance=2) == 1

    left = {m.content for m in await db.all_memories()}
    assert "随口说过的一句" not in left
    assert {"他的生日", "最近说的事", "答应他的事"} <= left
    db.close()


async def test_forget_matching_by_keyword(tmp_path):
    db = Database(tmp_path / "m.db")
    for content in ("他不吃香菜", "他喜欢跑步"):
        await db.add_memory(
            kind="fact", topic=None, content=content, importance=3, confidence=0.9,
            event_time=None, entities=[], source_msg_id=None, now=NOW.isoformat(),
        )
    memory = Memory(db, MockProvider(), "Asia/Shanghai")

    removed = await memory.forget_matching("香菜")
    assert removed == ["他不吃香菜"]
    assert [m.content for m in await db.all_memories()] == ["他喜欢跑步"]
    db.close()


# ------------------------------------------------------------------ 原话回读

async def test_detail_block_reads_original_wording(tmp_path):
    db = Database(tmp_path / "m.db")
    first = await db.insert_message("user", "我周五要交初稿，导师催得紧", "cli", "2026-10-20T10:00:00")
    await db.insert_message("assistant", "那你这两天别熬太晚", "cli", "2026-10-20T10:01:00")
    await db.add_memory(
        kind="event", topic="工作学业", content="他周五要交论文初稿", importance=4,
        confidence=0.9, event_time="2026-10-20", entities=["论文"], source_msg_id=first,
        now="2026-10-20T10:02:00",
    )

    memory = Memory(db, MockProvider(), "Asia/Shanghai")
    block = await memory.detail_block("还记得我上次说初稿的事吗")
    assert "我周五要交初稿" in block  # 原话，不是 40 字摘要
    assert "他：" in block

    assert await memory.detail_block("今天吃什么") == ""  # 没提旧事就不翻
    db.close()
