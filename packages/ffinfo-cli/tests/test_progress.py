"""sync 的进度提示（12 号 ticket）。

三条规矩各有一条测试守着：**只写 stderr**、**非 TTY 不刷同一行**、
**关掉时一个字都不写**。显示格式本身也钉住 —— 它是给人在终端里看的，不是契约，
但"第几页 / 多少条 / 用时"这三样一个都不能少。
"""

from __future__ import annotations

import io

from ffinfo.storage import FetchProgress
from ffinfo_cli.progress import ProgressReporter, reporter_for


class Clock:
    """手动时钟 —— "用时 3.5s" 不能靠真的等 3.5 秒。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def progress(pages: int = 1, records: int = 100, collection: str = "history") -> FetchProgress:
    return FetchProgress(collection=collection, pages=pages, records=records)


def reporter(
    *, interactive: bool, clock: Clock | None = None
) -> tuple[ProgressReporter, io.StringIO]:
    stream = io.StringIO()
    return (
        ProgressReporter(stream=stream, interactive=interactive, clock=clock or Clock()),
        stream,
    )


def test_interactive_keeps_it_on_one_line() -> None:
    """TTY 上是一行自己刷新 —— 不是刷屏。"""
    instance, stream = reporter(interactive=True)

    instance(progress(1, 100))
    instance(progress(2, 250))

    text = stream.getvalue()
    assert text.count("\r") == 2
    assert "\n" not in text


def test_finish_puts_the_cursor_on_a_new_line() -> None:
    instance, stream = reporter(interactive=True)
    instance(progress())

    instance.finish()

    assert stream.getvalue().endswith("\n")


def test_finish_is_a_no_op_when_nothing_was_written() -> None:
    """第一页就失败 —— 别在终端上留个空行。"""
    instance, stream = reporter(interactive=True)

    instance.finish()

    assert stream.getvalue() == ""


def test_non_interactive_writes_plain_lines() -> None:
    """被管道接的时候，``\\r`` 不是动画，是一堆控制字符 —— 一条都不许有。"""
    instance, stream = reporter(interactive=False)

    instance(progress(1, 100))
    instance(progress(2, 250))

    text = stream.getvalue()
    assert "\r" not in text
    assert text.count("\n") == 2


def test_non_interactive_finish_adds_nothing() -> None:
    instance, stream = reporter(interactive=False)
    instance(progress())

    instance.finish()

    assert stream.getvalue().count("\n") == 1


def test_line_says_page_records_and_elapsed() -> None:
    clock = Clock()
    instance, stream = reporter(interactive=True, clock=clock)
    clock.now = 3.5

    instance(progress(7, 640))

    text = stream.getvalue()
    assert "history" in text
    assert "第 7 页" in text
    assert "640" in text
    assert "3.5s" in text


def test_elapsed_counts_from_construction() -> None:
    """用时是"从开始拉到现在"，不是"上一页到这一页"。"""
    clock = Clock()
    clock.now = 10.0
    instance, stream = reporter(interactive=False, clock=clock)
    clock.now = 12.5

    instance(progress())

    assert "2.5s" in stream.getvalue()


def test_switching_collections_erases_the_old_line() -> None:
    """先 history 后 crypto —— 上一行比这一行长的时候不留残影。"""
    instance, stream = reporter(interactive=True)

    instance(progress(50, 4900, collection="history"))
    instance(progress(1, 1, collection="crypto"))

    assert "\x1b[K" in stream.getvalue()


def test_reporter_for_is_none_when_disabled() -> None:
    """关掉时返回 ``None`` —— 调用方直接把它当"没有回调"传下去，不用到处判空。"""
    assert reporter_for(io.StringIO(), enabled=False) is None


def test_reporter_for_follows_the_stream() -> None:
    """开关打开时，交互与否**由流自己说了算** —— StringIO 不是 TTY，所以走平铺那一支。"""
    stream = io.StringIO()
    instance = reporter_for(stream, enabled=True)
    assert instance is not None

    instance(progress())

    assert "\r" not in stream.getvalue()
