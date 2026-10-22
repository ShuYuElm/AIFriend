"""用户档案：她本来就知道的、关于"他"的恒定事实。

与 lorebook 的分工：
  lorebook     —— 她的世界（猫、室友、工作）
  user_profile —— 他的世界（住哪、做什么、游戏账号）
  memories     —— 共同经历（带时间，可检索）

支持两种写法：
  facts: [...]                    # 扁平
  sections: [{title, facts}]      # 分组（推荐，便于维护）
两者同时存在时会合并，并按出现顺序去重。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("aigirl.profile")

PLACEHOLDERS = ("___", "TODO", "待填")
MAX_FACTS = 40


@dataclass(slots=True)
class UserProfile:
    name: str = ""
    facts: list[str] = field(default_factory=list)
    dates: dict[str, str] = field(default_factory=dict)


def _usable(fact: str) -> bool:
    return bool(fact) and not any(mark in fact for mark in PLACEHOLDERS)


def _collect(raw: dict) -> list[str]:
    facts: list[str] = []
    for section in raw.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for item in section.get("facts") or []:
            facts.append(str(item).strip())
    for item in raw.get("facts") or []:
        facts.append(str(item).strip())
    return facts


def parse_profile(raw: dict | None) -> UserProfile:
    raw = raw or {}

    seen: set[str] = set()
    facts: list[str] = []
    for fact in _collect(raw):
        if not _usable(fact) or fact in seen:
            continue
        seen.add(fact)
        facts.append(fact)

    if len(facts) > MAX_FACTS:
        logger.warning("user_profile 有 %d 条事实，超过 %d 条上限，多余的不注入", len(facts), MAX_FACTS)
        facts = facts[:MAX_FACTS]

    return UserProfile(
        name=str(raw.get("name") or "").strip(),
        facts=facts,
        dates={str(k): str(v).strip() for k, v in (raw.get("dates") or {}).items()},
    )


class ProfileStore:
    """按 mtime 缓存，改完下一轮生效。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._profile = UserProfile()
        self._mtime: float = -1.0

    def invalidate(self, path: Path | None = None) -> None:
        """管理指令改完文件后调用。"""
        if path is not None:
            self.path = Path(path)
        self._mtime = -1.0

    def load(self) -> UserProfile:
        if not self.path.exists():
            return self._profile
        try:
            mtime = self.path.stat().st_mtime
            if mtime == self._mtime:
                return self._profile
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            self._profile = parse_profile(raw)
            self._mtime = mtime
        except (OSError, yaml.YAMLError) as exc:
            logger.error("用户档案读取失败，沿用上一版：%s", exc)
        return self._profile


def build_block(profile: UserProfile) -> str:
    if not profile.facts:
        return ""
    lines = ["## 关于他（这些是你早就知道的事，不用问，也不要说不确定）", ""]
    lines += [f"- {fact}" for fact in profile.facts]
    return "\n".join(lines)
