"""对话里的管理指令：改人设、改档案、删记忆。

直接在聊天框输入 `/persona set nickname 阿川` 就生效，不用去改 YAML。
修改一律写进 `config/*.local.yaml`，不碰仓库里的示例文件。

只在本地用：这些指令没有鉴权，别把服务暴露到公网。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .db import Database
from .persona import PersonaStore
from .profile import ProfileStore

logger = logging.getLogger("aigirl.admin")

# 指令名 → 人设卡字段
PERSONA_FIELDS = {
    "name": "name",
    "nickname": "user_nickname",
    "stage": "stage",
    "known_since": "known_since",
    "first_mes": "first_mes",
}

HELP = """可用的指令
/persona                    看人设卡当前值
/persona set <字段> <值>     字段：name / nickname / stage / known_since / first_mes
/profile                    看你自己的档案
/profile add <一条事实>      例：/profile add 不吃香菜
/profile del <关键词>        删掉含关键词的那几条
/mem                        最近 10 条记忆
/forget <id 或 关键词>       归档记忆（不删除，可恢复）
/help                       这张表"""


def _local_path(path: Path) -> Path:
    """总是写到 *.local.yaml，示例文件不动。"""
    if path.name.endswith(".local.yaml"):
        return path
    stem = path.name[: -len(".yaml")] if path.name.endswith(".yaml") else path.name
    return path.with_name(stem + ".local.yaml")


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("读取 %s 失败：%s", path, exc)
        return {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先留一份原文件：重写成规范 YAML 会丢注释
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    path.write_text(
        yaml.dump(
            data,
            Dumper=_ReadableDumper,
            allow_unicode=True,
            sort_keys=False,
            width=200,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )


class _ReadableDumper(yaml.SafeDumper):
    """多行文本用 | 块，别变成带换行转义的一行。"""


def _represent_str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ReadableDumper.add_representer(str, _represent_str)


class AdminCommands:
    def __init__(
        self,
        db: Database,
        persona_store: PersonaStore,
        profile_store: ProfileStore,
        persona_path: Path,
        profile_path: Path,
    ) -> None:
        self.db = db
        self.persona_store = persona_store
        self.profile_store = profile_store
        self.persona_path = Path(persona_path)
        self.profile_path = Path(profile_path)

    # ---------------------------------------------------------- 入口

    @staticmethod
    def is_command(text: str) -> bool:
        raw = (text or "").strip()
        if not raw.startswith("/"):
            return False
        head = raw.split(maxsplit=1)[0].lower()
        return head in {
            "/help", "/?", "/persona", "/profile", "/me", "/mem", "/forget", "/del"
        }

    async def run(self, text: str) -> str | None:
        """是管理指令就执行并返回回复；不是返回 None。"""
        if not self.is_command(text):
            return None
        raw = (text or "").strip()
        parts = raw.split(maxsplit=2)
        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        tail = parts[2] if len(parts) > 2 else ""

        if command in ("/help", "/?"):
            return HELP
        if command == "/persona":
            return self._persona(rest, tail) if rest else self._show_persona()
        if command in ("/profile", "/me"):
            return self._profile(rest, tail) if rest else self._show_profile()
        if command == "/mem":
            return await self._memories()
        if command in ("/forget", "/del"):
            return await self._forget(rest)
        return None

    # ---------------------------------------------------------- 人设卡

    def _show_persona(self) -> str:
        card = self.persona_store.load()
        return (
            f"名字：{card.name}\n"
            f"她叫你：{card.user_nickname}\n"
            f"关系阶段：{card.stage}\n"
            f"认识那天：{card.known_since or '（没填）'}\n"
            f"开场白：{card.first_mes}\n\n"
            "改：/persona set nickname 阿川"
        )

    def _persona(self, sub: str, tail: str) -> str:
        if sub != "set":
            return "用法：/persona set <字段> <值>"
        bits = tail.split(maxsplit=1) if tail else []
        if len(bits) < 2:
            return "用法：/persona set nickname 阿川\n字段：" + " / ".join(PERSONA_FIELDS)

        field, value = bits[0].strip().lower(), bits[1].strip()
        key = PERSONA_FIELDS.get(field)
        if key is None:
            return f"没这个字段：{field}。可以改：{' / '.join(PERSONA_FIELDS)}"

        target = _local_path(self.persona_path)
        data = _read_yaml(target if target.exists() else self.persona_path)
        old = data.get(key, "")
        data[key] = value
        _write_yaml(target, data)
        self.persona_store.invalidate(target)
        logger.info("人设卡 %s：%s → %s", key, old, value)
        return f"改好了：{field}「{old}」→「{value}」\n下一句就生效。"

    # ---------------------------------------------------------- 用户档案

    def _show_profile(self) -> str:
        profile = self.profile_store.load()
        if not profile.facts:
            return "档案还是空的。加一条：/profile add 不吃香菜"
        shown = "\n".join(f"- {fact}" for fact in profile.facts[:12])
        more = f"\n…共 {len(profile.facts)} 条" if len(profile.facts) > 12 else ""
        return f"关于他（{len(profile.facts)} 条）\n{shown}{more}"

    def _profile(self, sub: str, tail: str) -> str:
        if sub == "add":
            return self._profile_add(tail)
        if sub in ("del", "rm", "remove"):
            return self._profile_del(tail)
        return "用法：/profile add <事实> 或 /profile del <关键词>"

    def _profile_add(self, fact: str) -> str:
        fact = (fact or "").strip()
        if not fact:
            return "用法：/profile add 不吃香菜"
        if "___" in fact or fact.upper().startswith("TODO"):
            return "这条还是占位符，换成真事再发。"

        target = _local_path(self.profile_path)
        data = _read_yaml(target if target.exists() else self.profile_path)
        sections = data.setdefault("sections", [])
        if not isinstance(sections, list):
            sections = []
            data["sections"] = sections

        bucket = next(
            (s for s in sections if isinstance(s, dict) and s.get("title") == "基本"), None
        )
        if bucket is None:
            bucket = {"title": "补充", "facts": []}
            sections.append(bucket)
        facts = bucket.setdefault("facts", [])
        if fact in facts:
            return "这条已经记着了。"
        facts.append(fact)

        _write_yaml(target, data)
        self.profile_store.invalidate(target)
        return f"记下了：{fact}"

    def _profile_del(self, keyword: str) -> str:
        keyword = (keyword or "").strip()
        if not keyword:
            return "用法：/profile del 香菜"

        target = _local_path(self.profile_path)
        source = target if target.exists() else self.profile_path
        data = _read_yaml(source)

        removed: list[str] = []
        for section in data.get("sections") or []:
            if not isinstance(section, dict):
                continue
            kept = []
            for fact in section.get("facts") or []:
                if keyword in str(fact):
                    removed.append(str(fact))
                else:
                    kept.append(fact)
            section["facts"] = kept

        flat = data.get("facts")
        if isinstance(flat, list):
            kept = []
            for fact in flat:
                if keyword in str(fact):
                    removed.append(str(fact))
                else:
                    kept.append(fact)
            data["facts"] = kept

        if not removed:
            return f"没找到含「{keyword}」的条目。"
        _write_yaml(target, data)
        self.profile_store.invalidate(target)
        return "删掉了：\n" + "\n".join(f"- {item}" for item in removed)

    # ---------------------------------------------------------- 记忆

    async def _memories_text(self) -> str:
        items = await self.db.list_memories(10)
        if not items:
            return "还没有记忆。"
        lines = []
        for item in items:
            mark = "（以前）" if item.superseded_by else ""
            times = f" ×{item.episode_count}" if item.episode_count > 1 else ""
            lines.append(f"[{item.id}] {mark}{item.content}{times}")
        return "最近 10 条记忆\n" + "\n".join(lines) + "\n\n删：/forget <id 或 关键词>"

    async def _memories(self) -> str:
        items = await self.db.list_memories(10)
        if not items:
            return "还没有记忆。"
        lines = []
        for item in items:
            mark = "（以前）" if item.superseded_by else ""
            times = f" ×{item.episode_count}" if item.episode_count > 1 else ""
            lines.append(f"[{item.id}] {mark}{item.content}{times}")
        return "最近 10 条记忆\n" + "\n".join(lines) + "\n\n删：/forget <id 或 关键词>"

    async def _forget(self, target: str) -> str:
        target = (target or "").strip()
        if not target:
            return "用法：/forget 12 或 /forget 香菜"

        if target.isdigit():
            removed = await self.db.archive_memory(int(target))
            return f"归档了记忆 {target}。" if removed else f"没有 id 为 {target} 的记忆。"

        hits: list[str] = []
        for item in await self.db.all_memories():
            if target in item.content and await self.db.archive_memory(item.id):
                hits.append(item.content)
        if not hits:
            return f"没找到含「{target}」的记忆。"
        return "归档了：\n" + "\n".join(f"- {item}" for item in hits)
