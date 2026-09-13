"""Firefox 的微秒时间戳 ↔ UTC ``datetime``。

Firefox 把时间统一存成 PRTime（**微秒**级的 Unix 时间戳）：本地 ``places.sqlite`` 的
``moz_historyvisits.visit_date``、云端同步记录里的 ``visits[].date`` 都是它。

**一律走整数运算，不写 ``value / 1_000_000``**：1.7e15 这个量级上浮点除法会差零点几微秒。
合并"云端"与"本地"两个源靠的就是时间戳**逐微秒相等** —— 差 1 微秒，同一次访问就会出两行。

三个 collection 的时间单位**不一样**（历史微秒、标签页秒、书签毫秒），这里是历史的那个。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

__all__ = ["from_microseconds", "to_microseconds"]

_MICROSECONDS: Final = 1_000_000
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def to_microseconds(moment: datetime) -> int:
    """UTC ``datetime`` → 微秒。"""
    delta = moment.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS + delta.microseconds


def from_microseconds(value: int) -> datetime:
    """微秒 → UTC ``datetime``。"""
    seconds, micros = divmod(value, _MICROSECONDS)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=micros)
