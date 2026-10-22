"""内部数据模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Message:
    role: str
    content: str
    id: int | None = None
    created_at: str = ""


@dataclass(slots=True)
class Bubble:
    """一条要发出去的消息气泡。"""

    text: str
    delay_ms: int = 0


@dataclass(slots=True)
class LifeState:
    """她此刻在干什么。"""

    state: str = ""
    detail: str = ""

    def describe(self) -> str:
        return f"{self.state}（{self.detail}）" if self.detail else self.state


@dataclass(slots=True)
class MemoryItem:
    """一条长期记忆。"""

    id: int
    kind: str
    content: str
    importance: int = 3
    topic: str | None = None
    confidence: float = 0.8
    event_time: str | None = None
    created_at: str = ""
    entities: list[str] | None = None
    superseded_by: int | None = None
    last_used_at: str | None = None
    use_count: int = 0
    source_msg_id: int | None = None
    episode_id: int | None = None
    episode_count: int = 1
    episode_last_seen: str = ""

    @property
    def is_old(self) -> bool:
        return self.superseded_by is not None

    @property
    def repeated(self) -> bool:
        """这件事他提过不止一次。"""
        return self.episode_count > 1
