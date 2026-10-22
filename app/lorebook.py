"""静态世界设定：关键词触发 + token 预算。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import LOREBLOOK_MAX_ENTRIES


@dataclass(slots=True)
class Entry:
    keys: list[str] = field(default_factory=list)
    content: str = ""
    enabled: bool = True
    constant: bool = False
    insertion_order: int = 100
    priority: int = 80

    def matches(self, text: str) -> bool:
        return any(key and key in text for key in self.keys)


class Lorebook:
    def __init__(
        self,
        entries: list[Entry] | None = None,
        token_budget: int = 400,
        scan_depth: int = 4,
    ) -> None:
        self.entries = entries or []
        self.token_budget = token_budget
        self.scan_depth = scan_depth

    @classmethod
    def from_book(cls, book: dict[str, Any] | None) -> "Lorebook":
        book = book or {}
        entries: list[Entry] = []
        for raw in book.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            entries.append(
                Entry(
                    keys=[str(k) for k in (raw.get("keys") or [])],
                    content=str(raw.get("content") or "").strip(),
                    enabled=bool(raw.get("enabled", True)),
                    constant=bool(raw.get("constant", False)),
                    insertion_order=int(raw.get("insertion_order", 100)),
                    priority=int(raw.get("priority", 80)),
                )
            )
        return cls(
            entries,
            token_budget=int(book.get("token_budget", 400)),
            scan_depth=int(book.get("scan_depth", 4)),
        )

    def match(self, text: str) -> list[Entry]:
        """常驻条目永远注入；关键词命中按 priority 降序，受条数与预算限制。"""
        active = [e for e in self.entries if e.enabled and e.content]
        constants = sorted(
            [e for e in active if e.constant], key=lambda e: (e.insertion_order, -e.priority)
        )
        hits = sorted(
            [e for e in active if not e.constant and e.matches(text)],
            key=lambda e: (-e.priority, e.insertion_order),
        )

        selected = constants + hits[: max(0, LOREBLOOK_MAX_ENTRIES - len(constants))]
        selected.sort(key=lambda e: (e.insertion_order, -e.priority))

        out: list[Entry] = []
        used = 0
        for entry in selected:
            cost = estimate_tokens(entry.content)
            if out and used + cost > self.token_budget:
                continue
            out.append(entry)
            used += cost
        return out

    @staticmethod
    def format(entries: list[Entry]) -> str:
        if not entries:
            return ""
        lines = ["## 你不会主动提起、但本来就知道的事", ""]
        lines += [f"- {e.content}" for e in entries]
        return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """粗估：中文约 0.7 token/字，其余约 0.3。"""
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - chinese
    return int(chinese * 0.7 + other * 0.3)
