"""说话风格包：可开关的额外规则，拼进 P1。

P1 里已经有一组基础规则（短、别列清单、别每句提问…）。这里放的是"加料"——
默认的标准包不加任何东西，行为与以前完全一致；换包只影响语气，不影响人设。

配置在 `config/prompts.yaml`（可被 `prompts.local.yaml` 覆盖）。
当前选的是哪个包存在 `state` 表，改完下一句生效，不用重启。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("aigirl.prompts")

STATE_KEY = "prompt_pack"

FALLBACK: dict[str, Any] = {
    "active": "standard",
    "packs": {
        "standard": {"label": "标准", "description": "不加额外规则", "blocks": []},
    },
    "blocks": {},
}


class PromptPacks:
    """按 mtime 缓存；文件坏了就退回上一版/内置配置，不让聊天挂掉。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = FALLBACK
        self._mtime: float = -1.0

    def load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._data
        if mtime == self._mtime:
            return self._data

        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("读取 %s 失败，沿用上一版：%s", self.path, exc)
            return self._data

        if not isinstance(raw, dict) or not isinstance(raw.get("packs"), dict):
            logger.warning("%s 结构不对，沿用上一版", self.path)
            return self._data

        self._data = raw
        self._mtime = mtime
        return self._data

    def default_active(self) -> str:
        data = self.load()
        active = str(data.get("active") or "")
        packs = data.get("packs") or {}
        return active if active in packs else next(iter(packs), "standard")

    def pack(self, name: str) -> dict[str, Any]:
        data = self.load()
        packs = data.get("packs") or {}
        return packs.get(name) or packs.get(self.default_active()) or {}

    def render(self, name: str) -> str:
        """把包里的规则块拼成一段文本；标准包返回空串。"""
        data = self.load()
        blocks = data.get("blocks") or {}
        texts: list[str] = []
        for block_id in self.pack(name).get("blocks") or []:
            block = blocks.get(block_id)
            if not isinstance(block, dict):
                continue
            text = str(block.get("text") or "").strip()
            if text:
                texts.append(text)
        if not texts:
            return ""
        return "\n".join(texts)

    def catalog(self) -> list[dict[str, Any]]:
        """给设置面板用的结构：每个包 + 它包含的规则块。"""
        data = self.load()
        blocks = data.get("blocks") or {}
        result = []
        for pack_id, pack in (data.get("packs") or {}).items():
            if not isinstance(pack, dict):
                continue
            entries = []
            for block_id in pack.get("blocks") or []:
                block = blocks.get(block_id) or {}
                entries.append(
                    {
                        "id": block_id,
                        "label": str(block.get("label") or block_id),
                        "text": str(block.get("text") or "").strip(),
                    }
                )
            result.append(
                {
                    "id": pack_id,
                    "label": str(pack.get("label") or pack_id),
                    "description": str(pack.get("description") or ""),
                    "blocks": entries,
                }
            )
        return result
