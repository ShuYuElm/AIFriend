"""核心层：重要到不需要召回的记忆。"""

from app.db import Database
from app.llm import MockProvider
from app.memory import CORE_LIMIT, CORE_MAX_CHARS, Memory

NOW = "2026-10-24T20:00:00"


async def _add(db, content, importance=5, kind="event", now=NOW):
    await db.add_memory(
        kind=kind, topic=None, content=content, importance=importance,
        confidence=0.9, event_time=None, entities=[], source_msg_id=None, now=now,
    )


def _tracker(db):
    return Memory(db, MockProvider(), "Asia/Shanghai")


async def test_core_keeps_only_important_relationship_memories(tmp_path):
    db = Database(tmp_path / "m.db")
    await _add(db, "三个月前在王者五排认识他")
    await _add(db, "有次聊到凌晨三点", kind="event")
    await _add(db, "他不吃香菜", importance=2, kind="preference")
    await _add(db, "他答应早睡", kind="promise")

    core = await _tracker(db).core()
    texts = [item.content for item in core]

    assert "三个月前在王者五排认识他" in texts
    assert "他不吃香菜" not in texts       # 重要度不够
    assert "他答应早睡" not in texts       # 承诺不算关系骨架
    db.close()


async def test_core_skips_superseded_versions(tmp_path):
    db = Database(tmp_path / "m.db")
    old = await db.add_memory(
        kind="event", topic=None, content="他住成都", importance=5, confidence=0.9,
        event_time=None, entities=[], source_msg_id=None, now="2026-09-01T10:00:00",
    )
    await db.mark_superseded([old], old + 1)
    await _add(db, "他搬到上海了", now="2026-10-01T10:00:00")

    texts = [item.content for item in await _tracker(db).core()]
    assert "他住成都" not in texts
    assert "他搬到上海了" in texts
    db.close()


async def test_core_respects_limit_and_budget(tmp_path):
    db = Database(tmp_path / "m.db")
    for index in range(10):
        await _add(db, f"第{index}件重要的事" + "啊" * 20)

    core = await _tracker(db).core()
    assert len(core) <= CORE_LIMIT
    assert sum(len(item.content) for item in core) <= CORE_MAX_CHARS + 200
    db.close()


async def test_core_is_injected_even_for_unrelated_message(tmp_path):
    db = Database(tmp_path / "m.db")
    await _add(db, "你们在高铁站分开过一次，谁都没先走")
    await _add(db, "今天食堂的菜很咸", importance=3)

    block = await _tracker(db).block("今天吃什么")
    assert "你一定记得的事" in block
    assert "高铁站" in block
    db.close()


async def test_core_and_recall_do_not_repeat(tmp_path):
    db = Database(tmp_path / "m.db")
    content = "你们在高铁站分开过一次"
    await _add(db, content)

    block = await _tracker(db).block("还记得高铁站那次吗")
    assert block.count(content) == 1  # 核心层出现了，召回块不再重复
    db.close()


async def test_core_block_can_be_empty(tmp_path):
    db = Database(tmp_path / "m.db")
    await _add(db, "他不吃香菜", importance=2)

    block = await _tracker(db).block("在吗")
    assert "你一定记得的事" not in block
    db.close()
