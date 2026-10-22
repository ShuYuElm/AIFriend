"""记忆溯源与事件归并（schema v4）。"""

from datetime import datetime

from app.db import Database
from app.memory import collapse_by_episode, find_same_event
from app.models import MemoryItem

NOW = "2026-10-20T21:00:00"


def _item(**kwargs) -> MemoryItem:
    base = dict(id=1, kind="fact", content="x", entities=[])
    base.update(kwargs)
    return MemoryItem(**base)


# ------------------------------------------------------------------ 判同一件事

def test_same_event_by_shared_entity_even_when_wording_differs():
    """论文那两条字面只有 0.148，靠实体也要能合并。"""
    pool = [_item(id=1, content="他一边上课一边写论文，论文已接近完成", entities=["论文"])]
    match = find_same_event(
        {"kind": "fact", "content": "他的论文已经真收尾了，不是摸鱼", "entities": ["论文"]},
        pool,
    )
    assert match is not None and match.id == 1


def test_different_kind_never_merges():
    pool = [_item(id=1, kind="event", content="他论文写完了", entities=["论文"])]
    assert find_same_event(
        {"kind": "fact", "content": "他论文写完了", "entities": ["论文"]}, pool
    ) is None


def test_unrelated_entities_do_not_merge():
    pool = [_item(id=1, content="他是曼联铁粉", entities=["曼联"])]
    assert find_same_event(
        {"kind": "fact", "content": "他看F1", "entities": ["F1"]}, pool
    ) is None


def test_superseded_memory_can_still_be_grouped():
    """旧版本被取代了，但仍是同一件事，应该并进同一个 episode。"""
    pool = [_item(id=1, content="他住杭州", entities=["杭州"], superseded_by=9)]
    match = find_same_event(
        {"kind": "fact", "content": "他住杭州", "entities": ["杭州"]}, pool
    )
    assert match is not None and match.id == 1


# ------------------------------------------------------------------ 召回折叠

def test_collapse_keeps_first_of_same_episode():
    a = _item(id=1, episode_id=7, content="第一次")
    b = _item(id=2, episode_id=7, content="第二次")
    c = _item(id=3, episode_id=None, content="独立")
    assert [m.id for m in collapse_by_episode([a, b, c])] == [1, 3]


# ------------------------------------------------------------------ 写入与回填

async def test_extract_records_source_and_episode(tmp_path):
    from app.llm import MockProvider
    from app.memory import EXTRACT_MARKER  # noqa: F401
    from app.models import Message

    class Fake:
        name = "fake"
        model = "fake"

        async def chat(self, messages, **kwargs):
            return (
                '[{"kind":"fact","topic":"工作学业","content":"他在赶论文",'
                '"importance":3,"confidence":0.9,"entities":["论文"]}]'
            )

    db = Database(tmp_path / "m.db")
    from app.memory import Memory

    memory = Memory(db, Fake(), "Asia/Shanghai")
    msg_id = await db.insert_message("user", "我在赶论文", "cli", NOW)
    await db.insert_message("assistant", "加油", "cli", NOW)

    added = await memory.extract(
        [Message(role="user", content="我在赶论文", id=msg_id, created_at=NOW)]
    )
    assert added == 1

    items = await db.all_memories()
    assert items[0].source_msg_id == msg_id
    assert items[0].episode_id is not None
    db.close()


async def test_backfill_groups_existing_duplicates(tmp_path):
    from app.llm import MockProvider
    from app.memory import Memory

    db = Database(tmp_path / "m.db")
    for content in ("他一边上课一边写论文，论文已接近完成", "他的论文已经真收尾了，不是摸鱼"):
        await db.add_memory(
            kind="fact", topic="工作学业", content=content, importance=3, confidence=0.9,
            event_time=None, entities=["论文"], source_msg_id=None, now=NOW,
        )

    memory = Memory(db, MockProvider(), "Asia/Shanghai")
    assert await memory.backfill_episodes() == 2

    items = await db.all_memories()
    episodes = {item.episode_id for item in items}
    assert len(episodes) == 1  # 两条并进同一个事件
    assert items[0].episode_count == 2 or items[1].episode_count == 2
    db.close()


async def test_render_mentions_repeat_count():
    from app.memory import render

    items = [_item(id=1, content="他在赶论文", episode_count=2)]
    text = render(items, datetime(2026, 10, 20, 21, 0))
    assert "提过 2 次" in text
    assert "别把次数说出来" in text
