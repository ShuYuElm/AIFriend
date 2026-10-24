"""她此刻对他的情绪。

规则驱动，不花模型调用：看他说了什么、隔了多久，移动一个 -2..+2 的刻度，
再按时间自然消气。情绪只影响语气，不参与心跳闸门。

存 `state` 表的 `mood_toward`：{"level": -2..2, "note": "...", "updated_at": "..."}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from .db import Database
from .persona import now_local
from .timeutil import align

logger = logging.getLogger("aigirl.mood")

STATE_KEY = "mood_toward"

# 他说的软话 / 敷衍 / 难听 / 道歉
WARM_WORDS = ("喜欢你", "想你", "可爱", "抱抱", "么么", "爱你", "辛苦", "晚安", "早安", "乖")
COLD_WORDS = ("嗯", "哦", "随便", "都行", "行吧", "算了", "是吗", "呵呵", "没事")
ANGRY_WORDS = ("烦", "闭嘴", "别烦", "滚", "讨厌", "生气", "少来")
APOLOGY_WORDS = ("对不起", "抱歉", "我错了", "不好意思", "是我不好")

LONG_GAP_HOURS = 18        # 这么久没说话，她会有点介意
DECAY_AFTER_HOURS = 6      # 没新刺激就开始消气
FORGET_AFTER_HOURS = 48    # 两天后回到平常
MAX_LEVEL = 2


@dataclass
class Mood:
    level: int = 0
    note: str = ""
    updated_at: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {"level": self.level, "note": self.note, "updated_at": self.updated_at},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "Mood":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return cls()
        return cls(
            level=max(-MAX_LEVEL, min(MAX_LEVEL, int(data.get("level", 0)))),
            note=str(data.get("note") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


def _step(level: int) -> int:
    """往 0 靠一步。"""
    return level - 1 if level > 0 else level + 1 if level < 0 else 0


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


class MoodTracker:
    def __init__(self, db: Database, tz: str) -> None:
        self.db = db
        self.tz = tz

    async def load(self) -> Mood:
        return Mood.from_json(await self.db.get_state(STATE_KEY))

    async def save(self, mood: Mood) -> None:
        await self.db.set_state(STATE_KEY, mood.to_json())

    def _decay(self, mood: Mood, now: datetime) -> Mood:
        """时间过去，情绪自然回落。"""
        if mood.level == 0 or not mood.updated_at:
            return mood
        try:
            last = align(datetime.fromisoformat(mood.updated_at), now)
        except ValueError:
            return mood
        hours = (now - last).total_seconds() / 3600
        if hours >= FORGET_AFTER_HOURS:
            logger.info("情绪回到平常（%.0f 小时没新的事）", hours)
            return Mood()
        if hours >= DECAY_AFTER_HOURS:
            mood.level = _step(mood.level)
            if mood.level == 0:
                mood.note = ""
        return mood

    async def observe(self, text: str, gap_hours: float | None, now: datetime | None = None) -> Mood:
        """看完他这句话，更新情绪。"""
        now = now or now_local(self.tz)
        mood = self._decay(await self.load(), now)

        before = mood.level
        lowered = text or ""

        # 先算"隔了多久"，再看他这句话：冷落会扣分，软话能抵消回来
        if gap_hours is not None and gap_hours >= LONG_GAP_HOURS and mood.level > -MAX_LEVEL:
            mood.level -= 1
            mood.note = f"他隔了 {int(gap_hours)} 小时才回"

        if _has(lowered, ANGRY_WORDS):
            mood.level = -MAX_LEVEL
            mood.note = "他刚才说话有点冲"
        elif _has(lowered, APOLOGY_WORDS):
            mood.level = min(MAX_LEVEL, mood.level + 1)
            mood.note = "他道歉了"
        elif _has(lowered, WARM_WORDS):
            mood.level = min(MAX_LEVEL, mood.level + 1)
            mood.note = "他说了句软话"
        elif len(lowered.strip()) <= 3 and _has(lowered, COLD_WORDS):
            mood.level = max(-MAX_LEVEL, mood.level - 1)
            mood.note = "他回得很短，像在敷衍"

        mood.level = max(-MAX_LEVEL, min(MAX_LEVEL, mood.level))
        if mood.level == 0:
            mood.note = ""
        mood.updated_at = now.isoformat()
        await self.save(mood)

        if mood.level != before:
            logger.info("情绪 %+d → %+d（%s）", before, mood.level, mood.note)
        return mood

    @staticmethod
    def render(mood: Mood) -> str:
        """拼成 P1 里的一行；0 的时候不占位置。"""
        if mood.level == 0:
            return ""
        feeling = {
            2: "你此刻心情很好，对他很亲近",
            1: "你此刻心情不错",
            -1: "你此刻有点不高兴",
            -2: "你此刻在生气",
        }[mood.level]
        reason = f"（{mood.note}）" if mood.note else ""
        hint = "别解释自己的情绪，让它影响语气就够了。"
        return f"{feeling}{reason}。{hint}"
