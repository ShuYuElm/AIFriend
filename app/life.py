"""她的日程与此刻状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .models import LifeState

_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def to_min(hhmm: str) -> int:
    hour, minute = str(hhmm).split(":")
    return int(hour) * 60 + int(minute)


@dataclass(slots=True)
class Segment:
    start: int
    end: int
    state: str
    detail: str = ""

    def covers(self, minutes: int) -> bool:
        if self.start <= self.end:
            return self.start <= minutes < self.end
        return minutes >= self.start or minutes < self.end


@dataclass(slots=True)
class Life:
    groups: dict[str, list[Segment]] = field(default_factory=dict)
    quiet: tuple[int, int] | None = None
    events: list[str] = field(default_factory=list)

    def segments_for(self, when: datetime) -> list[Segment]:
        weekday = when.weekday()
        keys = ["mon-fri", "weekday"] if weekday < 5 else ["sat-sun", "weekend"]
        keys.append(_DAY_KEYS[weekday])
        for key in keys:
            if key in self.groups:
                return self.groups[key]
        return []

    def state_at(self, when: datetime) -> LifeState:
        minutes = when.hour * 60 + when.minute
        for seg in self.segments_for(when):
            if seg.covers(minutes):
                return LifeState(seg.state, seg.detail)
        return LifeState()

    def is_quiet(self, when: datetime) -> bool:
        if not self.quiet:
            return False
        start, end = self.quiet
        minutes = when.hour * 60 + when.minute
        return start <= minutes < end if start <= end else (minutes >= start or minutes < end)

    def state_for_message(self, when: datetime) -> LifeState:
        """她收到消息时的状态：睡着的人被叫醒，不能还写着"睡了"。"""
        state = self.state_at(when)
        if "睡" not in state.state:
            return state
        minutes = when.hour * 60 + when.minute
        bedtime = self.quiet[0] if self.quiet else 23 * 60
        if (minutes - bedtime) % 1440 <= 60:
            return LifeState("还没睡", "躺着刷手机")
        return LifeState("被消息吵醒了", "有点懵")


def load_life(path: Path) -> Life:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    groups: dict[str, list[Segment]] = {}
    for group, segs in (raw.get("schedule") or {}).items():
        groups[str(group)] = [
            Segment(
                start=to_min(seg["from"]),
                end=to_min(seg["to"]),
                state=str(seg.get("state", "")).strip(),
                detail=str(seg.get("detail", "")).strip(),
            )
            for seg in (segs or [])
        ]

    quiet_raw = raw.get("quiet_hours")
    quiet = (to_min(quiet_raw[0]), to_min(quiet_raw[1])) if quiet_raw else None

    return Life(
        groups=groups,
        quiet=quiet,
        events=[str(e) for e in (raw.get("events") or [])],
    )
