"""真实世界感知：天气 + 节假日。

数据源都免费、不用 Key：
  - 天气：Open-Meteo（一次请求查两个城市）
  - 节假日：holiday-cn 的年度 JSON（含调休与补班）

两条原则：
  1. 拉不到就当没有，绝不让对话因为网络挂掉
  2. **不在回复路径上等网络**：只读缓存，过期了丢给后台任务刷新
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import yaml

from .db import Database
from .persona import now_local
from .timeutil import align

logger = logging.getLogger("aigirl.world")

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
HOLIDAY_URL = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
STATE_KEY = "world_snapshot"

# WMO weather code → 短中文
_CODES = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "有雾",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "阵雪",
    95: "雷阵雨",
    96: "雷阵雨",
    99: "雷暴",
}

Fetcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def describe_code(code: int | None) -> str:
    if code is None:
        return "说不准"
    return _CODES.get(int(code), "说不准")


@dataclass(slots=True)
class WorldConfig:
    enabled: bool = True
    refresh_minutes: int = 30
    her: dict[str, Any] = field(default_factory=dict)
    him: dict[str, Any] = field(default_factory=dict)
    holidays: bool = True


def load_world_config(path: Path) -> WorldConfig:
    if not path.exists():
        return WorldConfig(enabled=False)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("world.yaml 解析失败：%s", exc)
        return WorldConfig(enabled=False)
    return WorldConfig(
        enabled=bool(raw.get("enabled", True)),
        refresh_minutes=max(5, int(raw.get("refresh_minutes", 30))),
        her=dict(raw.get("her") or {}),
        him=dict(raw.get("him") or {}),
        holidays=bool(raw.get("holidays", True)),
    )


async def _default_fetcher(url: str, params: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=6) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


class World:
    """世界快照：只读缓存，后台刷新，全程不阻塞回复。"""

    def __init__(
        self,
        db: Database,
        config: WorldConfig,
        tz: str = "Asia/Shanghai",
        fetcher: Fetcher | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.tz = tz
        self._fetch = fetcher or _default_fetcher
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------- 缓存

    async def _cached(self, now: datetime) -> tuple[dict[str, Any], bool]:
        cached = await self.db.get_state(STATE_KEY)
        if not cached:
            return {}, False
        return cached.get("data") or {}, self._fresh(cached.get("at"), now)

    def _fresh(self, at: str | None, now: datetime) -> bool:
        if not at:
            return False
        try:
            stamp = datetime.fromisoformat(at)
        except ValueError:
            return False
        return (now - align(stamp, now)) < timedelta(minutes=self.config.refresh_minutes)

    # ---------------------------------------------------------- 刷新

    async def refresh(self, now: datetime | None = None) -> dict[str, Any]:
        if not self.config.enabled:
            return {}
        now = now or now_local(self.tz)
        data: dict[str, Any] = {}
        try:
            data.update(await self._weather())
        except Exception as exc:
            logger.info("天气获取失败：%s", exc)
        if self.config.holidays:
            try:
                holiday = await self._holiday(now)
                if holiday:
                    data["holiday"] = holiday
            except Exception as exc:
                logger.info("节假日获取失败：%s", exc)
        if data:
            await self.db.set_state(STATE_KEY, {"at": now.isoformat(), "data": data})
        return data

    async def warm(self) -> None:
        """启动时调一次，把缓存填上；失败也不影响启动。"""
        if not self.config.enabled:
            return
        try:
            await self.refresh()
        except Exception as exc:
            logger.info("预热失败：%s", exc)

    def _schedule_refresh(self, now: datetime) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._safe_refresh(now))

    async def _safe_refresh(self, now: datetime) -> None:
        try:
            await self.refresh(now)
        except Exception as exc:
            logger.info("后台刷新失败：%s", exc)

    # ---------------------------------------------------------- 数据源

    async def _weather(self) -> dict[str, Any]:
        her, him = self.config.her, self.config.him
        if not her.get("latitude"):
            return {}
        params = {
            "latitude": f"{her['latitude']},{him.get('latitude', her['latitude'])}",
            "longitude": f"{her['longitude']},{him.get('longitude', her['longitude'])}",
            "current": "temperature_2m,weather_code",
            "daily": "precipitation_probability_max",
            "timezone": self.tz,
            "forecast_days": 1,
        }
        raw = await self._fetch(WEATHER_URL, params)
        blocks = raw if isinstance(raw, list) else [raw]
        if not blocks:
            return {}

        def pick(block: dict[str, Any]) -> dict[str, Any]:
            current = block.get("current") or {}
            daily = block.get("daily") or {}
            rains = daily.get("precipitation_probability_max") or []
            return {
                "temp": current.get("temperature_2m"),
                "code": current.get("weather_code"),
                "rain": rains[0] if rains else None,
            }

        out: dict[str, Any] = {"her": {"city": her.get("city", "她那"), **pick(blocks[0])}}
        if len(blocks) > 1:
            out["him"] = {"city": him.get("city", "他那边"), **pick(blocks[1])}
        return out

    async def _holiday(self, now: datetime) -> dict[str, Any]:
        raw = await self._fetch(HOLIDAY_URL.format(year=now.year), {})
        today = now.date().isoformat()
        for day in raw.get("days") or []:
            if day.get("date") == today:
                return {"name": day.get("name"), "off": bool(day.get("isOffDay"))}
        return {}

    # ---------------------------------------------------------- 注入

    @staticmethod
    def _place(prefix: str, place: dict[str, Any]) -> str:
        temp = place.get("temp")
        if temp is None:
            return ""
        line = f"{prefix}{describe_code(place.get('code'))}，{round(float(temp))}℃"
        rain = place.get("rain")
        if rain is not None and int(rain) >= 60:
            line += f"，降雨概率 {int(rain)}%"
        return line

    @staticmethod
    def render(data: dict[str, Any]) -> str:
        if not data:
            return ""
        parts: list[str] = []
        for prefix, key in (("你那边：", "her"), ("他那边：", "him")):
            place = data.get(key) or {}
            line = World._place(prefix, place)
            if line:
                parts.append(line)
        holiday = data.get("holiday") or {}
        if holiday:
            name = holiday.get("name") or ""
            if holiday.get("off"):
                parts.append(f"今天是{name}假期" if name else "今天是假期")
            elif name:
                parts.append(f"今天{name}调休上班")
        return "；".join(parts)

    async def line(self, now: datetime | None = None) -> str:
        """给 prompt 用的一行环境描写。读缓存，绝不等网络。"""
        if not self.config.enabled:
            return ""
        now = now or now_local(self.tz)
        data, fresh = await self._cached(now)
        if not fresh:
            self._schedule_refresh(now)
        return self.render(data)

    async def close(self) -> None:
        """退出时收掉后台刷新任务，不然解释器会报 Task was destroyed。"""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - 收尾，别抛
                pass
        self._task = None
