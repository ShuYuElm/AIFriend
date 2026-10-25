"""延迟分享：她下午经历的事，晚上才说。

不做的事：
  - 不让模型凭空编经历（编出来假、会重复）——事件是从 life.yaml 的手写池里抽的
  - 不在事情发生时就报告——存进队列，过 1–4 小时才开始"想起来"
  - 不重复分享——分享过就标 shared
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .db import Database
from .life import Life
from .persona import now_local

SHARE_PROBABILITY = 0.3      # 回复后顺带提起的概率
EARLIEST_HOUR = 9            # 太早还没经历什么
LATEST_HOUR = 21             # 21:30 之后不再产生新经历
EXPERIENCE_TTL_HOURS = 24    # 超过一天没分享的事就作废（status=dropped），不能再当"今天"讲
BACKFILL_DAYS = 5            # 服务没开的日子，最多补几天
RETURN_GAP_HOURS = 12        # 隔这么久才回，才提"这几天"


@dataclass(slots=True)
class Experience:
    id: int
    content: str
    occurred_at: str
    share_after: str
    status: str = "pending"


def _to_experience(row: dict[str, Any]) -> Experience:
    return Experience(
        id=int(row["id"]),
        content=str(row["content"]),
        occurred_at=str(row["occurred_at"]),
        share_after=str(row["share_after"]),
        status=str(row["status"]),
    )


class ExperienceQueue:
    def __init__(
        self,
        db: Database,
        life: Life,
        tz: str = "Asia/Shanghai",
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.life = life
        self.tz = tz
        self.rng = rng or random.Random()

    async def ensure_today(self, now: datetime | None = None) -> int:
        """每天生成 0–3 件"今天发生的事"；同一天只生成一次。"""
        now = now or now_local(self.tz)
        await self.expire_old(now)  # 顺手清理过期，不用另开定时
        if now.hour < EARLIEST_HOUR:
            return 0

        day = now.date().isoformat()
        if await self.db.experiences_for_day(day):
            return 0

        pool = list(self.life.events)
        if not pool:
            return 0

        count = self.rng.choices([0, 1, 2, 3], weights=[0.15, 0.35, 0.35, 0.15])[0]
        count = min(count, len(pool))
        if count == 0:
            return 0

        start_min = EARLIEST_HOUR * 60
        end_min = min(now.hour * 60 + now.minute, LATEST_HOUR * 60 + 30)
        if end_min <= start_min:
            end_min = start_min + 30

        for content in self.rng.sample(pool, count):
            minute = self.rng.randrange(start_min, end_min)
            occurred = now.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
            share_after = occurred + timedelta(minutes=self.rng.randint(60, 240))
            await self.db.add_experience(
                content,
                occurred.isoformat(),
                share_after.isoformat(),
                None,
            )
        return count

    async def due(self, now: datetime | None = None) -> list[Experience]:
        now = now or now_local(self.tz)
        rows = await self.db.due_experiences(now.isoformat())
        return [_to_experience(row) for row in rows]

    async def expire_old(
        self, now: datetime | None = None, max_age_hours: int = EXPERIENCE_TTL_HOURS
    ) -> int:
        """超过一天还没说出口的就过期。

        否则五天前"看悬疑剧"这件事，会在下次聊天时被当成今天的事讲出来。
        """
        now = now or now_local(self.tz)
        before = (now - timedelta(hours=max_age_hours)).isoformat()
        return await self.db.expire_experiences(before)

    async def backfill_missed_days(
        self, now: datetime | None = None, days: int = BACKFILL_DAYS
    ) -> int:
        """服务没开的那几天，她也在过日子：每天补 1 件。

        只补空缺、不动已有；补出来的当天就会过期，所以只会出现在
        "这几天我也…"的语境里，不会被当成今天发生的事。
        """
        now = now or now_local(self.tz)
        pool = list(self.life.events)
        if not pool:
            return 0

        made = 0
        for offset in range(days, 0, -1):
            day = (now - timedelta(days=offset)).date()
            if await self.db.experiences_for_day(day.isoformat()):
                continue
            content = self.rng.choice(pool)
            hour = self.rng.randint(10, 20)
            minute = self.rng.randrange(60)
            occurred = datetime(
                day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo
            )
            share_after = occurred + timedelta(minutes=self.rng.randint(60, 240))
            experience_id = await self.db.add_experience(
                content, occurred.isoformat(), share_after.isoformat(), None
            )
            # 补的是历史，直接作废：只能出现在"这几天"的语境里，别当今天
            await self.db.drop_experience(experience_id)
            made += 1
        return made

    async def recent_unshared(
        self, now: datetime | None = None, days: int = BACKFILL_DAYS, limit: int = 3
    ) -> list[Experience]:
        """这几天没说过的事——他隔很久才回来时，用来接一句"这几天我也…"。"""
        now = now or now_local(self.tz)
        start = (now - timedelta(days=days)).isoformat()
        rows = await self.db.experiences_since(start, limit)
        return [_to_experience(row) for row in rows]

    async def mark_shared(self, ids: list[int], now: datetime | None = None) -> None:
        if not ids:
            return
        now = now or now_local(self.tz)
        await self.db.mark_experiences_shared(ids, now.isoformat())

    @staticmethod
    def pick_for_reply(due: list[Experience], rng: random.Random) -> Experience | None:
        if not due or rng.random() >= SHARE_PROBABILITY:
            return None
        return rng.choice(due)


def mentions(text: str, content: str, threshold: float = 0.2) -> bool:
    """回复里有没有真的提起这件事（用字二元组重叠判断）。"""
    from .memory import char_bigrams, relevance

    return relevance(char_bigrams(text), char_bigrams(content)) >= threshold
