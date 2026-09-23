"""Firefox 时间戳 ↔ datetime：三个单位，一个 module。

数字取"整十亿秒"那种好记的；另外专门盯住**微秒逐位相等**这条纪律（双源合并的前提）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ffinfo.errors import ConfigurationError, FfinfoError, TimestampError
from ffinfo.timestamps import (
    from_microseconds,
    from_milliseconds,
    from_seconds,
    to_microseconds,
)

EPOCH_SECONDS = 1_700_000_000
"""2023-11-14T22:13:20+00:00 —— 整十亿秒，好记。"""


def test_microseconds_are_exact() -> None:
    """历史：微秒（PRTime）。"""
    moment = from_microseconds(EPOCH_SECONDS * 1_000_000)

    assert moment == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_milliseconds_keep_their_sub_second() -> None:
    """书签：毫秒 —— 丢掉毫秒就不是"毫秒级"了。"""
    moment = from_milliseconds(EPOCH_SECONDS * 1_000 + 123)

    assert moment == datetime(2023, 11, 14, 22, 13, 20, 123_000, tzinfo=UTC)


def test_seconds_have_no_sub_second() -> None:
    """标签页：秒。"""
    moment = from_seconds(EPOCH_SECONDS)

    assert moment == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert moment.microsecond == 0


def test_microsecond_round_trip_is_bit_exact() -> None:
    """双源合并靠它 —— 差 1 微秒，同一次访问就会出两行。"""
    for value in (
        EPOCH_SECONDS * 1_000_000,
        EPOCH_SECONDS * 1_000_000 + 1,
        EPOCH_SECONDS * 1_000_000 + 999_999,
        1_789_320_612_345_678,
    ):
        assert to_microseconds(from_microseconds(value)) == value


def test_to_microseconds_accepts_other_timezones() -> None:
    """带时区的 datetime 进来，换算的是**同一时刻**。"""
    moment = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)

    assert to_microseconds(moment.astimezone(timezone(timedelta(hours=8)))) == (
        EPOCH_SECONDS * 1_000_000
    )


@pytest.mark.parametrize("tz_name", ["Asia/Shanghai", "UTC"])
def test_naive_datetime_is_rejected_regardless_of_local_timezone(
    monkeypatch: pytest.MonkeyPatch, tz_name: str
) -> None:
    """翻转前：naive 按**机器本地时区**解释 —— 同一个值在 ``TZ=Asia/Shanghai`` 下
    与 aware UTC 版差 8 小时（28_800_000_000 微秒），双源合并的键随机器漂。

    现在：naive **稳定拒绝**（``ConfigurationError``），aware 结果也不随 ``TZ`` 漂 ——
    两种 ``TZ`` 下同一输入得到同一输出。
    """
    import time

    monkeypatch.setenv("TZ", tz_name)
    time.tzset()
    try:
        naive = datetime(2023, 11, 14, 22, 13, 20)
        with pytest.raises(ConfigurationError, match="时区"):
            to_microseconds(naive)

        aware = naive.replace(tzinfo=UTC)
        assert to_microseconds(aware) == EPOCH_SECONDS * 1_000_000
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize(
    "convert",
    [from_microseconds, from_milliseconds, from_seconds],
    ids=["microseconds", "milliseconds", "seconds"],
)
def test_out_of_range_value_raises_timestamp_error(convert: Callable[[int], datetime]) -> None:
    """超范围值抛的是 ``TimestampError``（``FfinfoError`` 子类），不是裸 ``ValueError``。"""
    with pytest.raises(TimestampError) as excinfo:
        convert(10**18)

    assert isinstance(excinfo.value, FfinfoError)
    assert not isinstance(excinfo.value, ValueError)
