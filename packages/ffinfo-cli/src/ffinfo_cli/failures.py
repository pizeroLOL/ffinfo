"""CLI 的失败契约：分档退出码 + 错误输出**跟随模式**。

成功时 stdout 只有结果；失败时 stdout 为空，stderr 上是：

* ``-j/--json`` —— 机器可读的错误 JSON
* 默认 —— 一行人读的 ``错误：…``

**退出码分档两种模式一样** —— 这是给 agent 的契约，``README.md`` 里有同一张表。
模式由 root callback 解析后 :func:`set_machine` 进来；命令层从 ``ctx.obj`` 读。

放在这里而不是 ``cli.py``：命令 module 要 import 它、装配点也要 import 它，
放 ``cli.py`` 会让两边成环。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Final, NoReturn

import typer

from ffinfo.errors import (
    AuthError,
    BackoffError,
    ConfigurationError,
    DecryptionError,
    FfinfoError,
    KeyDerivationError,
    SyncProtocolError,
)

__all__ = [
    "emit_error",
    "error_payload",
    "fail",
    "fail_usage",
    "guard",
    "set_machine",
    "warn",
]

_machine: bool = False
"""当前是不是 ``-j/--json`` 模式 —— root callback 每次解析后刷新，见 :func:`set_machine`。"""


def set_machine(machine: bool) -> None:
    """记下已解析的输出模式 —— 失败/警告的渲染跟着它走（命令层则读 ``ctx.obj``）。"""
    global _machine
    _machine = machine


_EXIT_CODES: Final[tuple[tuple[type[FfinfoError], int, str], ...]] = (
    (ConfigurationError, 3, "configuration"),
    (AuthError, 4, "auth"),
    (BackoffError, 5, "backoff"),
    (SyncProtocolError, 6, "protocol"),
    (DecryptionError, 7, "decryption"),
    (KeyDerivationError, 8, "key_derivation"),
)
"""异常 → (退出码, 错误码)。**这是给 agent 的契约**，README 里有同一张表。"""


def error_payload(exc: FfinfoError) -> tuple[int, dict[str, Any]]:
    """异常 → (退出码, 错误 JSON)。没登记的异常落到兜底档 —— 消息绝不丢。"""
    for klass, code, name in _EXIT_CODES:
        if isinstance(exc, klass):
            error: dict[str, Any] = {"code": name, "message": str(exc)}
            if isinstance(exc, BackoffError):
                error["wait_seconds"] = exc.wait_seconds
                error["soft"] = exc.soft
            return code, {"error": error}
    return 1, {"error": {"code": "error", "message": str(exc)}}


def emit_error(payload: dict[str, Any]) -> None:
    """错误走 stderr —— stdout 上永远只有成功的那份结果；形态跟着模式走。"""
    if _machine:
        typer.echo(json.dumps(payload, ensure_ascii=False), err=True)
    else:
        typer.echo(f"错误：{payload['error']['message']}", err=True)


def warn(message: str) -> None:
    """警告走 stderr：**不是失败**（退出码照旧），但人得看得见 —— 比如老库收敛了重复行。

    形态跟随模式：``-j`` 时是一行 warning JSON，默认是 ``警告：…``。
    """
    if _machine:
        typer.echo(json.dumps({"warning": {"message": message}}, ensure_ascii=False), err=True)
    else:
        typer.echo(f"警告：{message}", err=True)


def fail(exc: FfinfoError, *, note: str = "") -> NoReturn:
    """失败：分档退出码 + stderr 上的错误输出（形态跟随模式）。"""
    code, payload = error_payload(exc)
    if note:
        payload["error"]["message"] = f"{payload['error']['message']}{note}"
    emit_error(payload)
    raise typer.Exit(code=code) from exc


def fail_usage(message: str) -> NoReturn:
    """用法错误 —— 与其它失败共用同一层外壳；退出码 2 与 typer 自己的口径一致。"""
    emit_error({"error": {"code": "usage", "message": message}})
    raise typer.Exit(code=2)


def guard[ReportT](call: Callable[[], ReportT], *, backoff_note: str = "") -> ReportT:
    """跑一次业务调用；失败就按契约翻译（分档退出码 + stderr 错误输出）。"""
    try:
        return call()
    except BackoffError as exc:
        # 退避不是错误，是"现在别来" —— 顺带告诉 agent 库里没动过，重试是安全的
        fail(exc, note=backoff_note)
    except FfinfoError as exc:
        fail(exc)
