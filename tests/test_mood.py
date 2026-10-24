"""情绪状态：规则、衰减、注入。"""

from datetime import datetime, timedelta

from app.db import Database
from app.mood import MAX_LEVEL, Mood, MoodTracker

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 24, 21, 0)


def _tracker(tmp_path) -> tuple[MoodTracker, Database]:
    db = Database(tmp_path / "m.db")
    return MoodTracker(db, TZ), db


async def test_warm_message_lifts_mood(tmp_path):
    mood, db = _tracker(tmp_path)

    result = await mood.observe("想你了，今天累不累", None, NOW)
    assert result.level == 1
    assert "心情不错" in MoodTracker.render(result)

    again = await mood.observe("抱抱", None, NOW)
    assert again.level == 2
    assert "亲近" in MoodTracker.render(again)
    db.close()


async def test_angry_message_drops_to_floor(tmp_path):
    mood, db = _tracker(tmp_path)
    result = await mood.observe("别烦我", None, NOW)
    assert result.level == -MAX_LEVEL
    assert "生气" in MoodTracker.render(result)
    db.close()


async def test_short_cold_reply_counts_as_brushed_off(tmp_path):
    mood, db = _tracker(tmp_path)
    result = await mood.observe("哦", None, NOW)
    assert result.level == -1
    assert "短" in result.note
    db.close()


async def test_long_silence_makes_her_uneasy(tmp_path):
    mood, db = _tracker(tmp_path)
    result = await mood.observe("在", gap_hours=20, now=NOW)
    assert result.level == -1
    assert "小时" in result.note
    db.close()


async def test_mood_decays_with_time(tmp_path):
    mood, db = _tracker(tmp_path)
    old = (NOW - timedelta(hours=7)).isoformat()
    await mood.save(Mood(level=2, note="他说了句软话", updated_at=old))

    result = await mood.observe("今天天气不错", None, NOW)
    assert result.level == 1  # 消一层

    db.close()


async def test_mood_forgets_after_two_days(tmp_path):
    mood, db = _tracker(tmp_path)
    old = (NOW - timedelta(hours=50)).isoformat()
    await mood.save(Mood(level=-2, note="他说话很冲", updated_at=old))

    result = await mood.observe("今天天气不错", None, NOW)
    assert result.level == 0
    assert MoodTracker.render(result) == ""
    db.close()


async def test_level_is_clamped(tmp_path):
    mood, db = _tracker(tmp_path)
    for _ in range(5):
        result = await mood.observe("喜欢你", None, NOW)
    assert result.level == MAX_LEVEL
    db.close()


async def test_neutral_level_renders_nothing(tmp_path):
    mood, db = _tracker(tmp_path)
    result = await mood.observe("在干嘛呢今天", None, NOW)
    assert result.level == 0
    assert MoodTracker.render(result) == ""
    db.close()


async def test_warm_words_offset_a_long_absence(tmp_path):
    """隔很久才回，但说的是软话：扣分被抵消，回到平常。"""
    mood, db = _tracker(tmp_path)
    result = await mood.observe("想你了，这几天太忙", gap_hours=90, now=NOW)
    assert result.level == 0
    db.close()