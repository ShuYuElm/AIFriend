"""角色卡 / 档案的读取与保存接口。"""

import shutil

from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app


def _settings(tmp_path) -> Settings:
    persona = tmp_path / "persona.yaml"
    profile = tmp_path / "user_profile.yaml"
    shutil.copy(ROOT / "config" / "persona.yaml", persona)
    shutil.copy(ROOT / "config" / "user_profile.yaml", profile)
    return Settings(
        llm_base_url="http://localhost",
        llm_api_key="",
        llm_model="mock",
        tz="Asia/Shanghai",
        db_path=tmp_path / "web.db",
        persona_path=persona,
        life_path=ROOT / "config" / "life.yaml",
        profile_path=profile,
        seed_path=ROOT / "config" / "seed_memories.yaml",
        world_path=tmp_path / "no-world.yaml",
    )


def test_get_persona_has_all_fields(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        data = client.get("/api/persona").json()
        assert data["ok"] is True
        persona = data["persona"]
        for field in ("name", "user_nickname", "description", "personality", "system_prompt"):
            assert persona[field]
        assert isinstance(persona["lorebook"], list)
        assert persona["lorebook"] and persona["lorebook"][0]["keys"]


def test_put_persona_writes_local_and_reloads(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        persona = client.get("/api/persona").json()["persona"]
        persona["name"] = "新名字"
        persona["description"] = "改过的描述"
        persona["lorebook"] = [{"keys": ["书店"], "content": "她在书店上班", "constant": True}]

        res = client.put("/api/persona", json=persona).json()
        assert res["ok"] is True and res["name"] == "新名字"

        # 写到了 .local.yaml，示例文件没动
        assert (tmp_path / "persona.local.yaml").exists()
        assert "新名字" not in (tmp_path / "persona.yaml").read_text(encoding="utf-8")

        again = client.get("/api/persona").json()["persona"]
        assert again["name"] == "新名字"
        assert again["description"] == "改过的描述"
        assert again["lorebook"][0]["constant"] is True

        # 标题也跟着变
        assert client.get("/api/state").json()["name"] == "新名字"


def test_put_persona_rejects_missing_required(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        persona = client.get("/api/persona").json()["persona"]
        persona["system_prompt"] = "   "
        res = client.put("/api/persona", json=persona).json()
        assert res["ok"] is False
        assert "system_prompt" in res["errors"][0]


def test_profile_get_and_put(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        facts = client.get("/api/profile").json()["facts"]
        assert facts

        res = client.put("/api/profile", json={"facts": ["在成都上班", "不吃香菜"]}).json()
        assert res["ok"] is True and res["count"] == 2

        assert client.get("/api/profile").json()["facts"] == ["在成都上班", "不吃香菜"]
        assert (tmp_path / "user_profile.local.yaml").exists()
