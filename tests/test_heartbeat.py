"""心跳：闸门、动机分、决策解析、发送与幂等。"""

from datetime import datetime

from app.config import ROOT, Settings
from app.db import Database
from app.experiences import ExperienceQueue
from app.heartbeat import (
    BASE_MOTIVATION,
    Heartbeat,
    TickContext,
    build_prompt,
    check_gates,
    motivation,
    parse_decision,
    window_kind,
)
from app.life import load_life
from app.memory import Memory
from app.persona import PersonaStore
from app.profile import ProfileStore
from app.world import World, WorldConfig

MORNING = datetime(2026, 10, 19, 8, 0)
NIGHT = datetime(2026, 10, 19, 3, 0)
NOON = datetime(2026, 10, 19, 12, 0)


class DecisionProvider:
    name = "fake"
    model = "fake"

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.last_messages: list[dict[str, str]] = []

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = messages
        return self.payload


def _ctx(**overrides) -> TickContext:
    base = dict(
        now=MORNING,
        window="morning",
        quiet=False,
        sent_today=0,
        minutes_since_proactive=None,
        minutes_since_user=None,
        unanswered=0,
    )
    base.update(overrides)
    return TickContext(**base)


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_base_url="http://localhost",
        llm_api_key="",
        llm_model="fake",
        tz="Asia/Shanghai",
        db_path=tmp_path / "hb.db",
        persona_path=ROOT / "config" / "persona.yaml",
        life_path=ROOT / "config" / "life.yaml",
        profile_path=ROOT / "config" / "user_profile.yaml",
        seed_path=ROOT / "config" / "seed_memories.yaml",
        world_path=tmp_path / "no-world.yaml",
    )


def _heartbeat(tmp_path, payload: str, now: datetime = MORNING):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    provider = DecisionProvider(payload)
    life = load_life(settings.life_path)
    hb = Heartbeat(
        settings=settings,
        db=db,
        provider=provider,
        persona_store=PersonaStore(settings.persona_path),
        profile_store=ProfileStore(settings.profile_path),
        life=life,
        memory=Memory(db, provider, settings.tz),
        experiences=ExperienceQueue(db, life, settings.tz),
        clock=lambda: now,
    )
    return hb, db, provider


# ------------------------------------------------------------------ 纯函数

def test_window_kind():
    assert window_kind(datetime(2026, 10, 19, 8, 0)) == "morning"
    assert window_kind(datetime(2026, 10, 19, 12, 0)) == "noon"
    assert window_kind(datetime(2026, 10, 19, 20, 0)) == "evening"
    assert window_kind(datetime(2026, 10, 19, 23, 0)) == "night"
    assert window_kind(datetime(2026, 10, 19, 15, 0)) == "idle"


def test_gate_quiet_first():
    gate = check_gates(_ctx(quiet=True))
    assert gate and gate[0] == "quiet"


def test_gate_cap():
    gate = check_gates(_ctx(sent_today=4))
    assert gate and gate[0] == "cap"


def test_gate_unanswered():
    gate = check_gates(_ctx(unanswered=2))
    assert gate and gate[0] == "unanswered"


def test_gate_gap():
    gate = check_gates(_ctx(minutes_since_proactive=30))
    assert gate and gate[0] == "gap"


def test_gate_recent_chat():
    gate = check_gates(_ctx(minutes_since_user=5))
    assert gate and gate[0] == "recent"


def test_gate_passes():
    assert check_gates(_ctx(minutes_since_user=600)) is None


def test_motivation_idle_bonus_and_overheat():
    assert motivation(_ctx(minutes_since_user=60 * 8, sent_today=0)) > BASE_MOTIVATION["morning"]
    assert motivation(_ctx(minutes_since_user=60 * 8, sent_today=3)) < motivation(
        _ctx(minutes_since_user=60 * 8, sent_today=0)
    )


def test_motivation_fresh_experience_bonus():
    plain = motivation(_ctx(minutes_since_user=60 * 2))
    fresh = motivation(_ctx(minutes_since_user=60 * 2, fresh_experience=True))
    assert round(fresh - plain, 2) == 0.35


def test_motivation_quiet_window_low():
    assert motivation(_ctx(window="idle", minutes_since_user=60)) < 0.55


def test_parse_decision_send():
    raw = '```json\n{"send": true, "bubbles": ["刚下自习", "累死了"], "reason": "想到他"}\n```'
    decision = parse_decision(raw)
    assert decision["send"] is True
    assert decision["bubbles"] == ["刚下自习", "累死了"]


def test_parse_decision_skip():
    decision = parse_decision('{"send": false, "reason": "没什么想说的"}')
    assert decision["send"] is False


def test_parse_decision_garbage():
    assert parse_decision("我觉得不发") is None
    assert parse_decision("") is None
    assert parse_decision('{"send": "yes"}') is None


# ------------------------------------------------------------------ 集成

async def test_tick_sends_and_logs(tmp_path):
    hb, db, _ = _heartbeat(
        tmp_path, '{"send": true, "bubbles": ["刚下自习"], "reason": "想问他吃没吃"}'
    )

    result = await hb.tick()

    assert result["action"] == "sent"
    assert result["bubbles"] == ["刚下自习"]
    assert await db.count_messages("assistant") == 1
    assert await db.count_proactive("2026-10-19") == 1
    assert await db.get_state("proactive_unanswered") == 1
    logs = await db.recent_heartbeats(5)
    assert logs[0]["action"] == "sent"
    db.close()


async def test_tick_respects_quiet_hours(tmp_path):
    hb, db, provider = _heartbeat(
        tmp_path, '{"send": true, "bubbles": ["在吗"], "reason": "x"}', now=NIGHT
    )
    result = await hb.tick()
    assert result["action"] == "skipped"
    assert result["gate"] == "quiet"
    assert provider.calls == 0
    db.close()


async def test_tick_respects_daily_cap(tmp_path):
    hb, db, provider = _heartbeat(
        tmp_path, '{"send": true, "bubbles": ["在吗"], "reason": "x"}'
    )
    for i in range(4):
        await db.log_proactive("morning", f"morning:2026-10-1{i}", "2026-10-19T08:00:00", None)
    result = await hb.tick()
    assert result["action"] == "skipped"
    assert result["gate"] == "cap"
    assert provider.calls == 0
    db.close()


async def test_tick_stops_after_two_unanswered(tmp_path):
    hb, db, provider = _heartbeat(
        tmp_path, '{"send": true, "bubbles": ["在吗"], "reason": "x"}'
    )
    await db.set_state("proactive_unanswered", 2)
    result = await hb.tick()
    assert result["action"] == "skipped"
    assert result["gate"] == "unanswered"
    assert provider.calls == 0
    db.close()


async def test_tick_allows_non_action(tmp_path):
    hb, db, provider = _heartbeat(tmp_path, '{"send": false, "reason": "没什么想说的"}')
    result = await hb.tick()
    assert result["action"] == "skipped"
    assert result["gate"] == "self"
    assert provider.calls == 1
    assert await db.count_proactive("2026-10-19") == 0
    db.close()


async def test_force_bypasses_gates(tmp_path):
    hb, db, _ = _heartbeat(
        tmp_path, '{"send": true, "bubbles": ["睡不着"], "reason": "x"}', now=NIGHT
    )
    result = await hb.tick(force=True)
    assert result["action"] == "sent"
    db.close()


# ------------------------------------------------------------------ 跨天 / 环境

async def test_rollover_resets_day_counters(tmp_path):
    hb, db, _ = _heartbeat(tmp_path, '{"send": false, "reason": "x"}')
    await db.set_state("last_seen_date", "2026-10-19")
    await db.set_state("proactive_unanswered", 2)

    await hb.tick()

    assert await db.get_state("last_seen_date") == "2026-10-19"  # clock 是 10-19
    assert await db.get_state("proactive_unanswered") == 2  # 同一天，不清

    # 换一天
    hb.clock = lambda: datetime(2026, 10, 20, 8, 0)
    await hb.tick()

    assert await db.get_state("last_seen_date") == "2026-10-20"
    assert await db.get_state("proactive_unanswered") == 0
    db.close()


async def test_experiences_generated_after_midnight_rollover(tmp_path):
    """回归：0 点翻篇时 ensure_today 还太早，之后再没人补 → 整天没经历。"""
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    life = load_life(settings.life_path)

    class Clock:
        def __init__(self, value):
            self.value = value

        def __call__(self):
            return self.value

    clock = Clock(datetime(2026, 10, 20, 23, 50))
    hb = Heartbeat(
        settings=settings,
        db=db,
        provider=DecisionProvider('{"send": false, "reason": "x"}'),
        persona_store=PersonaStore(settings.persona_path),
        profile_store=ProfileStore(settings.profile_path),
        life=life,
        memory=Memory(db, DecisionProvider("{}"), settings.tz),
        experiences=ExperienceQueue(db, life, settings.tz),
        clock=clock,
    )

    # 0 点刚过翻篇：这一刻还太早，不该有经历
    clock.value = datetime(2026, 10, 21, 0, 30)
    await hb.tick()
    assert await db.experiences_for_day("2026-10-21") == []

    # 白天随便一跳：必须补上这一天的经历（可能抽到 0 件，多跳几次）
    for hour in (10, 12, 15, 18):
        clock.value = datetime(2026, 10, 21, hour, 5)
        await hb.tick()
        if await db.experiences_for_day("2026-10-21"):
            break
    assert await db.experiences_for_day("2026-10-21")
    db.close()


async def test_prompt_includes_weather(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    provider = DecisionProvider('{"send": false, "reason": "没什么想说的"}')
    life = load_life(settings.life_path)

    async def fake_fetch(url, params):
        return [
            {"current": {"temperature_2m": 18.5, "weather_code": 61},
             "daily": {"precipitation_probability_max": [90]}},
            {"current": {"temperature_2m": 23.0, "weather_code": 1},
             "daily": {"precipitation_probability_max": [10]}},
        ]

    world = World(db, WorldConfig(enabled=True, her={"city": "成都", "latitude": 1, "longitude": 2},
                                  him={"city": "上海", "latitude": 3, "longitude": 4}),
                  settings.tz, fetcher=fake_fetch)
    await world.refresh(MORNING)

    hb = Heartbeat(
        settings=settings,
        db=db,
        provider=provider,
        persona_store=PersonaStore(settings.persona_path),
        profile_store=ProfileStore(settings.profile_path),
        life=life,
        memory=Memory(db, provider, settings.tz),
        experiences=ExperienceQueue(db, life, settings.tz),
        world=world,
        clock=lambda: MORNING,
    )
    await hb.tick(force=True)

    prompt = provider.last_messages[0]["content"]
    assert "外面" in prompt
    assert "18℃" in prompt
    assert "降雨概率 90%" in prompt
    db.close()


def test_prompt_states_sent_today_only_once():
    prompt = build_prompt(
        name="林夏",
        now=MORNING,
        life_state="在书店兼职",
        gap_text="上一句是他 2 小时前（06:00）说的",
        sent_today=1,
        memory_block="",
        profile_block="",
    )
    assert prompt.count("今天你已经主动找过他") == 1
    assert "unless" not in prompt
