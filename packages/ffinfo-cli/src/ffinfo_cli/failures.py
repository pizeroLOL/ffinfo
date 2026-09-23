"""CLI 的失败契约：分档退出码 + 错误输出**跟随模式**。

成功时 stdout 只有结果；失败时 stdout 为空，stderr 上是：

* ``-j/--json`` —— 机器可读的错误 JSON
* 默认 —— 一行人读的 ``错误：…``

**退出码分档两种模式一样** —— 这是给 agent 的契约，``README.md`` 里有同一张表。

「这次是不是 ``-j``」是**一个值**：``_UsageAwareGroup.main`` 进门时用
:func:`initialize_mode` 从 argv 扫一遍存下（唯一写入点）；成功渲染（命令层
``render(..., machine=machine_mode())``）与失败/警告 emit 经 :func:`machine_mode`
读同一份 —— 没有第二份副本，也没有「刷错再补」。

放在这里而不是 ``cli.py``：命令 module 要 import 它、装配点也要 import 它，
放 ``cli.py`` 会让两边成环。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, Final, NoReturn

import typer
from click.exceptions import Abort as ClickAbort
from typer.core import TyperGroup

try:  # Typer 0.16+ 自带一份 click（``typer._click``）；老版本 Typer 直接复用 click 包
    from typer._click.exceptions import NoArgsIsHelpError, UsageError
except ImportError:  # pragma: no cover - 只对老版本 Typer 生效
    from click.exceptions import NoArgsIsHelpError, UsageError

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
    "EXIT_CODES",
    "CliTyper",
    "emit_error",
    "error_payload",
    "fail",
    "fail_abort",
    "fail_usage",
    "guard",
    "initialize_mode",
    "machine_mode",
    "warn",
]

_machine: bool = False
"""「这次是不是 ``-j/--json``」的唯一存储 —— 只有 :func:`initialize_mode` 写它。"""


def initialize_mode(argv: Sequence[str]) -> None:
    """从原始 argv 认出 ``-j`` / ``--json``，存成这次运行的输出模式 —— **唯一写入点**。

    在 :class:`_UsageAwareGroup` 的 ``main`` 进门时调用（Click 解析之前）：解析成功、
    解析失败、root callback 没跑 —— 三条路都读这同一份。认不出就当人读。
    """
    global _machine
    _machine = any(arg in ("-j", "--json") for arg in argv)


def machine_mode() -> bool:
    """当前输出模式 —— 成功渲染与失败/警告 emit 经这个 seam 读同一个值。"""
    return _machine


EXIT_CODES: Final[tuple[tuple[type[FfinfoError], int, str], ...]] = (
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
    for klass, code, name in EXIT_CODES:
        if isinstance(exc, klass):
            error: dict[str, Any] = {"code": name, "message": str(exc)}
            if isinstance(exc, BackoffError):
                error["wait_seconds"] = exc.wait_seconds
                error["soft"] = exc.soft
            return code, {"error": error}
    return 1, {"error": {"code": "error", "message": str(exc)}}


def emit_error(payload: dict[str, Any]) -> None:
    """错误走 stderr —— stdout 上永远只有成功的那份结果；形态跟着 :func:`machine_mode`。"""
    if machine_mode():
        typer.echo(json.dumps(payload, ensure_ascii=False), err=True)
    else:
        typer.echo(f"错误：{payload['error']['message']}", err=True)


def warn(message: str) -> None:
    """警告走 stderr：**不是失败**（退出码照旧），但人得看得见 —— 比如老库收敛了重复行。

    形态跟随模式：``-j`` 时是一行 warning JSON，默认是 ``警告：…``。
    """
    if machine_mode():
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


def fail_abort(exc: BaseException) -> NoReturn:
    """用户中断（Ctrl-C / 授权码接收被掐断）—— 退出码 130（POSIX 128+SIGINT）。

    ``click.Abort`` 与 ``typer.Abort`` 是两个互不为子类的信号类（``typer.prompt``
    抛后者），外加裸 ``KeyboardInterrupt`` —— 都归这一档。消息固定：中断不是
    系统故障，异常本身通常不带可读文本。
    """
    emit_error({"error": {"code": "aborted", "message": "用户中断（Ctrl-C）——命令未完成。"}})
    raise typer.Exit(code=130) from exc


class _UsageAwareGroup(TyperGroup):
    """把 Click/Typer **自己**解析阶段的 ``UsageError`` 接进同一套失败渲染。

    Typer 只给了 ``TyperGroup`` 这个夹具：真实入口和 ``CliRunner`` 调的都是它生成的
    Click group 的 ``main``，而不是 ``Typer`` 对象本身。这里用 ``standalone_mode=False``
    调用父类 —— 否则 Click 会在父类里就把 ``UsageError`` 打成人读文本并 ``sys.exit``，
    轮不到我们。``--help`` / ``--version`` / 补全抛的是 ``Exit``，照旧穿透。

    ``main`` 进门先 :func:`initialize_mode` —— 这次运行的输出模式在 Click 解析前
    就定好，解析错误、命令体、成功渲染读的都是这一份。
    """

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: object,
    ) -> object:
        initialize_mode(args if args is not None else sys.argv[1:])
        try:
            result = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except UsageError as exc:
            # 无子命令的 ``no_args_is_help`` 已经自己把 help 打了；别再当成错误重复一遍
            if not isinstance(exc, NoArgsIsHelpError):
                emit_error({"error": {"code": "usage", "message": exc.format_message()}})
            raise SystemExit(exc.exit_code) from exc
        # 命令体里的 ``typer.Exit``（fail / fail_usage）在非 standalone 模式下变成返回值
        if isinstance(result, int) and not isinstance(result, bool) and result != 0:
            raise SystemExit(result)
        return result


class CliTyper(typer.Typer):
    """``ffinfo-cli`` 的 Typer app —— 生成的使用错误都走 :class:`_UsageAwareGroup`。"""

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401 - 原样透传给 Typer
        """建 app；``cls`` 默认换成会接住解析错误的 :class:`_UsageAwareGroup`。"""
        kwargs.setdefault("cls", _UsageAwareGroup)
        super().__init__(**kwargs)


def guard[ReportT](call: Callable[[], ReportT], *, backoff_note: str = "") -> ReportT:
    """跑一次业务调用；失败就按契约翻译（分档退出码 + stderr 错误输出）。

    用户中断也在这里接住 —— 交互路径（``typer.prompt`` 收授权码、登录中 Ctrl-C）
    抛的 ``Abort`` / ``KeyboardInterrupt`` 不是 ``FfinfoError``，漏出去就是裸退出码
    加空流，把失败契约凿穿。
    """
    try:
        return call()
    except BackoffError as exc:
        # 退避不是错误，是"现在别来" —— 顺带告诉 agent 库里没动过，重试是安全的
        fail(exc, note=backoff_note)
    except FfinfoError as exc:
        fail(exc)
    except (ClickAbort, typer.Abort, KeyboardInterrupt) as exc:
        fail_abort(exc)
