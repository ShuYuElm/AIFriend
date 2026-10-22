"""她的日程与消息时的状态。"""

from datetime import datetime

from app.config import ROOT
from app.life import load_life

LIFE = load_life(ROOT / "config" / "life.yaml")


def test_schedule_covers_weekday_morning():
    state = LIFE.state_at(datetime(2026, 10, 19, 8, 0))  # 周一
    assert "上班" in state.state or "起床" in state.state


def test_schedule_evening_free_time():
    state = LIFE.state_at(datetime(2026, 10, 19, 21, 30))
    assert state.state == "自由安排"


def test_quiet_hours_at_night():
    assert LIFE.is_quiet(datetime(2026, 10, 19, 3, 0))
    assert not LIFE.is_quiet(datetime(2026, 10, 19, 12, 0))


def test_message_during_sleep_is_not_awake():
    # 她已经睡了，但消息进来了 —— 不能还说"睡了"
    state = LIFE.state_for_message(datetime(2026, 10, 19, 3, 0))
    assert "睡了" not in state.state
    assert "吵醒" in state.state


def test_message_just_after_bedtime():
    state = LIFE.state_for_message(datetime(2026, 10, 19, 1, 10))
    assert state.state == "还没睡"


def test_message_during_day_keeps_state():
    state = LIFE.state_for_message(datetime(2026, 10, 19, 21, 30))
    assert state.state == "自由安排"
