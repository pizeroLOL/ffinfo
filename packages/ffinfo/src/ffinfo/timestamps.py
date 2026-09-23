"""Firefox 的时间戳 ↔ UTC ``datetime`` —— **三个单位，一个 module**。

Firefox 把时间存成 Unix 时间戳，但**三个 collection 三个单位**：

* 历史（``visits[].date``、``moz_historyvisits.visit_date``）—— **微秒**（PRTime）
* 书签（``dateAdded``）—— **毫秒**
* 标签页（``lastUsed``）—— **秒**

混了单位时间就飘到 1970 或五万年后 —— 所以**单位写进函数名**，调用点没有机会猜错。

**一律走整数运算，不写 ``value / 1_000_000``**：1.7e15 这个量级上浮点除法会差零点几微秒。
合并"云端"与"firefox"两个源靠的就是时间戳**逐微秒相等** —— 差 1 微秒，同一次访问就会出两行。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from ffinfo.errors import ConfigurationError, TimestampError

__all__ = [
    "failure_reason",
    "from_microseconds",
    "from_milliseconds",
    "from_seconds",
    "to_microseconds",
]

_MICROSECONDS: Final = 1_000_000
_MILLISECONDS: Final = 1_000
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_OUT_OF_RANGE: Final = "时间戳超出可表示范围"


def from_microseconds(value: int) -> datetime:
    """微秒 → UTC ``datetime``（历史）。"""
    seconds, micros = divmod(value, _MICROSECONDS)
    return _from_unix(seconds, microsecond=micros, raw=value)


def from_milliseconds(value: int) -> datetime:
    """毫秒 → UTC ``datetime``（书签）。"""
    seconds, millis = divmod(value, _MILLISECONDS)
    return _from_unix(seconds, microsecond=millis * 1_000, raw=value)


def from_seconds(value: int) -> datetime:
    """秒 → UTC ``datetime``（标签页）。"""
    return _from_unix(value, raw=value)


def failure_reason(exc: BaseException) -> str:
    """换算失败落到报告里的一行短原因 —— ``skipped`` / ``dropped`` 共用这一个口径。"""
    return str(exc) if isinstance(exc, TimestampError) else _OUT_OF_RANGE


def _from_unix(seconds: int, *, raw: int, microsecond: int = 0) -> datetime:
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microsecond)
    except (ValueError, OSError, OverflowError) as exc:
        raise TimestampError(f"{_OUT_OF_RANGE}: {raw}") from exc


def to_microseconds(moment: datetime) -> int:
    """aware ``datetime`` → 微秒 —— 双源合并的键就是它。

    **naive 输入拒收**：``astimezone`` 对 naive 会悄悄按机器本地时区补时区，
    同一个 naive 值在 ``TZ=Asia/Shanghai`` 与 ``TZ=UTC`` 下差 8 小时 —— 合并键
    不许随运行机器漂。要按 UTC 解释就显式 ``moment.replace(tzinfo=UTC)``。
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        msg = (
            f"to_microseconds 需要带时区的 datetime，收到 naive 的 {moment.isoformat()!r}"
            " —— 按 UTC 解释请传 moment.replace(tzinfo=UTC)，"
            "naive 输入在不同 TZ 下会差几小时"
        )
        raise ConfigurationError(msg)
    delta = moment.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS + delta.microseconds
