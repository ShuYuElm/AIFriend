"""ChatEngine 集成测试（FakeProvider，不联网）。"""

from app.chat import ChatEngine, trim_window
from app.config import ROOT, Settings
from app.db import Database
from app.life import load_life
from app.llm import LLMError
from app.lorebook import Lorebook
from app.memory import EXTRACT_MARKER, SUMMARY_MARKER
from app.persona import PersonaStore
from app.profile import ProfileStore


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, text: str = "又加？\n饭吃了没", extract_json: str = "[]") -> None:
        self.text = text
        self.extract_json = extract_json
        self.last_messages: list[dict[str, str]] = []

    async def chat(self, messages, **kwargs):
        system = messages[0]["content"] if messages else ""
        if EXTRACT_MARKER in system:
            return self.extract_json
        if SUMMARY_MARKER in system:
            return "今天他加班到很晚。"
        self.last_messages = messages
        return self.text


class BoomProvider(FakeProvider):
    async def chat(self, messages, **kwargs):
        raise LLMError("boom")


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_base_url="http://localhost",
        llm_api_key="",
        llm_model="fake",
        tz="Asia/Shanghai",
        db_path=tmp_path / "test.db",
        persona_path=ROOT / "config" / "persona.yaml",
        life_path=ROOT / "config" / "life.yaml",
        profile_path=ROOT / "config" / "user_profile.yaml",
        seed_path=ROOT / "config" / "seed_memories.yaml",
        world_path=tmp_path / "no-world.yaml",
    )


def _build(tmp_path, provider):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    store = PersonaStore(settings.persona_path)
    persona = store.load()
    engine = ChatEngine(
        settings=settings,
        provider=provider,
        db=db,
        persona_store=store,
        profile_store=ProfileStore(settings.profile_path),
        lorebook=Lorebook.from_book(persona.book),
        life=load_life(settings.life_path),
    )
    return engine, db


async def test_handle_returns_bubbles_and_persists(tmp_path):
    engine, db = _build(tmp_path, FakeProvider())
    await engine.startup()

    bubbles = await engine.handle("今天加班到十点")

    assert [b.text for b in bubbles] == ["又加？", "饭吃了没"]
    assert await db.count_messages("user") == 1
    assert await db.count_messages("assistant") == 1
    db.close()


async def test_prompt_includes_profile_facts(tmp_path):
    provider = FakeProvider()
    engine, db = _build(tmp_path, provider)
    await engine.startup()

    await engine.handle("我在哪个区来着")

    system_text = "\n".join(m["content"] for m in provider.last_messages if m["role"] == "system")
    assert "关于他" in system_text
    assert "上海" in system_text
    db.close()


async def test_prompt_includes_persona_and_lorebook(tmp_path):
    provider = FakeProvider()
    engine, db = _build(tmp_path, provider)
    await engine.startup()

    await engine.handle("我在书店碰到一个人")

    system_text = "\n".join(m["content"] for m in provider.last_messages if m["role"] == "system")
    assert "林夏" in system_text
    assert "书店" in system_text
    assert provider.last_messages[-1] == {"role": "user", "content": "我在书店碰到一个人"}
    db.close()


async def test_llm_failure_returns_fallback_without_storing(tmp_path):
    engine, db = _build(tmp_path, BoomProvider())
    await engine.startup()

    bubbles = await engine.handle("在吗")

    assert len(bubbles) == 1
    assert await db.count_messages("assistant") == 0
    db.close()


async def test_seed_memories_inserted_once(tmp_path):
    engine, db = _build(tmp_path, FakeProvider())
    await engine.startup()
    assert await db.count_memories() == 3

    await engine.startup()
    assert await db.count_memories() == 3

    assert await db.get_state("first_seen")
    assert await db.get_state("relationship_stage")
    db.close()


async def test_empty_input_returns_nothing(tmp_path):
    engine, db = _build(tmp_path, FakeProvider())
    await engine.startup()
    assert await engine.handle("   ") == []
    db.close()


async def test_extraction_runs_every_six_messages(tmp_path):
    provider = FakeProvider(
        extract_json='[{"kind":"fact","topic":"工作学业","content":"他在准备考试",'
        '"importance":4,"confidence":0.9}]'
    )
    engine, db = _build(tmp_path, provider)
    await engine.startup()
    before = await db.count_memories()

    for i in range(6):
        await engine.handle(f"第{i}句")
    await engine.drain()

    assert await db.count_memories() == before + 1

    await engine.handle("还记得我在准备什么吗")
    system_text = "\n".join(m["content"] for m in provider.last_messages if m["role"] == "system")
    assert "你记得的事" in system_text
    assert "他在准备考试" in system_text
    db.close()


async def test_admin_command_skips_model_and_history(tmp_path):
    provider = FakeProvider()
    engine, db = _build(tmp_path, provider)
    await engine.startup()

    bubbles = await engine.handle("/help")

    assert len(bubbles) == 1 and "/persona" in bubbles[0].text
    assert provider.last_messages == []          # 没调模型
    assert await db.count_messages() == 0        # 没进聊天历史
    db.close()


# ------------------------------------------------------------------ 窗口裁剪

class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


def test_trim_window_keeps_short_history_intact():
    msgs = [_Msg("短") for _ in range(10)]
    assert len(trim_window(msgs, chars=4000)) == 10


def test_trim_window_drops_oldest_by_chars():
    msgs = [_Msg("x" * 500) for _ in range(30)]  # 15000 字
    kept = trim_window(msgs, chars=4000, floor=8)
    assert sum(len(m.content) for m in kept) <= 4000
    assert kept[-1] is msgs[-1]  # 最新的那条一定在


def test_trim_window_respects_floor():
    msgs = [_Msg("x" * 5000) for _ in range(30)]
    kept = trim_window(msgs, chars=1000, floor=8)
    assert len(kept) == 8  # 宁可超字数，也留够 8 条
