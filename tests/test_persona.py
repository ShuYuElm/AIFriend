"""人设卡解析。"""

from datetime import datetime, timedelta

import pytest

from app.config import ROOT
from app.persona import (
    PersonaError,
    PersonaStore,
    RuntimeContext,
    build_p1,
    build_p5,
    format_gap,
    parse_persona,
)

PERSONA_PATH = ROOT / "config" / "persona.yaml"


def test_loads_real_card():
    card = PersonaStore(PERSONA_PATH).load()
    assert card.name == "林夏"
    assert card.user_nickname == "阿川"
    assert "暧昧" in card.stage
    assert card.first_mes == "今天书店人少得离谱，我坐这儿看了一下午书"
    assert card.book["entries"]


def test_build_p1_contains_context():
    card = PersonaStore(PERSONA_PATH).load()
    now = datetime(2026, 10, 19, 21, 47)
    ctx = RuntimeContext(
        now=now,
        gap_seconds=3600 * 3,
        stage="暧昧",
        life_state="宿舍休息",
        life_detail="刷手机",
        days_known=5,
    )
    text = build_p1(card, ctx)
    assert "林夏" in text
    assert "阿川" in text
    assert "2026-10-19" in text
    assert "3 小时前" in text
    assert "第 5 天" in text
    assert "说话方式" in text


def test_build_p5_is_post_history():
    card = PersonaStore(PERSONA_PATH).load()
    assert build_p5(card) == card.post_history_instructions


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "第一次说话"),
        (30, "刚发来消息"),
        (600, "10 分钟前"),
        (3600 * 5 + 60, "5 小时 1 分钟前"),
        (3600 * 50, "2 天前"),
    ],
)
def test_format_gap(seconds, expected):
    assert expected in format_gap(seconds)


def test_format_gap_adds_clock_to_avoid_3am_confusion():
    now = datetime(2026, 10, 20, 8, 10)
    text = format_gap(3600 * 3, now)
    assert "3 小时前" in text
    assert "05:10" in text


def test_week_strip_has_today_and_tomorrow():
    from app.persona import _week_strip

    text = _week_strip(datetime(2026, 10, 20, 10, 0))  # 周二
    assert "(今天)" in text
    assert "10-21周三" in text
    assert "明天是 10-21 周三" in text


def test_rejects_wrong_spec():
    with pytest.raises(PersonaError):
        parse_persona({"spec": "other", "name": "x"})


def test_rejects_missing_fields():
    with pytest.raises(PersonaError) as exc:
        parse_persona({"spec": "aigirl_card_v1", "name": "x"})
    assert "缺字段" in str(exc.value)
