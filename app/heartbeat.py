"""心跳：每 90 秒醒来一次，决定行动或不行动。

一次心跳做五件事：
  1. 看此刻在哪个时段窗
  2. 过闸门（静默期 / 日上限 / 连续未回 / 最小间隔 / 刚聊完 / 该 slot 已发）
  3. 算动机分（时段基础 + 久未联系加成 - 主动过热惩罚）
  4. 过阈值才让模型决定"发什么"，模型也可以选 send:false
  5. 记录每一次决策（含跳过），否则没法调"克制"
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from .config import (
    DAILY_PROACTIVE_CAP,
    HEARTBEAT_INTERVAL_S,
    MIN_PROACTIVE_GAP_MIN,
    MOTIVATION_THRESHOLD,
    UNANSWERED_STOP,
    Settings,
)
from .db import Database
from .events import DueEvent, EventPlanner
from .experiences import Experience, ExperienceQueue, mentions
from .life import Life
from .llm import LLMError, LLMProvider
from .memory import Memory, render
from .models import Bubble
from .persona import PersonaStore, format_gap, now_local
from .profile import ProfileStore
from .style import delay_for, split_bubbles
from .timeutil import align
from .world import World

logger = logging.getLogger("aigirl.heartbeat")

# (kind, 开始分钟, 结束分钟)
WINDOWS = (
    ("morning", 7 * 60 + 30, 9 * 60),
    ("noon", 11 * 60 + 30, 13 * 60),
    ("evening", 19 * 60 + 30, 22 * 60),
    ("night", 22 * 60 + 30, 24 * 60),
)
BASE_MOTIVATION = {"morning": 0.5, "noon": 0.2, "evening": 0.5, "night": 0.4, "idle": 0.1}
RECENT_CHAT_MIN = 20

OnMessage = Callable[[list[Bubble], str], Awaitable[None]]


# ------------------------------------------------------------------ 纯函数

def window_kind(now: datetime) -> str:
    minutes = now.hour * 60 + now.minute
    for kind, start, end in WINDOWS:
        if start <= minutes < end:
            return kind
    return "idle"


def _minutes_since(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return max(0.0, (now - align(then, now)).total_seconds() / 60)


@dataclass(slots=True)
class TickContext:
    now: datetime
    window: str
    quiet: bool
    sent_today: int
    minutes_since_proactive: float | None
    minutes_since_user: float | None
    unanswered: int
    fresh_experience: bool = False
    urgent: bool = False


def check_gates(ctx: TickContext, *, cap: int = DAILY_PROACTIVE_CAP) -> tuple[str, str] | None:
    """返回第一个不通过的闸门；全通过返回 None。

    到点事件（承诺/生日）只让过静默期和"刚聊完"这两道——
    每日上限和"连发没回"仍然管着，不然她会变成闹钟。
    """
    if ctx.quiet and not ctx.urgent:
        return ("quiet", "静默时段")
    if ctx.sent_today >= cap:
        return ("cap", f"今天已主动 {ctx.sent_today} 条")
    if ctx.unanswered >= UNANSWERED_STOP:
        return ("unanswered", "连着发了没回，今天不发了")
    if ctx.minutes_since_proactive is not None and ctx.minutes_since_proactive < MIN_PROACTIVE_GAP_MIN:
        return ("gap", f"离上一条只有 {int(ctx.minutes_since_proactive)} 分钟")
    if ctx.minutes_since_user is not None and ctx.minutes_since_user < RECENT_CHAT_MIN:
        return ("recent", "刚聊完")
    return None


def motivation(ctx: TickContext) -> float:
    if ctx.urgent:
        return 1.0
    base = BASE_MOTIVATION.get(ctx.window, 0.1)
    idle_hours = (ctx.minutes_since_user or 24 * 60) / 60
    idle_bonus = min(0.45, 0.15 * int(idle_hours // 6))
    overheat = 0.15 * ctx.sent_today
    fresh = 0.35 if ctx.fresh_experience else 0.0
    return round(base + idle_bonus + fresh - overheat, 3)


def parse_decision(raw: str) -> dict | None:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("send"), bool):
        return None
    bubbles = [str(b).strip() for b in (data.get("bubbles") or []) if str(b).strip()]
    return {
        "send": data["send"],
        "bubbles": bubbles[:2],
        "reason": str(data.get("reason") or "").strip()[:80],
    }


def build_prompt(
    *,
    name: str,
    now: datetime,
    life_state: str,
    gap_text: str,
    sent_today: int,
    memory_block: str,
    profile_block: str,
    experience_block: str = "",
    world_block: str = "",
    event_block: str = "",
) -> str:
    extra = "\n\n".join(
        part for part in (profile_block, memory_block, experience_block, event_block) if part
    )
    world = f"\n外面：{world_block}" if world_block else ""
    repeat = (
        "（今天已经找过了，除非真有非说不可的，这次就该是不发）" if sent_today >= 1 else ""
    )
    return f"""【任务：主动消息】

你是{name}。现在是 {now:%H:%M}，你{life_state}。{world}
{gap_text}。
今天你已经主动找过他 {sent_today} 次。{repeat}

{extra}

你要决定：现在要不要给他发消息，发什么。

只输出 JSON，不要解释：
{{"send": true, "bubbles": ["第一句", "第二句"], "reason": "为什么现在想说"}}
或者
{{"send": false, "reason": "现在没什么想说的"}}

规则：
- 最多两条，每条 5–25 字
- 别用"在吗""在干嘛""吃了吗"这种空话开头
- 可以带出你此刻正在干什么
- 不要提"主动""推送""打扰"这类词
- 如果有「到点了」那一段，必须先兑现它，send 必须为 true，别扯别的
- **"天气变了""今天有件小事""好久没聊了"都不算理由**——这些你心里想想就够了，不用发
- 只有真的想到他、有件事非说不可、或者想问一句才发；拿不准就选 send: false
- send: false 很正常，不代表你不想他
"""


# ------------------------------------------------------------------ 心跳

class Heartbeat:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        provider: LLMProvider,
        persona_store: PersonaStore,
        profile_store: ProfileStore,
        life: Life,
        memory: Memory,
        experiences: ExperienceQueue,
        world: World | None = None,
        events: EventPlanner | None = None,
        on_message: OnMessage | None = None,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.provider = provider
        self.persona_store = persona_store
        self.profile_store = profile_store
        self.life = life
        self.memory = memory
        self.experiences = experiences
        self.world = world
        self.events = events or EventPlanner(db, profile_store, persona_store, settings.tz)
        self.on_message = on_message
        self.clock = clock or (lambda: now_local(settings.tz))
        self.rng = rng or random.Random()
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------- 跨天

    async def _rollover_if_new_day(self, now: datetime, today: str) -> None:
        """日期变了就翻篇：清掉昨天的未回复计数。

        经历的生成不放在这里——凌晨 0 点翻篇时 ensure_today 会因为"太早"直接返回 0，
        之后再没人补，整天就没有新经历。改成每跳都问一次（它自己保证一天只生成一次）。
        """
        last = await self.db.get_state("last_seen_date")
        if last == today:
            return
        await self.db.set_state("last_seen_date", today)
        if last is None:
            return
        await self.db.set_state("proactive_unanswered", 0)
        logger.info("新的一天 %s", today)

        # 每天做一次：给昨天写日记、把没用的记忆归档
        try:
            await self.memory.summarize_day()
        except Exception as exc:  # 日记失败不能拖垮心跳
            logger.warning("自动日记失败：%s", exc)
        try:
            forgotten = await self.memory.forget_stale()
            if forgotten:
                logger.info("归档了 %d 条久未使用的低价值记忆", forgotten)
        except Exception as exc:
            logger.warning("遗忘流程失败：%s", exc)

    # ---------------------------------------------------------- 单次心跳

    async def tick(self, force: bool = False) -> dict:
        now = self.clock()
        persona = self.persona_store.load()
        today = now.date().isoformat()
        window = window_kind(now)
        slot = f"{window}:{today}" if window != "idle" else f"idle:{today}:{now.hour:02d}"

        await self._rollover_if_new_day(now, today)
        await self.experiences.ensure_today(now)

        events = await self.events.due(now)
        due = await self.experiences.due(now)

        ctx = TickContext(
            now=now,
            window=window,
            quiet=self.life.is_quiet(now),
            sent_today=await self.db.count_proactive(today),
            minutes_since_proactive=_minutes_since(await self.db.last_proactive_at(), now),
            minutes_since_user=_minutes_since(await self.db.last_user_at(), now),
            unanswered=int(await self.db.get_state("proactive_unanswered", 0)),
            fresh_experience=bool(due),
            urgent=bool(events),
        )
        score = motivation(ctx)

        gate = check_gates(ctx)
        if gate and not force:
            await self.db.log_heartbeat(now.isoformat(), "skipped", gate[1], score, gate[0])
            return {"action": "skipped", "gate": gate[0], "reason": gate[1], "motivation": score}

        if await self.db.has_proactive_slot(window, slot) and not force and not events:
            reason = "这个时段已经发过了"
            await self.db.log_heartbeat(now.isoformat(), "skipped", reason, score, "slot")
            return {"action": "skipped", "gate": "slot", "reason": reason, "motivation": score}

        if score < MOTIVATION_THRESHOLD and not force:
            reason = "没什么想说的"
            await self.db.log_heartbeat(now.isoformat(), "skipped", reason, score, "motivation")
            return {"action": "skipped", "gate": "motivation", "reason": reason, "motivation": score}

        decision = await self._decide(now, ctx, due, events)
        if decision is None:
            reason = "模型没给出可用决定"
            await self.db.log_heartbeat(now.isoformat(), "skipped", reason, score, "llm")
            return {"action": "skipped", "gate": "llm", "reason": reason, "motivation": score}

        if not decision["send"] or not decision["bubbles"]:
            reason = decision["reason"] or "她有话但没说"
            await self.events.record_decline(now, events)
            await self.db.log_heartbeat(now.isoformat(), "skipped", reason, score, "self")
            return {"action": "skipped", "gate": "self", "reason": reason, "motivation": score}

        bubbles = [
            Bubble(text=part, delay_ms=delay_for(part, self.rng))
            for part in split_bubbles("\n".join(decision["bubbles"]), max_bubbles=2)
        ]
        message_id = await self.db.insert_message(
            "assistant",
            "\n".join(b.text for b in bubbles),
            "cli",
            now.isoformat(),
            meta={"proactive": window, "reason": decision["reason"]},
        )
        await self.db.log_proactive(window, slot, now.isoformat(), message_id)
        await self.db.set_state("proactive_unanswered", ctx.unanswered + 1)
        await self.events.done(now, events)
        if due and mentions("\n".join(b.text for b in bubbles), due[0].content):
            await self.experiences.mark_shared([due[0].id], now)
        await self.db.log_heartbeat(now.isoformat(), "sent", decision["reason"], score, "")
        logger.info("主动消息发出：%s", decision["reason"])

        if self.on_message:
            await self.on_message(bubbles, window)
        return {
            "action": "sent",
            "bubbles": [b.text for b in bubbles],
            "reason": decision["reason"],
            "motivation": score,
            "events": [e.kind for e in events],
        }

    async def _decide(
        self,
        now: datetime,
        ctx: TickContext,
        due: list[Experience],
        events: list[DueEvent] | None = None,
    ) -> dict | None:
        persona = self.persona_store.load()
        state = self.life.state_for_message(now)
        gap_text = format_gap(
            None if ctx.minutes_since_user is None else ctx.minutes_since_user * 60, now
        )
        profile = self.profile_store.load()
        profile_block = "\n".join(f"- {fact}" for fact in profile.facts[:10])
        if profile_block:
            profile_block = "## 关于他\n\n" + profile_block

        recent = await self.db.recent_messages(6, roles=("user", "assistant"))
        last_user_text = next((m.content for m in reversed(recent) if m.role == "user"), "")
        recalled = await self.memory.recall(last_user_text or "他")
        memory_block = render(recalled[:4], now)

        experience_block = ""
        if due:
            experience_block = "## 今天你自己经历的事（挑一件自然说出来，别全说）\n" + "\n".join(
                f"- {item.content}" for item in due[:2]
            )

        prompt = build_prompt(
            name=persona.name,
            now=now,
            life_state=state.describe(),
            gap_text=gap_text,
            sent_today=ctx.sent_today,
            memory_block=memory_block,
            profile_block=profile_block,
            experience_block=experience_block,
            world_block=await self.world.line(now) if self.world else "",
            event_block=EventPlanner.block(events or []),
        )

        try:
            raw = await self.provider.chat(
                [{"role": "system", "content": prompt}], temperature=0.9, max_tokens=200
            )
        except LLMError as exc:
            logger.warning("主动消息生成失败：%s", exc)
            return None
        return parse_decision(raw)

    # ---------------------------------------------------------- 循环

    async def run(self, interval: float = HEARTBEAT_INTERVAL_S) -> None:
        """先等一个周期再跳：否则每次启动都会立刻发一条，太急。"""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # 心跳绝不能停
                logger.exception("心跳异常")

    def start(self, interval: float = HEARTBEAT_INTERVAL_S) -> asyncio.Task:
        self._task = asyncio.create_task(self.run(interval))
        return self._task

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
