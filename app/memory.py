"""记忆：抽取 / 召回 / 时间推理 / 摘要。

设计要点：
  - 只追加：新增记忆不覆盖旧记忆；冲突靠 superseded_by 打标记，旧记忆仍可被召回并显示"（以前）"
  - 召回：字二元组相似度 + 实体命中 + 重要度 + 新鲜度
  - 不做向量库：单用户规模下，全量扫描 + bigram 打分足够快，省掉一个依赖
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .config import (
    FORGET_DAYS,
    FORGET_MAX_IMPORTANCE,
    MEMORY_MAX_CHARS,
    RECALL_TOP_K,
    RECENCY_HALF_LIFE_DAYS,
    ROLLUP_KEEP,
    ROLLUP_MAX_CHARS,
    ROLLUP_MAX_MESSAGES,
)
from .db import Database
from .llm import LLMError, LLMProvider
from .models import MemoryItem, Message
from .persona import WEEKDAYS, now_local

logger = logging.getLogger("aigirl.memory")

# 他提到过去的事时，才去回读原话
REFERENCE_RE = re.compile(r"上次|上回|之前说|你说过|我说过|还记得|记得吗|那个事|那天|我跟你说")
DETAIL_TOP = 2
DETAIL_SPAN = 2
DETAIL_CHARS = 70

# 核心层：重要到不需要召回的记忆
CORE_MIN_IMPORTANCE = 4
CORE_KINDS = ("relation", "event", "preference", "self")
CORE_LIMIT = 6
CORE_MAX_CHARS = 240

EXTRACT_MARKER = "【任务：记忆抽取】"
SUMMARY_MARKER = "【任务：写日记】"
ROLLUP_MARKER = "【任务：归纳这段对话】"

KINDS = {"fact", "preference", "event", "relation", "promise", "emotion", "self"}
TOPIC_HINT = "身份/住处/工作学业/作息/健康/喜好/厌恶/关系/家庭/计划/其他"

MAX_PER_EXTRACTION = 5
MIN_CONFIDENCE = 0.6
MIN_RECALL_SCORE = 0.22


# ------------------------------------------------------------------ 相似度

def char_bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def relevance(query: set[str], doc: set[str]) -> float:
    if not query or not doc:
        return 0.0
    return len(query & doc) / math.sqrt(len(query) * len(doc))


# 判"是不是同一件事"：字面相似度在这套数据上不够用
# （"论文已接近完成" vs "论文真收尾了" 只有 0.148），所以实体命中优先。
SAME_EVENT_SIM = 0.25


def find_same_event(item: dict[str, Any], pool: list[MemoryItem]) -> MemoryItem | None:
    """在已有记忆里找"同一件事"。同类型 + （实体有交集 或 字面够像）。"""
    kind = item.get("kind")
    entities = {str(e).casefold() for e in (item.get("entities") or []) if str(e).strip()}
    grams = char_bigrams(item.get("content") or "")

    best: MemoryItem | None = None
    best_score = 0.0
    for other in pool:
        # 被取代的旧记忆也可以并进来：归并说的是"同一个人同一件事"，
        # 跟"哪条更可信"不是一回事，渲染时旧的那条仍然标（以前）。
        if other.kind != kind:
            continue
        shared = entities & {str(e).casefold() for e in (other.entities or [])}
        sim = relevance(grams, char_bigrams(other.content))
        if not shared and sim < SAME_EVENT_SIM:
            continue
        weight = sim + 0.2 * len(shared)
        if weight > best_score:
            best, best_score = other, weight
    return best


def recency(event_time: str | None, created_at: str, now) -> float:
    stamp = event_time or created_at
    try:
        day = date.fromisoformat(str(stamp)[:10])
    except (ValueError, TypeError):
        return 0.5
    days = max(0, (now.date() - day).days)
    return 0.5 ** (days / RECENCY_HALF_LIFE_DAYS)


def score(item: MemoryItem, query_bg: set[str], query_text: str, now) -> float:
    rel = relevance(query_bg, char_bigrams(item.content))
    entity_hit = 0.0
    if item.entities:
        hits = sum(1 for name in item.entities if name and name in query_text)
        entity_hit = hits / len(item.entities)
    return (
        0.45 * rel
        + 0.25 * entity_hit
        + 0.15 * (item.importance / 5)
        + 0.15 * recency(item.event_time, item.created_at, now)
    )


def age_label(item: MemoryItem, now) -> str:
    stamp = item.event_time or item.created_at
    try:
        day = date.fromisoformat(str(stamp)[:10])
    except (ValueError, TypeError):
        return ""
    days = (now.date() - day).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 30:
        return f"{days}天前"
    months = max(1, days // 30)
    return f"{months}个月前"


def collapse_by_episode(items: Iterable[MemoryItem]) -> list[MemoryItem]:
    """同一个 episode 只留一条：优先留没有被取代的那条。

    "他在赶论文"和"论文收尾了"是同一个人同一件事的两次提及，
    并列注入只会让她重复问同一个问题；留旧版本则会让她以为还没收尾。
    """
    items = list(items)
    rep: dict[int, MemoryItem] = {}
    for item in items:
        if item.episode_id is None:
            continue
        current = rep.get(item.episode_id)
        if current is None or (current.is_old and not item.is_old):
            rep[item.episode_id] = item

    out: list[MemoryItem] = []
    emitted: set[int] = set()
    for item in items:
        if item.episode_id is None:
            out.append(item)
            continue
        if item.episode_id in emitted:
            continue
        emitted.add(item.episode_id)
        out.append(rep[item.episode_id])
    return out


def render_core(items: Iterable[MemoryItem]) -> str:
    """核心层：不依赖查询、每轮都在场的关系骨架。"""
    items = list(items)
    if not items:
        return ""
    lines = [
        "## 你一定记得的事（你们之间的，别主动复述）",
        "",
    ]
    lines.extend(f"- {item.content}" for item in items)
    lines.append("")
    lines.append("这些事不用主动提，和话题有关的时候自然会想起来。")
    return "\n".join(lines)


def render(items: Iterable[MemoryItem], now) -> str:
    items = list(items)
    if not items:
        return ""
    lines = [
        "## 你记得的事（不用说明来源，自然地带出来）",
        "",
    ]
    for item in items:
        if item.is_old:
            lines.append(f"- （以前）{item.content}")
            continue
        age = age_label(item, now)
        note = f"（这事他提过 {item.episode_count} 次）" if item.repeated else ""
        lines.append(f"- ({age}) {item.content}{note}" if age else f"- {item.content}{note}")
    if any(item.repeated for item in items):
        lines.append("")
        lines.append("括号里的次数只是让你知道这事对他有多重要，别把次数说出来。")
    return "\n".join(lines)


# ------------------------------------------------------------------ 抽取

def build_extract_prompt(
    existing: list[MemoryItem], transcript: str, now: datetime | None = None
) -> str:
    known = "\n".join(f"[{m.id}] {m.content}" for m in existing) or "（还没有）"
    clock = (
        f"现在是 {now:%Y-%m-%d} 周{WEEKDAYS[now.weekday()]} {now:%H:%M}。\n\n"
        if now is not None
        else ""
    )
    return f"""{EXTRACT_MARKER}

从下面的对话里，抽取"值得长期记住"的信息。

{clock}只记这些：
- fact：关于他的稳定事实（身份、住处、工作、学业、健康、家人）
- preference：他明确喜欢或讨厌的东西
- event：发生过、以后可能还会提的事
- relation：他和某个人的关系
- promise：**她（你）答应了要为他做的事**，到点要被提醒的那种
- emotion：他对某件事的稳定态度（不是一时的情绪）

不要记：寒暄、语气词、一次性的情绪、你猜的、重复的。
注意：**他答应你的事不算 promise**——那种记成 fact，content 写清"他答应了什么"。

已有记忆（不要重复添加；如果是同一个人/同一件事的新版本，用 supersedes 指向旧编号）：
{known}

只输出 JSON 数组，不要任何解释：
[{{"kind":"promise","topic":"作息","content":"答应他明早七点半叫他起床","importance":4,"confidence":0.9,"due_at":"2026-10-21T07:30","entities":["叫起床"],"supersedes":[]}}]

规则：
- content 一条只说一件事，具体、可检验，不超过 40 字，用"他"称呼
- topic 从这些里选：{TOPIC_HINT}
- importance：5=身份级（名字、职业、生日、雷区），3=普通，1=随口一提
- event_time 只在能确定日期时填 YYYY-MM-DD，否则省略
- promise **必须尽量填 due_at**（YYYY-MM-DDTHH:MM，24 小时制）：根据上面"现在是"推算，
  "明早七点半"→ 明天 07:30，"今晚"→ 今天 21:00，"周末"→ 本周六 20:00。实在推不出才省略
- supersedes 只在确实"取代"旧记忆时填旧编号，否则空数组
- 最多 {MAX_PER_EXTRACTION} 条；没有值得记的就输出 []

对话：
{transcript}
"""


def parse_extraction(raw: str) -> list[dict[str, Any]]:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if len(content) < 2:
            continue
        content = content[:MEMORY_MAX_CHARS]
        kind = str(item.get("kind") or "fact")
        if kind not in KINDS:
            kind = "fact"
        try:
            importance = max(1, min(5, int(item.get("importance", 3))))
        except (TypeError, ValueError):
            importance = 3
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
        except (TypeError, ValueError):
            confidence = 0.8
        entities = [str(e).strip() for e in (item.get("entities") or []) if str(e).strip()][:4]
        supersedes = []
        for value in item.get("supersedes") or []:
            try:
                supersedes.append(int(value))
            except (TypeError, ValueError):
                continue
        event_time = item.get("event_time")
        event_time = str(event_time)[:10] if event_time else None
        due_at = item.get("due_at")
        due_at = str(due_at)[:16] if due_at else None
        out.append(
            {
                "kind": kind,
                "topic": str(item.get("topic") or "").strip() or None,
                "content": content,
                "importance": importance,
                "confidence": confidence,
                "entities": entities,
                "supersedes": supersedes,
                "event_time": event_time,
                "due_at": due_at,
            }
        )
        if len(out) >= MAX_PER_EXTRACTION:
            break
    return out


def build_summary_prompt(transcript: str) -> str:
    return f"""{SUMMARY_MARKER}

把下面这段对话压缩成一段不超过 150 字的中文日记，用第三人称。

只写：今天发生了什么、他提到的重要的事、你答应过什么。
不要评价，不要写"他们聊得很开心"这种空话，不要分点。

对话：
{transcript}
"""


def build_rollup_prompt(transcript: str) -> str:
    return f"""{ROLLUP_MARKER}

把下面这段对话收拢成一段不超过 {ROLLUP_MAX_CHARS} 字的笔记，用第三人称，写给"几天后再看"的自己。

要保留：
- 他最近在做的事、遇到的麻烦
- 他的状态和心情（只写能看出来的，别猜）
- 没聊完的话题、约好要做的事
- 你们聊到过的具体人和事（名字要留）

不要：
- 逐条复述对话
- 评价"聊得很愉快"这种空话
- 分点、标题

对话：
{transcript}
"""


# ------------------------------------------------------------------ 服务

class Memory:
    def __init__(self, db: Database, provider: LLMProvider, tz: str = "Asia/Shanghai") -> None:
        self.db = db
        self.provider = provider
        self.tz = tz

    async def _complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        thinking: bool = False,
    ) -> str:
        """结构化任务用的调用封装。

        思考模式会先输出思维链，如果 max_tokens 不够，正文会是空的。
        这里兜一层：拿到空内容就关掉思考重试一次。
        """
        messages = [{"role": "system", "content": prompt}]
        text = await self.provider.chat(
            messages, temperature=temperature, max_tokens=max_tokens, thinking=thinking
        )
        if thinking and not (text or "").strip():
            logger.info("思考模式返回空，退回非思考模式重试")
            text = await self.provider.chat(
                messages, temperature=temperature, max_tokens=max_tokens, thinking=False
            )
        return (text or "").strip()

    # ---------------------------------------------------------- 召回

    async def recall(self, text: str, limit: int = RECALL_TOP_K) -> list[MemoryItem]:
        items = await self.db.all_memories()
        if not items:
            return []
        now = now_local(self.tz)
        query_bg = char_bigrams(text)
        ranked = [(score(item, query_bg, text, now), item) for item in items]
        ranked = [(s, item) for s, item in ranked if s >= MIN_RECALL_SCORE]
        ranked.sort(key=lambda pair: (-pair[0], -pair[1].importance))
        picked = collapse_by_episode(item for _, item in ranked)[:limit]
        if picked:
            await self.db.touch_memories([item.id for item in picked], now.isoformat())
        return picked

    # ---------------------------------------------------------- 抽取

    async def extract(self, messages: list[Message]) -> int:
        if not messages:
            return 0
        transcript = "\n".join(
            f"{'他' if m.role == 'user' else '你'}：{m.content}" for m in messages
        )
        existing = await self.db.list_memories(8)
        prompt = build_extract_prompt(existing, transcript, now_local(self.tz))

        try:
            raw = await self._complete(
                prompt, temperature=0.2, max_tokens=700, thinking=True
            )
        except LLMError as exc:
            logger.warning("记忆抽取调用失败：%s", exc)
            return 0

        candidates = parse_extraction(raw)
        if not candidates:
            logger.debug("本轮没有值得记的东西")
            return 0

        known_contents = {item.content for item in existing}
        known_ids = {item.id for item in existing}
        now = now_local(self.tz).isoformat()
        # 溯源：记下这轮对话里他最后说的那句，之后可以回读原话
        anchor = next((m.id for m in reversed(messages) if m.role == "user"), None)
        pool = await self.db.all_memories()
        added = 0

        for item in candidates:
            if item["confidence"] < MIN_CONFIDENCE or item["content"] in known_contents:
                continue
            match = find_same_event(item, pool)
            memory_id = await self.db.add_memory(
                kind=item["kind"],
                topic=item["topic"],
                content=item["content"],
                importance=item["importance"],
                confidence=item["confidence"],
                event_time=item["event_time"],
                entities=item["entities"],
                source_msg_id=anchor,
                now=now,
                due_at=item.get("due_at"),
            )
            episode_id = await self._attach_episode(memory_id, item, match, now)
            pool.append(
                MemoryItem(
                    id=memory_id,
                    kind=item["kind"],
                    content=item["content"],
                    topic=item["topic"],
                    entities=item["entities"],
                    created_at=now,
                    episode_id=episode_id,
                    episode_count=2 if match else 1,
                )
            )
            added += 1
            supersedes = [i for i in item["supersedes"] if i in known_ids]
            if supersedes:
                marked = await self.db.mark_superseded(supersedes, memory_id)
                logger.info("记忆 %s 取代了 %s", memory_id, supersedes[:3] if marked else [])

        if added:
            logger.info("新增记忆 %d 条", added)
        return added

    async def _attach_episode(
        self, memory_id: int, item: dict[str, Any], match: MemoryItem | None, now: str
    ) -> int:
        """把新记忆挂到"同一件事"上；没有就新建一个。"""
        if match is not None:
            episode_id = match.episode_id
            if episode_id is None:  # 老记忆还没归并过，补一个
                episode_id = await self.db.create_episode(
                    match.kind, match.topic, match.content, now
                )
                await self.db.set_memory_episode(match.id, episode_id)
                match.episode_id = episode_id
            await self.db.set_memory_episode(memory_id, episode_id)
            await self.db.touch_episode(episode_id, now)
            logger.info("记忆 %s 归入已有事件 %s（%s）", memory_id, episode_id, match.content[:16])
            return episode_id

        episode_id = await self.db.create_episode(
            item["kind"], item["topic"], item["content"], now
        )
        await self.db.set_memory_episode(memory_id, episode_id)
        return episode_id

    # ---------------------------------------------------------- 摘要

    async def summarize_day(self, day: str | None = None) -> bool:
        """给某一天写日记（默认昨天）；消息太少或已经写过就跳过。"""
        target = day or (now_local(self.tz).date() - timedelta(days=1)).isoformat()
        existing = await self.db.summaries(7)
        if any(str(row["period_end"])[:10] == target for row in existing):
            logger.info("%s 的日记已经写过了", target)
            return False

        rows = await self.db.recent_messages(200, roles=("user", "assistant"))
        day_rows = [m for m in rows if str(m.created_at)[:10] == target]
        if len(day_rows) < 4:
            logger.info("%s 只有 %d 条消息，不写日记", target, len(day_rows))
            return False

        return await self.summarize(day_rows, f"{target}T00:00:00", f"{target}T23:59:59")

    async def summarize(self, messages: list[Message], period_start: str, period_end: str) -> bool:
        if len(messages) < 4:
            return False
        transcript = "\n".join(
            f"{'他' if m.role == 'user' else '你'}：{m.content}" for m in messages
        )
        try:
            text = await self._complete(
                build_summary_prompt(transcript),
                temperature=0.4,
                max_tokens=400,
                thinking=False,
            )
        except LLMError as exc:
            logger.warning("生成日记失败：%s", exc)
            return False

        text = (text or "").strip()
        if len(text) < 10:
            return False
        now = now_local(self.tz).isoformat()
        await self.db.add_summary(period_start, period_end, text[:400], now)
        return True

    # ---------------------------------------------------------- 归纳

    async def rollup(self) -> bool:
        """把上次归纳之后的新对话收拢成一段笔记（滚动摘要）。"""
        last_id = await self.db.last_rollup_to_id()
        messages = await self.db.messages_after(last_id, ROLLUP_MAX_MESSAGES)
        if len(messages) < 6:
            return False

        transcript = "\n".join(
            f"{'他' if m.role == 'user' else '你'}：{m.content}" for m in messages
        )
        try:
            text = await self._complete(
                build_rollup_prompt(transcript),
                temperature=0.3,
                max_tokens=600,
                thinking=False,
            )
        except LLMError as exc:
            logger.warning("归纳调用失败：%s", exc)
            return False

        text = (text or "").strip()
        if len(text) < 20:
            return False

        now = now_local(self.tz).isoformat()
        await self.db.add_rollup(messages[0].id, messages[-1].id, text[: ROLLUP_MAX_CHARS * 2], now)
        logger.info("归纳了 %d 条消息", len(messages))
        return True

    # ---------------------------------------------------------- 渲染

    async def core(self, limit: int = CORE_LIMIT) -> list[MemoryItem]:
        """重要到不需要召回的记忆：你们怎么认识的、一起经历过的大事。

        召回按字面相关性打分，"今天吃什么"永远匹配不到"高铁站分开"，
        但这些恰恰是关系的骨架，所以单独开一层，每轮恒注入。
        """
        items = [
            item
            for item in await self.db.all_memories()
            if item.importance >= CORE_MIN_IMPORTANCE
            and item.kind in CORE_KINDS
            and item.superseded_by is None
        ]
        items.sort(key=lambda item: item.created_at or "", reverse=True)
        items.sort(key=lambda item: item.importance, reverse=True)

        kept: list[MemoryItem] = []
        total = 0
        for item in collapse_by_episode(items):
            length = len(item.content)
            if kept and total + length > CORE_MAX_CHARS:
                break
            kept.append(item)
            total += length
            if len(kept) >= limit:
                break
        return kept

    async def block(self, text: str) -> str:
        now = now_local(self.tz)
        parts: list[str] = []

        core = await self.core()
        core_ids = {item.id for item in core}
        core_episodes = {item.episode_id for item in core if item.episode_id}
        core_block = render_core(core)
        if core_block:
            parts.append(core_block)

        rollups = await self.db.rollups(ROLLUP_KEEP)
        if rollups:
            lines = ["## 之前聊过的（你记得的）", ""]
            for row in reversed(rollups):
                day = str(row["created_at"])[:10]
                lines.append(f"- {day}：{row['content']}")
            parts.append("\n".join(lines))

        summaries = await self.db.summaries(3)
        if summaries:
            lines = ["## 最近的日记", ""]
            for row in reversed(summaries):
                day = str(row["period_end"])[:10]
                lines.append(f"- {day}：{row['content']}")
            parts.append("\n".join(lines))

        recalled = [
            item
            for item in await self.recall(text)
            if item.id not in core_ids and item.episode_id not in core_episodes
        ]
        merged = list(recalled)

        # 最近 48 小时记下的事，不管像不像都带上——这是"他刚说过的事"，
        # 只靠相似度召回会漏掉（"有我可以帮忙的吗"跟"论文"没有字面重叠）
        cutoff = now - timedelta(hours=48)
        seen = set(core_ids) | {item.id for item in merged}
        for item in await self.db.list_memories(8):
            if item.id in seen:
                continue
            try:
                created = datetime.fromisoformat(item.created_at)
            except (TypeError, ValueError):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=now.tzinfo)
            if created >= cutoff:
                merged.append(item)
                seen.add(item.id)
            if len(merged) >= RECALL_TOP_K + 4:
                break

        memory_block = render(collapse_by_episode(merged), now)
        if memory_block:
            parts.append(memory_block)

        detail = await self.detail_block(text)
        if detail:
            parts.append(detail)

        return "\n\n".join(parts)

    async def backfill_episodes(self) -> int:
        """给还没有 episode 的老记忆补归并（一次性，幂等）。

        schema v4 之前写入的记忆没有事件归属，启动时补一遍，
        这样"论文"那种同一件事的多条记忆能立刻并成一条。
        """
        items = await self.db.all_memories()
        ungrouped = [item for item in items if item.episode_id is None]
        if not ungrouped:
            return 0

        pool = [item for item in items if item.episode_id is not None]
        assigned = 0
        for item in ungrouped:
            payload = {
                "kind": item.kind,
                "topic": item.topic,
                "content": item.content,
                "entities": item.entities or [],
            }
            match = find_same_event(payload, pool)
            stamp = item.created_at or now_local(self.tz).isoformat()
            episode_id = await self._attach_episode(item.id, payload, match, stamp)
            item.episode_id = episode_id
            pool.append(item)
            assigned += 1

        if assigned:
            logger.info("给 %d 条老记忆补了事件归并", assigned)
        return assigned

    # ---------------------------------------------------------- 遗忘

    async def forget_stale(
        self, days: int = FORGET_DAYS, max_importance: int = FORGET_MAX_IMPORTANCE
    ) -> int:
        """把又旧又不重要的记忆归档（不删，可查）。承诺不参与。"""
        cutoff = (now_local(self.tz) - timedelta(days=days)).isoformat()
        stale = await self.db.stale_memories(cutoff, max_importance)
        count = 0
        for item in stale:
            count += await self.db.archive_memory(item.id)
            logger.debug("归档记忆 %s：%s", item.id, item.content[:20])
        return count

    async def forget(self, memory_id: int) -> bool:
        return bool(await self.db.archive_memory(memory_id))

    async def forget_matching(self, keyword: str) -> list[str]:
        """按 id 或关键词归档，返回被归档的内容。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        hits: list[str] = []
        for item in await self.db.all_memories():
            if keyword == str(item.id) or keyword in item.content:
                if await self.db.archive_memory(item.id):
                    hits.append(item.content)
        return hits

    # ---------------------------------------------------------- 原话回读

    async def detail_block(self, text: str) -> str:
        """他提到"上次那个"时，把当时的原话捞出来。

        记忆只有 40 字摘要，细节在 messages 里；靠写入时记下的 source_msg_id 回读。
        """
        if not REFERENCE_RE.search(text or ""):
            return ""
        items = collapse_by_episode(await self.recall(text, limit=DETAIL_TOP + 2))[:DETAIL_TOP]
        lines: list[str] = []
        for item in items:
            if not item.source_msg_id:
                continue
            around = await self.db.messages_around(item.source_msg_id, DETAIL_SPAN)
            if not around:
                continue
            day = str(around[0].created_at)[:10]
            lines.append(f"- {day}：")
            for message in around:
                who = "他" if message.role == "user" else "你"
                snippet = message.content.replace("\n", " ")[:DETAIL_CHARS]
                lines.append(f"  {who}：{snippet}")
        if not lines:
            return ""
        header = "## 你们当时的原话（对得上就用，别逐句复述）"
        return "\n".join([header, ""] + lines)
