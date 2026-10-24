"""说话风格包：解析、渲染、API、注入 prompt。"""

from fastapi.testclient import TestClient

from app.chat import ChatEngine
from app.config import ROOT, Settings
from app.db import Database
from app.life import load_life
from app.lorebook import Lorebook
from app.memory import EXTRACT_MARKER
from app.persona import PersonaStore
from app.profile import ProfileStore
from app.prompts import PromptPacks
from app.main import create_app

PACKS_YAML = """
active: standard
packs:
  standard:
    label: 标准
    description: 不加料
    blocks: []
  human:
    label: 更像人
    description: 加小毛病
    blocks: [tics]
blocks:
  tics:
    label: 口语小毛病
    text: |
      - 偶尔打错一个字。
"""


class FakeProvider:
    name = "fake"
    model = "fake"

    def __init__(self):
        self.last_messages = []

    async def chat(self, messages, **kwargs):
        if EXTRACT_MARKER in (messages[0]["content"] if messages else ""):
            return "[]"
        self.last_messages = messages
        return "嗯"


def _packs(tmp_path):
    path = tmp_path / "prompts.yaml"
    path.write_text(PACKS_YAML, encoding="utf-8")
    return PromptPacks(path)


def test_standard_pack_renders_nothing(tmp_path):
    packs = _packs(tmp_path)
    assert packs.render("standard") == ""
    assert packs.default_active() == "standard"


def test_human_pack_renders_block_text(tmp_path):
    packs = _packs(tmp_path)
    assert "打错一个字" in packs.render("human")


def test_catalog_lists_packs_and_blocks(tmp_path):
    packs = _packs(tmp_path)
    catalog = {pack["id"]: pack for pack in packs.catalog()}
    assert set(catalog) == {"standard", "human"}
    assert catalog["human"]["blocks"][0]["label"] == "口语小毛病"


def test_unknown_pack_falls_back(tmp_path):
    packs = _packs(tmp_path)
    assert packs.render("不存在") == ""  # 回退到 standard


def test_missing_file_uses_builtin(tmp_path):
    packs = PromptPacks(tmp_path / "nope.yaml")
    assert packs.default_active() == "standard"
    assert packs.render("standard") == ""


def _engine(tmp_path, provider):
    settings = Settings(
        llm_base_url="http://localhost",
        llm_api_key="",
        llm_model="fake",
        tz="Asia/Shanghai",
        db_path=tmp_path / "t.db",
        persona_path=ROOT / "config" / "persona.yaml",
        life_path=ROOT / "config" / "life.yaml",
        profile_path=ROOT / "config" / "user_profile.yaml",
        seed_path=ROOT / "config" / "seed_memories.yaml",
        world_path=tmp_path / "no-world.yaml",
        prompts_path=tmp_path / "prompts.yaml",
    )
    (tmp_path / "prompts.yaml").write_text(PACKS_YAML, encoding="utf-8")
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
    )
    return engine, db


async def test_pack_text_is_injected_into_system_prompt(tmp_path):
    provider = FakeProvider()
    engine, db = _engine(tmp_path, provider)
    await engine.startup()

    await engine.handle("在吗")  # 标准包：没有加料
    assert "打错一个字" not in provider.last_messages[0]["content"]

    await db.set_state("prompt_pack", "human")
    await engine.handle("在吗")
    assert "打错一个字" in provider.last_messages[0]["content"]
    db.close()


# ------------------------------------------------------------------ API

def _web_settings(tmp_path) -> Settings:
    import shutil

    for name in ("persona.yaml", "user_profile.yaml"):
        shutil.copy(ROOT / "config" / name, tmp_path / name)
    (tmp_path / "prompts.yaml").write_text(PACKS_YAML, encoding="utf-8")
    return Settings(
        llm_base_url="http://localhost",
        llm_api_key="",
        llm_model="mock",
        tz="Asia/Shanghai",
        db_path=tmp_path / "web.db",
        persona_path=tmp_path / "persona.yaml",
        life_path=ROOT / "config" / "life.yaml",
        profile_path=tmp_path / "user_profile.yaml",
        seed_path=ROOT / "config" / "seed_memories.yaml",
        world_path=tmp_path / "no-world.yaml",
        prompts_path=tmp_path / "prompts.yaml",
    )


def test_prompts_api_switch_pack(tmp_path):
    with TestClient(create_app(_web_settings(tmp_path))) as client:
        data = client.get("/api/prompts").json()
        assert data["active"] == "standard"
        assert {pack["id"] for pack in data["packs"]} == {"standard", "human"}
        assert data["text"] == ""

        res = client.put("/api/prompts", json={"active": "human"}).json()
        assert res["ok"] is True and "打错一个字" in res["text"]

        assert client.get("/api/prompts").json()["active"] == "human"


def test_prompts_api_rejects_unknown(tmp_path):
    with TestClient(create_app(_web_settings(tmp_path))) as client:
        res = client.put("/api/prompts", json={"active": "火星"}).json()
        assert res["ok"] is False


def test_mood_api_reflects_conversation(tmp_path):
    with TestClient(create_app(_web_settings(tmp_path))) as client:
        assert client.get("/api/mood").json()["level"] == 0

        client.post("/api/chat", json={"text": "想你了"})

        mood = client.get("/api/mood").json()
        assert mood["level"] == 1
        assert "心情不错" in mood["text"]