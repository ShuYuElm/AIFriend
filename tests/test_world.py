"""世界感知：天气码、渲染、缓存、失败降级。"""

from datetime import datetime

import pytest

from app.db import Database
from app.world import World, WorldConfig, describe_code, load_world_config

NOW = datetime(2026, 10, 20, 10, 0)


def _weather_payload():
    return [
        {
            "current": {"temperature_2m": 18.5, "weather_code": 61},
            "daily": {"precipitation_probability_max": [90]},
        },
        {
            "current": {"temperature_2m": 23.4, "weather_code": 1},
            "daily": {"precipitation_probability_max": [10]},
        },
    ]


class FakeFetch:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def __call__(self, url: str, params: dict) -> dict:
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("网络挂了")
        if "open-meteo" in url:
            return _weather_payload()
        return {"days": [{"date": "2026-10-20", "name": "国庆节", "isOffDay": True}]}


def _config(**kwargs) -> WorldConfig:
    base = dict(
        enabled=True,
        her={"city": "成都", "latitude": 32.0, "longitude": 118.8},
        him={"city": "上海", "latitude": 31.3, "longitude": 120.6},
        holidays=True,
    )
    base.update(kwargs)
    return WorldConfig(**base)


def test_describe_code():
    assert describe_code(0) == "晴"
    assert describe_code(61) == "小雨"
    assert describe_code(999) == "说不准"
    assert describe_code(None) == "说不准"


def test_render_includes_rain_and_holiday():
    data = {
        "her": {"temp": 18.5, "code": 61, "rain": 90},
        "him": {"temp": 23.4, "code": 1, "rain": 10},
        "holiday": {"name": "国庆节", "off": True},
    }
    line = World.render(data)
    assert "小雨，18℃" in line
    assert "降雨概率 90%" in line
    assert "23℃" in line
    assert "国庆节假期" in line


def test_render_empty_if_no_data():
    assert World.render({}) == ""


async def test_refresh_then_line(tmp_path):
    db = Database(tmp_path / "w.db")
    fetch = FakeFetch()
    world = World(db, _config(), "Asia/Shanghai", fetch)

    await world.refresh(NOW)
    line = await world.line(NOW)

    assert "18℃" in line
    assert "国庆节假期" in line
    assert len(fetch.calls) == 2  # 天气 + 节假日
    db.close()


async def test_line_reads_cache_without_network(tmp_path):
    db = Database(tmp_path / "w.db")
    fetch = FakeFetch()
    world = World(db, _config(), "Asia/Shanghai", fetch)
    await world.refresh(NOW)

    # 30 分钟内再问，不该再发请求
    await world.line(NOW)
    assert len(fetch.calls) == 2
    db.close()


async def test_naive_clock_does_not_crash(tmp_path):
    """回归：naive 的 now 和 aware 的缓存时间相减曾经抛 TypeError。"""
    from datetime import datetime as _dt

    db = Database(tmp_path / "w.db")
    world = World(db, _config(), "Asia/Shanghai", FakeFetch())
    await world.refresh(NOW)  # NOW 是 naive

    naive_now = _dt(2026, 10, 20, 10, 5)
    line = await world.line(naive_now)  # 不能抛
    assert "18℃" in line
    db.close()


async def test_disabled_returns_empty_and_never_fetches(tmp_path):
    db = Database(tmp_path / "w.db")
    fetch = FakeFetch()
    world = World(db, _config(enabled=False), "Asia/Shanghai", fetch)

    assert await world.refresh(NOW) == {}
    assert await world.line(NOW) == ""
    assert fetch.calls == []
    db.close()


async def test_failure_degrades_silently(tmp_path):
    db = Database(tmp_path / "w.db")
    world = World(db, _config(), "Asia/Shanghai", FakeFetch(fail=True))

    assert await world.refresh(NOW) == {}
    assert await world.line(NOW) == ""
    db.close()


def test_load_world_config_missing_file(tmp_path):
    config = load_world_config(tmp_path / "nope.yaml")
    assert config.enabled is False


def test_load_world_config_real_file():
    from app.config import ROOT

    config = load_world_config(ROOT / "config" / "world.yaml")
    assert config.enabled is True
    assert config.her["city"] == "成都"
    assert config.him["city"] == "上海"
