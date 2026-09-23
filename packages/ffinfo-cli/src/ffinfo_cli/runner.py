"""命令 edge 的共享 runner —— ``asyncio.run`` 与 httpx 客户端生命周期只在这一处。

每个操作只有一份公开 async interface（``run_*`` / ``build_report``，形参上注入
``http`` / ``clock`` / ``warn`` 供测试喂假）；同步薄包装（``*_blocking`` /
``login_sync``）已经删掉。命令层把协程交给这里跑：

* :func:`run` —— 纯本地操作（``list`` 三种 · export/import · profiles），
  起一个新事件循环跑完；
* :func:`run_with_http` —— 联网操作（``sync`` · ``login``），先开
  ``httpx.AsyncClient`` 再跑，正常与异常路径都由 ``async with`` 关闭。

``asyncio.run`` 在整个 ``src`` 里只出现在本模块 —— 命令层不再各写一份外壳。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Final

import httpx

__all__ = ["HTTP_TIMEOUT_SECONDS", "LOGIN_TIMEOUT_SECONDS", "run", "run_with_http"]

HTTP_TIMEOUT_SECONDS: Final = 60.0
"""``sync`` 拉数据的 HTTP 超时（秒）—— :func:`run_with_http` 的默认值。"""

LOGIN_TIMEOUT_SECONDS: Final = 30.0
"""``login`` 授权 / 换码的 HTTP 超时（秒）—— 交互链路比批量拉取更快失败。"""


def run[ReportT](op: Callable[[], Coroutine[Any, Any, ReportT]]) -> ReportT:
    """在新事件循环里跑一次**纯本地**操作 —— 命令 edge 的同步外壳。"""
    return asyncio.run(op())


def run_with_http[ReportT](
    op: Callable[[httpx.AsyncClient], Coroutine[Any, Any, ReportT]],
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> ReportT:
    """开一个 ``httpx.AsyncClient`` → 跑操作 → 关闭（异常路径也关）。

    ``sync`` / ``login`` 的真实客户端生命周期都走这里 —— 操作本体只拿注入的
    ``http``，不自己开闭；``timeout`` 由命令按操作传（login 见
    :data:`LOGIN_TIMEOUT_SECONDS`）。
    """

    async def main() -> ReportT:
        async with httpx.AsyncClient(timeout=timeout) as http:
            return await op(http)

    return asyncio.run(main())
