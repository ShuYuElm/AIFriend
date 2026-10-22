"""到点事件：承诺、生日、纪念日。

心跳每 90 秒问一次"现在有没有到点的事"：
  - 承诺：记忆里 kind='promise' 且 due_at 到了（24 小时内有效，避免补发上周的）
  - 生日：user_profile 的 dates.his_birthday 是今天
  - 纪念日：认识天数正好是 100 / 200 / 365 / 500 / 730 / 1000 天

到点的消息优先级最高：跳过静默期和"这个时段发过了"，
但仍受每日上限和"连发未回"约束——不然她会变成闹钟。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .db import Database
from .persona import PersonaStore
from .profile import ProfileStore

logger = logging.getLogger("aigirl.events")

MILESTONES = {100, 200, 300, 365, 500, 730, 1000}
LOOKBACK = timedelta(hours=24)
MAX_TRIES = 2


@dataclass(slots=True)
class DueEvent:
    kind: str          # promise / birthday / anniversary
    text: str
    memory_id: int | None = None

    def slot(self, day: str) -> str:
        return f"{self.kind}:{day}"


def _matches_month_day(today: date, spec: str) -> bool:
    """生日支持 07-07 或 7-7。"""
    parts = str(spec).strip().replace("/", "-").split("-")
    if len(parts) < 2:
        return False
    try:
        month, day = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return today.month == month and today.day == day


class EventPlanner:
    def __init__(
        self,
        db: Database,
        profile_store: ProfileStore,
        persona_store: PersonaStore,
        tz: str = "Asia/Shanghai",
    ) -> None:
        self.db = db
        self.profile_store = profile_store
        self.persona_store = persona_store
        self.tz = tz

    async def due(self, now: datetime) -> list[DueEvent]:
        events: list[DueEvent] = []
        day = now.date().isoformat()

        rows = await self.db.promises_due(
            now.isoformat(timespec="minutes"),
            (now - LOOKBACK).isoformat(timespec="minutes"),
        )
        for row in rows:
            events.append(DueEvent("promise", str(row["content"]), int(row["id"])))

        dates = self.profile_store.load().dates
        birthday = dates.get("his_birthday")
        if birthday and _matches_month_day(now.date(), birthday):
            events.append(DueEvent("birthday", "今天是他的生日"))

        known_since = getattr(self.persona_store.load(), "known_since", "") or ""
        try:
            since = date.fromisoformat(str(known_since)[:10])
        except ValueError:
            since = None
        if since:
            days = (now.date() - since).days
            if days in MILESTONES:
                events.append(DueEvent("anniversary", f"你们认识第 {days} 天"))

        # 生日/纪念日一天只发一次；承诺发完就归档，不重复
        fresh: list[DueEvent] = []
        for event in events:
            if event.kind != "promise" and await self.db.has_proactive_slot(
                "event", event.slot(day)
            ):
                continue
            # 她已经拒绝过两次，就别再问了——否则每 90 秒重试一次，一次调用
            if await self.db.get_state(self._try_key(event.kind, day), 0) >= MAX_TRIES:
                continue
            fresh.append(event)
        return fresh

    @staticmethod
    def _try_key(kind: str, day: str) -> str:
        return f"event_try:{kind}:{day}"

    async def record_decline(self, now: datetime, events: list[DueEvent]) -> None:
        """模型选择不发时记一笔；攒到 MAX_TRIES 就当天不再问。"""
        day = now.date().isoformat()
        for kind in {event.kind for event in events}:
            key = self._try_key(kind, day)
            tries = int(await self.db.get_state(key, 0)) + 1
            await self.db.set_state(key, tries)
            if tries >= MAX_TRIES:
                logger.info("%s 今天已被拒绝 %d 次，不再追问", kind, tries)

    async def done(self, now: datetime, events: list[DueEvent]) -> None:
        day = now.date().isoformat()
        for event in events:
            if event.memory_id is not None:
                await self.db.archive_memory(event.memory_id)
            else:
                await self.db.log_proactive("event", event.slot(day), now.isoformat(), None)

    @staticmethod
    def block(events: list[DueEvent]) -> str:
        if not events:
            return ""
        lines = ["## 到点了（这些是你答应过或该记得的事，先用一句话兑现）", ""]
        for event in events:
            if event.kind == "birthday":
                lines.append("- 今天是他的生日，说句生日快乐，可以带一句你准备的什么")
            elif event.kind == "anniversary":
                lines.append(f"- {event.text}，提一句")
            else:
                # 记忆内容常写成"答应他明早七点半叫他起床"，别拼成"你答应过他：答应他…"
                text = event.text
                for prefix in ("答应过他", "答应他", "答应", "承诺"):
                    if text.startswith(prefix):
                        text = text[len(prefix):].lstrip("：: ，,")
                        break
                lines.append(f"- 你答应过他：{text}")
        return "\n".join(lines)
