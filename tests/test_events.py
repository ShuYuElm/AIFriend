"""到点事件：承诺、生日、纪念日，以及它们对心跳闸门的影响。"""

from datetime import datetime

from app.config import ROOT
from app.db import Database
from app.events import EventPlanner, _matches_month_day
from app.heartbeat import TickContext, check_gates, motivation
from app.persona import PersonaStore
from app.profile import ProfileStore

NIGHT_3AM = datetime(2026, 10, 21, 3, 30)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _profile(tmp_path, dates: dict[str, str] | None = None) -> ProfileStore:
    path = tmp_path / "profile.yaml"
    lines = ["name: 阿川", "dates:"]
    for key, value in (dates or {}).items():
        lines.append(f'  {key}: "{value}"')
    lines.append("sections: []")
    path.write_text("\n".join(lines), encoding="utf-8")
    return ProfileStore(path)


def _planner(db, tmp_path, dates=None) -> EventPlanner:
    return EventPlanner(
        db,
        _profile(tmp_path, dates),
        PersonaStore(ROOT / "config" / "persona.yaml"),
        "Asia/Shanghai",
    )


def test_matches_month_day():
    assert _matches_month_day(datetime(2026, 7, 7).date(), "07-07")
    assert _matches_month_day(datetime(2026, 7, 7).date(), "7-7")
    assert not _matches_month_day(datetime(2026, 7, 8).date(), "07-07")


# ------------------------------------------------------------------ 闸门

def test_urgent_bypasses_quiet_and_quiet_only():
    ctx = TickContext(
        now=NIGHT_3AM, window="night", quiet=True, sent_today=0,
        minutes_since_proactive=None, minutes_since_user=600,
        unanswered=0, urgent=True,
    )
    assert check_gates(ctx) is None
    assert motivation(ctx) == 1.0


def test_urgent_still_respects_daily_cap():
    ctx = TickContext(
        now=NIGHT_3AM, window="night", quiet=True, sent_today=5,
        minutes_since_proactive=None, minutes_since_user=600,
        unanswered=0, urgent=True,
    )
    gate = check_gates(ctx)
    assert gate and gate[0] == "cap"


# ------------------------------------------------------------------ 承诺

async def _add_promise(db, due_at: str, content: str = "明早叫他起床") -> int:
    return await db.add_memory(
        kind="promise", topic=None, content=content, importance=4, confidence=0.9,
        event_time=None, entities=[], source_msg_id=None,
        now="2026-10-20T22:00", due_at=due_at,
    )


async def test_promise_due_fires(tmp_path):
    db = Database(tmp_path / "e.db")
    await _add_promise(db, "2026-10-21T07:30")
    planner = _planner(db, tmp_path)

    events = await planner.due(datetime(2026, 10, 21, 7, 31))
    assert [e.kind for e in events] == ["promise"]
    assert "起床" in events[0].text
    db.close()


async def test_promise_not_yet(tmp_path):
    db = Database(tmp_path / "e.db")
    await _add_promise(db, "2026-10-21T07:30")
    planner = _planner(db, tmp_path)

    assert await planner.due(datetime(2026, 10, 21, 6, 0)) == []
    db.close()


async def test_promise_too_old(tmp_path):
    db = Database(tmp_path / "e.db")
    await _add_promise(db, "2026-10-18T07:30")
    planner = _planner(db, tmp_path)

    assert await planner.due(datetime(2026, 10, 21, 7, 31)) == []
    db.close()


async def test_promise_done_archives(tmp_path):
    db = Database(tmp_path / "e.db")
    memory_id = await _add_promise(db, "2026-10-21T07:30")
    planner = _planner(db, tmp_path)
    now = datetime(2026, 10, 21, 7, 31)

    events = await planner.due(now)
    await planner.done(now, events)

    # 归档后既不再触发，也不出现在活跃记忆里
    assert await planner.due(now) == []
    assert memory_id not in [m.id for m in await db.all_memories()]
    db.close()


# ------------------------------------------------------------------ 生日 / 纪念日

async def test_birthday_fires_once(tmp_path):
    db = Database(tmp_path / "e.db")
    planner = _planner(db, tmp_path, {"his_birthday": "07-07"})
    now = datetime(2026, 7, 7, 9, 0)

    events = await planner.due(now)
    assert [e.kind for e in events] == ["birthday"]

    await planner.done(now, events)
    assert await planner.due(now) == []
    db.close()


def test_anniversary_at_milestone(tmp_path):
    import asyncio

    async def run():
        db = Database(tmp_path / "e.db")
        planner = _planner(db, tmp_path)
        # persona known_since = 2026-07-19，第 100 天是 2026-10-27
        events = await planner.due(datetime(2026, 10, 27, 10, 0))
        kinds = [e.kind for e in events]
        db.close()
        return kinds

    assert "anniversary" in asyncio.run(run())


# ------------------------------------------------------------------ 拒绝后不再追问

async def test_declined_twice_stops_asking(tmp_path):
    db = Database(tmp_path / "e.db")
    await _add_promise(db, "2026-10-21T07:30")
    planner = _planner(db, tmp_path)
    now = datetime(2026, 10, 21, 7, 31)

    first = await planner.due(now)
    assert first and first[0].kind == "promise"
    await planner.record_decline(now, first)
    assert await planner.due(now)  # 第一次拒绝后还会再问一次

    second = await planner.due(now)
    await planner.record_decline(now, second)
    assert await planner.due(now) == []  # 两次之后当天不再问
    db.close()


def test_block_strips_duplicate_promise_prefix(tmp_path):
    from app.events import DueEvent

    text = EventPlanner.block([DueEvent("promise", "答应他明早七点半叫他起床")])
    assert "你答应过他：明早七点半叫他起床" in text
    assert "答应他" not in text.split("你答应过他：")[1]
