"""对话里的管理指令：改人设、改档案、归档记忆。"""

from pathlib import Path

import yaml

from app.admin import AdminCommands
from app.db import Database
from app.persona import PersonaStore
from app.profile import ProfileStore

SAMPLE_PERSONA = """
spec: aigirl_card_v1
name: 林夏
user_nickname: 阿川
stage: 暧昧
known_since: "2026-07-19"
description: 书店店员
personality: 话短
scenario: 三个月前认识
system_prompt: 你是林夏
post_history_instructions: 只发短消息
first_mes: 在吗
mes_example: "<user>在吗<char>在"
character_book:
  entries: []
"""

SAMPLE_PROFILE = """
name: 阿川
sections:
  - title: 基本
    facts:
      - 在上海做后端
"""


def _setup(tmp_path):
    persona_path = tmp_path / "persona.yaml"
    profile_path = tmp_path / "user_profile.yaml"
    persona_path.write_text(SAMPLE_PERSONA, encoding="utf-8")
    profile_path.write_text(SAMPLE_PROFILE, encoding="utf-8")
    db = Database(tmp_path / "a.db")
    persona_store = PersonaStore(persona_path)
    profile_store = ProfileStore(profile_path)
    admin = AdminCommands(db, persona_store, profile_store, persona_path, profile_path)
    return admin, db, persona_store, profile_store, tmp_path


def test_non_command_returns_none():
    assert AdminCommands.is_command("今天吃什么") is False


async def test_help(tmp_path):
    admin, db, *_ = _setup(tmp_path)
    text = await admin.run("/help")
    assert "/persona" in text and "/forget" in text
    db.close()


async def test_persona_set_writes_local_and_reloads(tmp_path):
    admin, db, persona_store, _, path = _setup(tmp_path)

    reply = await admin.run("/persona set nickname 阿川")
    assert "阿川" in reply

    local = path / "persona.local.yaml"
    assert local.exists()  # 写到 .local，不动示例
    saved = yaml.safe_load(local.read_text(encoding="utf-8"))
    assert saved["user_nickname"] == "阿川"

    # 示例文件没被改
    sample = yaml.safe_load((path / "persona.yaml").read_text(encoding="utf-8"))
    assert sample["user_nickname"] == "阿川"

    # store 已经指向新文件并读到新值
    assert persona_store.load().user_nickname == "阿川"
    db.close()


async def test_persona_rejects_unknown_field(tmp_path):
    admin, db, *_ = _setup(tmp_path)
    reply = await admin.run("/persona set 年龄 20")
    assert "没这个字段" in reply
    db.close()


async def test_profile_add_and_del(tmp_path):
    admin, db, _, profile_store, path = _setup(tmp_path)

    assert "记下了" in await admin.run("/profile add 不吃香菜")
    assert "不吃香菜" in profile_store.load().facts

    assert "不吃香菜" in await admin.run("/profile del 香菜")
    assert "不吃香菜" not in profile_store.load().facts

    saved = yaml.safe_load((path / "user_profile.local.yaml").read_text(encoding="utf-8"))
    kept = [f for s in saved["sections"] for f in s["facts"]]
    assert "不吃香菜" not in kept and "在上海做后端" in kept
    db.close()


async def test_forget_by_keyword_archives(tmp_path):
    admin, db, *_ = _setup(tmp_path)
    await db.add_memory(
        kind="fact", topic=None, content="他不吃香菜", importance=3, confidence=0.9,
        event_time=None, entities=[], source_msg_id=None, now="2026-10-20T10:00:00",
    )

    reply = await admin.run("/forget 香菜")
    assert "归档了" in reply
    assert await db.all_memories() == []
    db.close()


async def test_mem_lists_entries(tmp_path):
    admin, db, *_ = _setup(tmp_path)
    await db.add_memory(
        kind="fact", topic=None, content="他养了只猫", importance=3, confidence=0.9,
        event_time=None, entities=[], source_msg_id=None, now="2026-10-20T10:00:00",
    )
    reply = await admin.run("/mem")
    assert "[1]" in reply and "他养了只猫" in reply
    db.close()
