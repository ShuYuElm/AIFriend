"""时间对齐：naive / aware 混用不能再炸（timeutil）。"""

from datetime import datetime, timedelta, timezone

from app.timeutil import align

AWARE = datetime(2026, 10, 20, 10, 0, tzinfo=timezone(timedelta(hours=8)))
NAIVE = datetime(2026, 10, 20, 10, 0)


def test_align_adds_tz():
    assert align(NAIVE, AWARE).tzinfo is not None


def test_align_removes_tz():
    assert align(AWARE, NAIVE).tzinfo is None


def test_align_keeps_when_same():
    assert align(AWARE, AWARE) is AWARE
    assert align(NAIVE, NAIVE) is NAIVE


def test_aligned_subtraction_does_not_raise():
    assert (AWARE - align(NAIVE, AWARE)).total_seconds() == 0
    assert (NAIVE - align(AWARE, NAIVE)).total_seconds() == 0
