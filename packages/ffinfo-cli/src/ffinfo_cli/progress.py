r"""``ffinfo-cli sync`` 的进度提示。

全量拉 50 页要跑 30 秒，之前**全程一声不吭** —— 人和 agent 都分不清它是在干活还是卡死了。

三条规矩，一条都不能破：

* **只写 stderr** —— stdout 是给 agent 吃的 JSON，一个字节都不能掺
* **非 TTY 就不刷同一行** —— ``\\r`` 灌进管道不是动画，是一堆控制字符
* **``--no-progress`` 时一个字都不写**
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final, TextIO

from ffinfo.storage import FetchProgress

__all__ = ["ProgressReporter", "reporter_for"]

_ERASE: Final = "\x1b[K"
"""擦到行尾 —— 换 collection 时上一行可能比这一行长，不擦会留残影。"""


class ProgressReporter:
    """把"拉到第几页了"写到 stderr。

    **不做进度条、不算百分比** —— 这是"个人复盘"用的 CLI，不是下载器。
    一行会自己刷新的状态就够。
    """

    __slots__: tuple[str, ...] = ("_clock", "_interactive", "_started", "_stream", "_width")

    def __init__(
        self,
        *,
        stream: TextIO,
        interactive: bool,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``stream`` 传 stderr；``interactive`` 由调用方从流上问出来（别在这儿猜）。"""
        self._stream = stream
        self._interactive = interactive
        self._clock = clock
        self._started = clock()
        self._width = 0

    def __call__(self, progress: FetchProgress) -> None:
        """每翻完一页被调一次。"""
        elapsed = self._clock() - self._started
        line = (
            f"拉取 {progress.collection}：第 {progress.pages} 页，"
            f"已拉 {progress.records} 条，用时 {elapsed:.1f}s"
        )
        if self._interactive:
            self._stream.write(f"\r{line}{_ERASE}")
            self._width = len(line)
        else:
            self._stream.write(f"{line}\n")
        self._stream.flush()

    def finish(self) -> None:
        """收尾：交互模式下把光标挪到下一行。

        **没写过东西就什么都不做** —— 第一页就失败的话，别留个空行在那儿。
        """
        if self._interactive and self._width:
            self._stream.write("\n")
            self._stream.flush()
            self._width = 0


def reporter_for(
    stream: TextIO,
    *,
    enabled: bool,
    clock: Callable[[], float] = time.monotonic,
) -> ProgressReporter | None:
    """按需造一个 —— 关掉时返回 ``None``，调用方直接把它当"没有回调"传下去。"""
    if not enabled:
        return None
    return ProgressReporter(stream=stream, interactive=stream.isatty(), clock=clock)
