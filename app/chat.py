"""ChatEngine：一条消息的完整编排。

通道无关：不 import fastapi，也不 import 任何 adapter。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any, Callable

import yaml

from .admin import AdminCommands
from .config import (
    EXTRACT_EVERY_N_USER_MSGS,
    MAX_TOKENS,
    RECENT_CHARS,
    RECENT_MESSAGES,
    RECENT_MIN_MESSAGES,
    ROLLUP_EVERY_N_USER_MSGS,
    TEMPERATURE,
    Settings,
)
from .db import Database
from .experiences import ExperienceQueue, mentions
from .life import Life
from .llm import LLMError, LLMProvider
from .lorebook import Lorebook
from .memory import Memory
from .models import Bubble
from .mood import MoodTracker
from .persona import PersonaStore, RuntimeContext, build_p1, build_p5, now_local
from .profile import ProfileStore, build_block as build_profile_block
from .prompts import STATE_KEY as PROMPT_STATE_KEY
from .prompts import PromptPacks
from .search import SearchClient
from .style import delay_for, split_bubbles
from .world import World, load_world_config

logger = logging.getLogger("aigirl.chat")

FALLBACK_REPLY = "等会儿说，我这边有点事"

# 只有他问到"今天/怎么样/在干嘛"这类，才把今天的经历说出来
SHARE_TOPIC = re.compile(r"今天|怎么样|在干嘛|干嘛呢|忙什么|忙啥|做什么|过的")


def trim_window(messages: list[Any], chars: int = RECENT_CHARS, floor: int = RECENT_MIN_MESSAGES) -> list[Any]:
    """按条数取出后再按字数裁一次：长消息不会把窗口撑爆。

    从最旧的开始丢，但不低于 floor 条——太少了上下文就断了。
    """
    kept = list(messages)
    total = sum(len(m.content) for m in kept)
    while len(kept) > floor and total > chars:
        total -= len(kept[0].content)
        kept.pop(0)
    return kept


class ChatEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        provider: LLMProvider,
        db: Database,
        persona_store: PersonaStore,
        profile_store: ProfileStore,
        lorebook: Lorebook,
        life: Life,
        rng: random.Random | None = None,
        search: SearchClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.db = db
        self.persona_store = persona_store
        self.profile_store = profile_store
        self.lorebook = lorebook
        self.life = life
        self.rng = rng or random.Random()
        self.clock = clock or (lambda: now_local(settings.tz))
        self._book: dict[str, Any] | None = None
        self.memory = Memory(db, provider, settings.tz)
        self.experiences = ExperienceQueue(db, life, settings.tz, self.rng)
        self.mood = MoodTracker(db, settings.tz)
        self.packs = PromptPacks(settings.prompts_path)
        self.admin = AdminCommands(
            db, persona_store, profile_store, settings.persona_path, settings.profile_path
        )
        self.world = World(db, load_world_config(settings.world_path), settings.tz)
        self.search = search or SearchClient(
            db,
            settings.llm_base_url,
            settings.llm_api_key,
            settings.tz,
            enabled=settings.search_enabled,
        )
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- 启动

    async def startup(self) -> None:
        persona = self.persona_store.load()
        self._book = persona.book
        now = self.clock().isoformat()

        logger.info("人设卡：%s（%s）", self.settings.persona_path.name, persona.name)

        await self.world.warm()

        rows = self._load_seed_rows()
        if rows:
            inserted = await self.db.seed_memories(rows, now)
            if inserted:
                logger.info("写入初始记忆 %d 条", inserted)

        if await self.db.get_state("first_seen") is None:
            await self.db.set_state("first_seen", now)
        if await self.db.get_state("relationship_stage") is None:
            await self.db.set_state("relationship_stage", persona.stage)

        await self.experiences.ensure_today(self.clock())
        await self.memory.backfill_episodes()

    def _load_seed_rows(self) -> list[dict[str, Any]]:
        path = self.settings.seed_path
        if not path.exists():
            return []
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            logger.warning("seed_memories 解析失败：%s", exc)
            return []
        rows: list[dict[str, Any]] = []
        for item in raw.get("memories") or []:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            rows.append(
                {
                    "kind": str(item.get("kind") or "fact"),
                    "topic": item.get("topic"),
                    "content": str(item["content"]).strip(),
                    "importance": int(item.get("importance", 3)),
                    "confidence": float(item.get("confidence", 0.9)),
                    "event_time": item.get("event_time"),
                    "entities": [str(e) for e in (item.get("entities") or [])],
                }
            )
        return rows

    # ---------------------------------------------------------------- 主流程

    async def handle(self, text: str, channel: str = "cli") -> list[Bubble]:
        text = (text or "").strip()
        if not text:
            return []

        # 管理指令（/persona、/profile、/forget…）不经过模型，也不进聊天历史
        if AdminCommands.is_command(text):
            reply = await self.admin.run(text)
            if reply is not None:
                return [Bubble(reply, 200)]

        persona = self.persona_store.load()
        now = self.clock()
        recent = trim_window(await self.db.recent_messages(RECENT_MESSAGES))

        gap: float | None = None
        last_user = await self.db.last_user_at()
        if last_user:
            try:
                gap = max(0.0, (now - datetime.fromisoformat(last_user)).total_seconds())
            except ValueError:
                gap = None

        stage = await self.db.get_state("relationship_stage") or persona.stage
        life_state = self.life.state_for_message(now)

        await self.db.insert_message("user", text, channel, now.isoformat())
        await self.db.set_state("proactive_unanswered", 0)

        scan = text + " " + " ".join(m.content for m in recent[-3:])
        entries = self.lorebook.match(scan)

        ctx = RuntimeContext(
            now=now,
            gap_seconds=gap,
            stage=str(stage),
            life_state=life_state.state,
            life_detail=life_state.detail,
            days_known=self._days_known(persona, now),
            world=await self.world.line(now),
        )

        # 她此刻对他的情绪（规则驱动，不看模型）
        mood = await self.mood.observe(text, (gap or 0) / 3600 if gap else None, now)
        mood_line = self.mood.render(mood)

        p1 = build_p1(persona, ctx)
        if mood_line:
            p1 += "\n" + mood_line
        pack_name = await self.db.get_state(PROMPT_STATE_KEY) or self.packs.default_active()
        pack_text = self.packs.render(str(pack_name))
        if pack_text:
            p1 += "\n\n" + pack_text

        messages: list[dict[str, str]] = [{"role": "system", "content": p1}]

        profile_block = build_profile_block(self.profile_store.load())
        if profile_block:
            messages.append({"role": "system", "content": profile_block})

        block = Lorebook.format(entries)
        if block:
            messages.append({"role": "system", "content": block})

        # 记忆和联网并行，别串行等两次 IO
        memory_block, search_block = await asyncio.gather(
            self.memory.block(text),
            self.search.for_message(text, now),
        )
        if memory_block:
            messages.append({"role": "system", "content": memory_block})

        # 延迟分享：今天有早就过去的事，且他正好问起，才提
        due = await self.experiences.due(now)
        picked = self.experiences.pick_for_reply(due, self.rng)
        if picked and not SHARE_TOPIC.search(text):
            picked = None

        messages += [
            {"role": m.role, "content": m.content}
            for m in recent
            if m.role in ("user", "assistant")
        ]

        p5 = build_p5(persona)
        if picked:
            p5 += (
                "\n\n他问的正是你今天怎么样，把这件事说给他听，一句话讲完，别解释前因后果："
                f"\n{picked.content}"
            )
        if search_block:
            p5 += "\n\n" + search_block
        messages.append({"role": "system", "content": p5})
        messages.append({"role": "user", "content": text})

        try:
            reply = await self.provider.chat(
                messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS
            )
        except LLMError as exc:
            logger.warning("模型调用失败，返回兜底：%s", exc)
            return [Bubble(FALLBACK_REPLY, 600)]

        if not (reply or "").strip():
            logger.warning("模型返回空内容")
            return [Bubble(FALLBACK_REPLY, 600)]

        bubbles = [
            Bubble(text=part, delay_ms=delay_for(part, self.rng))
            for part in split_bubbles(reply)
        ]
        if not bubbles:
            return [Bubble(FALLBACK_REPLY, 600)]

        joined = "\n".join(b.text for b in bubbles)
        if picked and mentions(joined, picked.content):
            await self.experiences.mark_shared([picked.id], now)

        meta = {
            "model": self.provider.model,
            "mock": getattr(self.provider, "name", "") == "mock",
            "lorebook": [e.content for e in entries],
            "stage": str(stage),
            "life": life_state.state,
            "bubbles": len(bubbles),
        }
        await self.db.insert_message("assistant", joined, channel, now.isoformat(), meta=meta)
        await self._maybe_extract()
        await self._maybe_rollup()
        return bubbles

    @staticmethod
    def _days_known(persona: Any, now: datetime) -> int:
        since = str(getattr(persona, "known_since", "") or "")[:10]
        try:
            start = datetime.fromisoformat(since).date()
        except ValueError:
            return 0
        return max(1, (now.date() - start).days + 1)

    # ---------------------------------------------------------------- 记忆

    async def _maybe_extract(self) -> None:
        """每 N 轮用户消息触发一次抽取；后台跑，不阻塞回复。"""
        count = int(await self.db.get_state("msgs_since_extract", 0)) + 1
        if count < EXTRACT_EVERY_N_USER_MSGS:
            await self.db.set_state("msgs_since_extract", count)
            return
        await self.db.set_state("msgs_since_extract", 0)
        recent = await self.db.recent_messages(12)
        self._spawn(self.memory.extract(recent))

    async def _maybe_rollup(self) -> None:
        """每 M 轮做一次滚动归纳，把快滑出窗口的对话收成一段笔记。"""
        count = int(await self.db.get_state("msgs_since_rollup", 0)) + 1
        if count < ROLLUP_EVERY_N_USER_MSGS:
            await self.db.set_state("msgs_since_rollup", count)
            return
        await self.db.set_state("msgs_since_rollup", 0)
        self._spawn(self.memory.rollup())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """等后台任务（抽取、归纳）跑完，退出前调用。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def summarize_day(self, day: str | None = None) -> bool:
        return await self.memory.summarize_day(day or (self.clock().date() - timedelta(days=1)).isoformat())
