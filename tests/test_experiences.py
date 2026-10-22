"""延迟分享：生成、到期、不重复。"""

import random
from datetime import datetime

from app.config import ROOT
from app.db import Database
from app.experiences import ExperienceQueue, mentions
from app.life import load_life

LIFE = load_life(ROOT / "config" / "life.yaml")


class FixedRng(random.Random):
    """固定产 2 条经历，时间从 09:00 起。"""

    def choices(self, population, weights=None, k=1):  # noqa: A003
        return [2]

    def sample(self, population, k):
        return list(population)[:k]

    def randrange(self, a, b):
        return a

    def randint(self, a, b):
        return a


def _queue(tmp_path) -> tuple[ExperienceQueue, Database]:
    db = Database(tmp_path / "exp.db")
    return ExperienceQueue(db, LIFE, "Asia/Shanghai", FixedRng()), db


async def test_ensure_today_generates(tmp_path):
    queue, db = _queue(tmp_path)
    now = datetime(2026, 10, 20, 15, 0)

    made = await queue.ensure_today(now)
    rows = await db.experiences_for_day("2026-10-20")

    assert made == 2
    assert len(rows) == 2
    assert all(row["status"] == "pending" for row in rows)
    db.close()


async def test_ensure_today_is_idempotent(tmp_path):
    queue, db = _queue(tmp_path)
    now = datetime(2026, 10, 20, 15, 0)
    await queue.ensure_today(now)
    assert await queue.ensure_today(now) == 0
    assert len(await db.experiences_for_day("2026-10-20")) == 2
    db.close()


async def test_too_early_generates_nothing(tmp_path):
    queue, db = _queue(tmp_path)
    assert await queue.ensure_today(datetime(2026, 10, 20, 8, 0)) == 0
    db.close()


async def test_due_only_after_share_time(tmp_path):
    queue, db = _queue(tmp_path)
    now = datetime(2026, 10, 20, 15, 0)
    await db.add_experience("早就该说了", "2026-10-20T10:00:00", "2026-10-20T12:00:00")
    await db.add_experience("还没到时候", "2026-10-20T14:30:00", "2026-10-20T18:00:00")

    due = await queue.due(now)

    assert [item.content for item in due] == ["早就该说了"]
    db.close()


async def test_mark_shared_removes_from_due(tmp_path):
    queue, db = _queue(tmp_path)
    now = datetime(2026, 10, 20, 15, 0)
    exp_id = await db.add_experience("说过了", "2026-10-20T10:00:00", "2026-10-20T11:00:00")

    await queue.mark_shared([exp_id], now)

    assert await queue.due(now) == []
    db.close()


def test_pick_for_reply_respects_probability():

    class Yes:
        def random(self):
            return 0.0

        def choice(self, seq):
            return seq[0]

    class No:
        def random(self):
            return 0.99

    class Item:
        content = "x"

    due = [Item()]
    assert ExperienceQueue.pick_for_reply(due, Yes()) is not None
    assert ExperienceQueue.pick_for_reply(due, No()) is None
    assert ExperienceQueue.pick_for_reply([], Yes()) is None


def test_mentions_detects_reference():
    event = "在书店遇到个同学，文件发了三次"
    assert mentions("今天书店那个同学又来了", event)
    assert not mentions("那我先去洗澡了", event)
