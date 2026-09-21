"""CLI 的失败契约：分档退出码 + 错误 JSON。

成功时 stdout 只有结果；失败时 stdout 为空、stderr 是错误 JSON，退出码分档 ——
**这是给 agent 的契约**，``README.md`` 里有同一张表。

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

__all__ = ["emit_error", "error_payload", "fail", "fail_usage", "guard", "warn"]

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
    """错误 JSON 走 stderr —— stdout 上永远只有成功的那份结果。"""
    typer.echo(json.dumps(payload, ensure_ascii=False), err=True)


def warn(message: str) -> None:
    """警告走 stderr：**不是失败**（退出码照旧），但人得看得见 —— 比如老库收敛了重复行。"""
    typer.echo(f"警告：{message}", err=True)


def fail(exc: FfinfoError, *, note: str = "") -> NoReturn:
    """失败也机器可读：分档退出码 + stderr 上的错误 JSON。"""
    code, payload = error_payload(exc)
    if note:
        payload["error"]["message"] = f"{payload['error']['message']}{note}"
    emit_error(payload)
    raise typer.Exit(code=code) from exc


def fail_usage(message: str) -> NoReturn:
    """用法错误 —— 与其它失败共用一套 JSON 外壳；退出码 2 与 typer 自己的口径一致。"""
    emit_error({"error": {"code": "usage", "message": message}})
    raise typer.Exit(code=2)


def guard[ReportT](call: Callable[[], ReportT], *, backoff_note: str = "") -> ReportT:
    """跑一次业务调用；失败就按契约翻译（分档退出码 + stderr 错误 JSON）。

    ``backoff_note`` 追加在退避消息后面 —— 只有 sync 用（"库里没动任何东西"）。
    """
    try:
        return call()
    except BackoffError as exc:
        # 退避不是错误，是"现在别来" —— 顺带告诉 agent 库里没动过，重试是安全的
        fail(exc, note=backoff_note)
    except FfinfoError as exc:
        fail(exc)
