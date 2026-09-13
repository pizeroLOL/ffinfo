"""Firefox 的时间戳 ↔ UTC ``datetime`` —— **三个单位，一个 module**。

Firefox 把时间存成 Unix 时间戳，但**三个 collection 三个单位**：

* 历史（``visits[].date``、``moz_historyvisits.visit_date``）—— **微秒**（PRTime）
* 书签（``dateAdded``）—— **毫秒**
* 标签页（``lastUsed``）—— **秒**

混了单位时间就飘到 1970 或五万年后 —— 所以**单位写进函数名**，调用点没有机会猜错。

**一律走整数运算，不写 ``value / 1_000_000``**：1.7e15 这个量级上浮点除法会差零点几微秒。
合并"云端"与"本地"两个源靠的就是时间戳**逐微秒相等** —— 差 1 微秒，同一次访问就会出两行。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

__all__ = ["from_microseconds", "from_milliseconds", "from_seconds", "to_microseconds"]

_MICROSECONDS: Final = 1_000_000
_MILLISECONDS: Final = 1_000
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def from_microseconds(value: int) -> datetime:
    """微秒 → UTC ``datetime``（历史）。"""
    seconds, micros = divmod(value, _MICROSECONDS)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=micros)


def from_milliseconds(value: int) -> datetime:
    """毫秒 → UTC ``datetime``（书签）。"""
    seconds, millis = divmod(value, _MILLISECONDS)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=millis * 1_000)


def from_seconds(value: int) -> datetime:
    """秒 → UTC ``datetime``（标签页）。"""
    return datetime.fromtimestamp(value, tz=UTC)


def to_microseconds(moment: datetime) -> int:
    """UTC ``datetime`` → 微秒 —— 双源合并的键就是它。"""
    delta = moment.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS + delta.microseconds
