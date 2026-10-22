"""记忆：相似度、打分、抽取解析、取代关系。"""

from datetime import datetime

import pytest

from app.db import Database
from app.memory import (
    EXTRACT_MARKER,
    ROLLUP_MARKER,
    SUMMARY_MARKER,
    Memory,
    age_label,
    build_extract_prompt,
    char_bigrams,
    parse_extraction,
    relevance,
    render,
    score,
)
from app.models import MemoryItem, Message

NOW = datetime(2026, 10, 19, 21, 0)


class ScriptedProvider:
    name = "fake"
    model = "fake"

    def __init__(
        self,
        chat_text="嗯",
        extract_json="[]",
        summary_text="今天没什么。",
        rollup_text="他这周在赶论文初稿，情绪有点紧，说周末想打两把。",
    ) -> None:
        self.chat_text = chat_text
        self.extract_json = extract_json
        self.summary_text = summary_text
        self.rollup_text = rollup_text
        self.calls: list[str] = []

    async def chat(self, messages, **kwargs):
        system = messages[0]["content"]
        self.calls.append(system[:20])
        if EXTRACT_MARKER in system:
            return self.extract_json
        if SUMMARY_MARKER in system:
            return self.summary_text
        if ROLLUP_MARKER in system:
            return self.rollup_text
        return self.chat_text


# ------------------------------------------------------------------ 纯函数

def test_bigrams():
    assert char_bigrams("杭州") == {"杭州"}
    assert "实习" in char_bigrams("今天实习好累")


def test_relevance_bounds():
    assert relevance({"ab"}, {"ab"}) == 1.0
    assert relevance({"ab"}, {"cd"}) == 0.0
    assert relevance(set(), {"ab"}) == 0.0


def test_recent_memory_scores_higher():
    fresh = MemoryItem(id=1, kind="fact", content="他在杭州做实习", importance=4,
                       created_at="2026-10-18T10:00:00", entities=["杭州"])
    stale = MemoryItem(id=2, kind="fact", content="他在杭州做实习", importance=4,
                       created_at="2025-01-01T10:00:00", entities=["杭州"])
    q = char_bigrams("杭州实习怎么样")
    assert score(fresh, q, "杭州实习怎么样", NOW) > score(stale, q, "杭州实习怎么样", NOW)


def test_age_label():
    item = MemoryItem(id=1, kind="fact", content="x", created_at="2026-10-16T10:00:00")
    assert age_label(item, NOW) == "3天前"
    item2 = MemoryItem(id=2, kind="fact", content="x", created_at="2026-07-19T10:00:00")
    assert age_label(item2, NOW) == "3个月前"


def test_render_marks_superseded():
    old = MemoryItem(id=1, kind="fact", content="他住成都", created_at="2026-08-01T10:00:00",
                     superseded_by=2)
    new = MemoryItem(id=2, kind="fact", content="他搬到杭州了", created_at="2026-10-01T10:00:00")
    text = render([new, old], NOW)
    assert "（以前）他住成都" in text
    assert "他搬到杭州了" in text


# ------------------------------------------------------------------ 抽取解析

def test_parse_plain_json():
    raw = '[{"kind":"fact","content":"他在杭州实习","importance":4,"confidence":0.9}]'
    items = parse_extraction(raw)
    assert len(items) == 1
    assert items[0]["content"] == "他在杭州实习"
    assert items[0]["importance"] == 4


def test_parse_fenced_json():
    raw = '```json\n[{"kind":"preference","content":"他喜欢火锅","confidence":0.8}]\n```'
    assert parse_extraction(raw)[0]["content"] == "他喜欢火锅"


def test_parse_garbage_returns_empty():
    assert parse_extraction("我不知道") == []
    assert parse_extraction("") == []
    assert parse_extraction("[not json]") == []


def test_parse_clamps_and_defaults():
    raw = '[{"content":"他很忙","importance":99,"confidence":"x","kind":"乱写"}]'
    item = parse_extraction(raw)[0]
    assert item["importance"] == 5
    assert item["confidence"] == 0.8
    assert item["kind"] == "fact"


def test_parse_limits_five():
    raw = "[" + ",".join('{"content":"事实%d"}' % i for i in range(9)) + "]"
    assert len(parse_extraction(raw)) == 5


def test_extract_prompt_lists_existing_ids():
    existing = [MemoryItem(id=7, kind="fact", content="他住成都")]
    prompt = build_extract_prompt(existing, "他：我搬到杭州了")
    assert EXTRACT_MARKER in prompt
    assert "[7] 他住成都" in prompt


# ------------------------------------------------------------------ 服务

async def test_extract_adds_memory(tmp_path):
    db = Database(tmp_path / "m.db")
    provider = ScriptedProvider(
        extract_json='[{"kind":"fact","topic":"住处","content":"他搬到杭州了","importance":4,'
        '"confidence":0.9,"entities":["杭州"]}]'
    )
    memory = Memory(db, provider)

    added = await memory.extract([Message(role="user", content="我搬到杭州了")])

    assert added == 1
    items = await db.all_memories()
    assert items[0].content == "他搬到杭州了"
    assert items[0].entities == ["杭州"]
    db.close()


async def test_extract_skips_low_confidence(tmp_path):
    db = Database(tmp_path / "m.db")
    provider = ScriptedProvider(
        extract_json='[{"kind":"fact","content":"他可能喜欢猫","confidence":0.3}]'
    )
    memory = Memory(db, provider)
    assert await memory.extract([Message(role="user", content="嗯")]) == 0
    db.close()


async def test_supersede_marks_old_memory(tmp_path):
    db = Database(tmp_path / "m.db")
    old_id = await db.add_memory(
        kind="fact", topic="住处", content="他住成都", importance=4, confidence=0.9,
        event_time=None, entities=["成都"], source_msg_id=None, now="2026-08-01T10:00:00",
    )
    provider = ScriptedProvider(
        extract_json='[{"kind":"fact","topic":"住处","content":"他搬到杭州了","importance":5,'
        '"confidence":0.9,"entities":["杭州"],"supersedes":[%d]}]' % old_id
    )
    memory = Memory(db, provider)
    await memory.extract([Message(role="user", content="我上个月搬到杭州了")])

    items = {item.id: item for item in await db.all_memories()}
    assert items[old_id].superseded_by is not None
    db.close()


async def test_recall_prefers_relevant(tmp_path):
    db = Database(tmp_path / "m.db")
    for content, entities in (("他喜欢打王者", ["王者"]), ("他在成都读大学", ["成都"])):
        await db.add_memory(
            kind="fact", topic=None, content=content, importance=3, confidence=0.9,
            event_time=None, entities=entities, source_msg_id=None, now="2026-10-15T10:00:00",
        )
    memory = Memory(db, provider=ScriptedProvider())
    picked = await memory.recall("今晚王者打不打")
    assert picked and picked[0].content == "他喜欢打王者"
    db.close()


# ------------------------------------------------------------------ 滚动归纳

async def test_rollup_needs_enough_messages(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.insert_message("user", "在吗", "cli", "2026-10-20T10:00:00")
    memory = Memory(db, provider=ScriptedProvider())
    assert await memory.rollup() is False
    db.close()


async def test_rollup_creates_and_is_incremental(tmp_path):
    db = Database(tmp_path / "m.db")
    for i in range(8):
        await db.insert_message("user" if i % 2 == 0 else "assistant", f"第{i}句", "cli", f"2026-10-20T10:0{i}:00")

    memory = Memory(db, provider=ScriptedProvider())
    assert await memory.rollup() is True

    rollups = await db.rollups(3)
    assert len(rollups) == 1
    assert "论文" in rollups[0]["content"]
    assert rollups[0]["to_msg_id"] == 8

    # 没有新消息就不重复归纳
    assert await memory.rollup() is False
    db.close()


async def test_rollup_only_covers_new_messages(tmp_path):
    db = Database(tmp_path / "m.db")
    for i in range(6):
        await db.insert_message("user", f"旧{i}", "cli", f"2026-10-20T09:0{i}:00")
    memory = Memory(db, provider=ScriptedProvider())
    await memory.rollup()

    for i in range(6):
        await db.insert_message("user", f"新{i}", "cli", f"2026-10-20T11:0{i}:00")
    assert await memory.rollup() is True

    rollups = await db.rollups(3)
    assert len(rollups) == 2
    assert rollups[0]["from_msg_id"] == 7
    db.close()


async def test_block_injects_rollup(tmp_path):
    db = Database(tmp_path / "m.db")
    for i in range(6):
        await db.insert_message("user", f"第{i}句", "cli", f"2026-10-20T10:0{i}:00")
    memory = Memory(db, provider=ScriptedProvider())
    await memory.rollup()

    block = await memory.block("随便聊聊")
    assert "之前聊过的" in block
    assert "论文" in block
    db.close()


class EmptyThinkingProvider(ScriptedProvider):
    """思考模式返回空（思维链吃光预算），非思考模式正常。"""

    async def chat(self, messages, **kwargs):
        if kwargs.get("thinking"):
            return ""
        return "他这周在赶论文初稿，周末想打两把游戏，情绪还行。"


async def test_rollup_falls_back_when_thinking_returns_empty(tmp_path):
    db = Database(tmp_path / "m.db")
    for i in range(6):
        await db.insert_message("user", f"第{i}句", "cli", f"2026-10-20T10:0{i}:00")

    memory = Memory(db, provider=EmptyThinkingProvider())
    assert await memory.rollup() is True
    assert len(await db.rollups(3)) == 1
    db.close()
