"""配置文件的读写：备份、可读 YAML、角色卡的表单化。

管理指令（app/admin.py）和 Web 编辑面板（/api/persona）共用这里的逻辑：
  - 一律写 *.local.yaml，仓库里的示例文件不动
  - 覆盖前留 .bak（重写成规范 YAML 会丢注释）
  - 多行文本用 | 块，保持能看懂
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("aigirl.editing")

# 角色卡里可以在界面上编辑的字段（顺序即表单顺序）
TEXT_FIELDS = (
    "creator_notes",
    "description",
    "personality",
    "scenario",
    "system_prompt",
    "post_history_instructions",
    "first_mes",
    "mes_example",
)
SHORT_FIELDS = ("name", "user_nickname", "stage", "known_since")


class _ReadableDumper(yaml.SafeDumper):
    """多行文本用 | 块，别变成带换行转义的一行。"""


def _represent_str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ReadableDumper.add_representer(str, _represent_str)


def local_path(path: Path) -> Path:
    """总是写到 *.local.yaml，示例文件不动。"""
    if path.name.endswith(".local.yaml"):
        return path
    stem = path.name[: -len(".yaml")] if path.name.endswith(".yaml") else path.name
    return path.with_name(stem + ".local.yaml")


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("读取 %s 失败：%s", path, exc)
        return {}


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def persona_to_editable(card: Any, raw: dict[str, Any]) -> dict[str, Any]:
    """把加载好的人设卡 + 原始 YAML 变成前端表单结构。"""
    book = raw.get("character_book") or card.book or {}
    entries = []
    for entry in book.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entries.append(
            {
                "keys": [str(k) for k in (entry.get("keys") or [])],
                "content": str(entry.get("content") or ""),
                "constant": bool(entry.get("constant", False)),
                "enabled": bool(entry.get("enabled", True)),
            }
        )

    data: dict[str, Any] = {"spec": "aigirl_card_v1"}
    for field in SHORT_FIELDS:
        data[field] = str(getattr(card, field, "") or "")
    for field in TEXT_FIELDS:
        data[field] = str(getattr(card, field, "") or "")
    data["alternate_greetings"] = list(getattr(card, "alternate_greetings", []) or [])
    data["lorebook"] = entries
    return data


def editable_to_persona(payload: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """把表单内容合回人设卡 YAML（保留 base 里不认识的字段）。"""
    data: dict[str, Any] = dict(base or {})
    data["spec"] = "aigirl_card_v1"

    for field in SHORT_FIELDS + TEXT_FIELDS:
        if field in payload:
            value = payload.get(field)
            data[field] = str(value or "").strip()

    greetings = payload.get("alternate_greetings")
    if isinstance(greetings, list):
        data["alternate_greetings"] = [str(g).strip() for g in greetings if str(g).strip()]

    entries = payload.get("lorebook")
    if isinstance(entries, list):
        book = dict(data.get("character_book") or {})
        book.setdefault("scan_depth", 4)
        book.setdefault("token_budget", 400)
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            keys = entry.get("keys") or []
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.replace("，", ",").split(",")]
            keys = [str(k).strip() for k in keys if str(k).strip()]
            content = str(entry.get("content") or "").strip()
            if not keys or not content:
                continue
            cleaned.append(
                {
                    "keys": keys,
                    "content": content,
                    "enabled": bool(entry.get("enabled", True)),
                    "constant": bool(entry.get("constant", False)),
                    "insertion_order": 100 + 10 * len(cleaned),
                    "priority": 80,
                }
            )
        book["entries"] = cleaned
        data["character_book"] = book
    return data


def validate_persona_data(data: dict[str, Any]) -> list[str]:
    """缺哪些必填字段。"""
    from .persona import REQUIRED_FIELDS

    return [key for key in REQUIRED_FIELDS if not str(data.get(key) or "").strip()]
