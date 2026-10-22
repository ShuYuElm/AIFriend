"""人设卡加载与 prompt 片段构建。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("aigirl.persona")

WEEKDAYS = "一二三四五六日"

REQUIRED_FIELDS = (
    "name",
    "description",
    "personality",
    "scenario",
    "system_prompt",
    "post_history_instructions",
    "first_mes",
    "mes_example",
)


class PersonaError(RuntimeError):
    """人设卡缺失或格式错误。"""


# ------------------------------------------------------------------ 时间

def now_local(tz_name: str) -> datetime:
    """本地时间；Windows 缺 tzdata 时退回固定 +8。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(timezone(timedelta(hours=8)))


def format_gap(seconds: float | None, now: datetime | None = None) -> str:
    """只说"3 小时前"会被模型读成"三点"，所以超过一小时就补上钟点。"""
    if seconds is None:
        return "这是你们今天第一次说话"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "他刚发来消息"

    clock = ""
    if now is not None:
        then = now - timedelta(seconds=seconds)
        clock = f"（{then:%H:%M}）"

    if minutes < 60:
        return f"上一句是他 {minutes} 分钟前说的"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        if rest:
            return f"上一句是他 {hours} 小时 {rest} 分钟前{clock}说的"
        return f"上一句是他 {hours} 小时前{clock}说的"
    days, hours = divmod(hours, 24)
    return f"上一句是他 {days} 天前说的"


# ------------------------------------------------------------------ 卡片

@dataclass(slots=True)
class PersonaCard:
    name: str
    user_nickname: str
    description: str
    personality: str
    scenario: str
    system_prompt: str
    post_history_instructions: str
    first_mes: str
    stage: str = "熟悉"
    known_since: str = ""
    alternate_greetings: list[str] = field(default_factory=list)
    mes_example: str = ""
    book: dict[str, Any] = field(default_factory=dict)


def parse_persona(data: dict[str, Any]) -> PersonaCard:
    if not isinstance(data, dict):
        raise PersonaError("人设卡内容不是 YAML 映射")
    if data.get("spec") != "aigirl_card_v1":
        raise PersonaError(f"spec 应为 aigirl_card_v1，实际 {data.get('spec')!r}")

    missing = [k for k in REQUIRED_FIELDS if not str(data.get(k) or "").strip()]
    if missing:
        raise PersonaError(f"缺字段：{', '.join(missing)}")

    return PersonaCard(
        name=str(data["name"]).strip(),
        user_nickname=str(data.get("user_nickname") or "你").strip(),
        description=str(data["description"]).strip(),
        personality=str(data["personality"]).strip(),
        scenario=str(data["scenario"]).strip(),
        system_prompt=str(data["system_prompt"]).strip(),
        post_history_instructions=str(data["post_history_instructions"]).strip(),
        first_mes=str(data["first_mes"]).strip(),
        stage=str(data.get("stage") or "熟悉").strip(),
        known_since=str(data.get("known_since") or "").strip(),
        alternate_greetings=[str(x).strip() for x in (data.get("alternate_greetings") or [])],
        mes_example=str(data["mes_example"]).strip(),
        book=dict(data.get("character_book") or {}),
    )


class PersonaStore:
    """按 mtime 缓存；改完下一轮生效，不用重启。解析失败保留上一版。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._card: PersonaCard | None = None
        self._mtime: float = -1.0

    def invalidate(self, path: Path | None = None) -> None:
        """管理指令改完文件后调用；顺带可以切换到新路径（如刚生成的 .local.yaml）。"""
        if path is not None:
            self.path = Path(path)
        self._mtime = -1.0

    def load(self) -> PersonaCard:
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            raise PersonaError(f"读不到人设卡：{self.path}") from exc

        if self._card is not None and mtime == self._mtime:
            return self._card

        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            card = parse_persona(raw)
        except (yaml.YAMLError, PersonaError) as exc:
            if self._card is not None:
                logger.error("人设卡解析失败，沿用上一版：%s", exc)
                return self._card
            raise PersonaError(f"人设卡解析失败：{exc}") from exc

        self._card = card
        self._mtime = mtime
        return card


# ------------------------------------------------------------------ 运行时上下文

@dataclass(slots=True)
class RuntimeContext:
    now: datetime
    gap_seconds: float | None = None
    stage: str = "熟悉"
    life_state: str = ""
    life_detail: str = ""
    days_known: int = 0
    proactive_today: int = 0
    world: str = ""


def _week_strip(now: datetime) -> str:
    """给一条本周日期条，帮模型算"周五还剩几天""明天周几"。"""
    monday = now.date() - timedelta(days=now.weekday())
    parts = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        label = f"{day:%m-%d}" + f"周{WEEKDAYS[day.weekday()]}"
        if day == now.date():
            label += "(今天)"
        parts.append(label)
    tomorrow = now.date() + timedelta(days=1)
    return f"本周：{' '.join(parts)}；明天是 {tomorrow:%m-%d} 周{WEEKDAYS[tomorrow.weekday()]}"


def build_p1(persona: PersonaCard, ctx: RuntimeContext) -> str:
    now = ctx.now
    lines = [
        persona.system_prompt,
        "",
        "## 现在的情况",
        f"现在是 {now:%Y-%m-%d} 周{WEEKDAYS[now.weekday()]} {now:%H:%M}",
        format_gap(ctx.gap_seconds, now),
        _week_strip(now),
    ]
    if ctx.life_state:
        detail = f"（{ctx.life_detail}）" if ctx.life_detail else ""
        lines.append(f"你此刻：{ctx.life_state}{detail}")
    if ctx.world:
        lines.append(f"外面：{ctx.world}")
    if ctx.days_known > 0:
        lines.append(f"你们认识第 {ctx.days_known} 天，关系阶段：{ctx.stage}")
    else:
        lines.append(f"关系阶段：{ctx.stage}")

    lines += [
        "",
        "## 你和他的称呼",
        f"你叫{persona.name}，他叫{persona.user_nickname}。",
        "",
        "## 说话方式（必须遵守）",
        "- 一次只说一两句，10–40 字，最多发三条",
        "- 不列清单、不总结、不讲道理、不用 Markdown",
        "- 不每句都提问；不想聊了可以直接收住",
        "- 关于他的事如果确实没记住，别编细节，也别直说「我不知道」，反问他一句把话接过去",
        "- 只记得一半的时候先反问确认，别自己补全",
        "- 他讲专业细节时你只听懂大意，不懂就问，别给技术建议",
        "- 他问「还有几天」「周几」时，照上面那行日期算，直接给数字和星期，别绕开",
        "- 别每次都拿他记性开玩笑",
        "- 绝不出现「作为一个 AI」「希望这对你有帮助」这类话",
    ]
    return "\n".join(lines)


def build_p5(persona: PersonaCard) -> str:
    return persona.post_history_instructions
