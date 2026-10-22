"""时间工具。

库里存的时间戳有时带时区、有时不带（测试、手工数据都会出现）。
直接相减会抛 TypeError: can't subtract offset-naive and offset-aware datetimes，
所以比较前统一对齐。
"""

from __future__ import annotations

from datetime import datetime


def align(stamp: datetime, ref: datetime) -> datetime:
    """把 stamp 的时区标记对齐到 ref；只补/去 tzinfo，不改时刻。"""
    if (stamp.tzinfo is None) != (ref.tzinfo is None):
        return stamp.replace(tzinfo=ref.tzinfo)
    return stamp
