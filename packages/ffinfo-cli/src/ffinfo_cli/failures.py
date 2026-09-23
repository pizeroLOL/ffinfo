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
    "machine_from_argv",
    "set_machine",
    "warn",
]

_machine: bool = False
"""当前是不是 ``-j/--json`` 模式 —— root callback 每次解析后刷新，见 :func:`set_machine`。"""


def set_machine(machine: bool) -> None:
    """记下已解析的输出模式 —— 失败/警告的渲染跟着它走（命令层则读 ``ctx.obj``）。"""
    global _machine
    _machine = machine


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


def fail_abort(exc: BaseException) -> NoReturn:
    """用户中断（Ctrl-C / 授权码接收被掐断）—— 退出码 130（POSIX 128+SIGINT）。

    ``click.Abort`` 与 ``typer.Abort`` 是两个互不为子类的信号类（``typer.prompt``
    抛后者），外加裸 ``KeyboardInterrupt`` —— 都归这一档。消息固定：中断不是
    系统故障，异常本身通常不带可读文本。
    """
    emit_error({"error": {"code": "aborted", "message": "用户中断（Ctrl-C）——命令未完成。"}})
    raise typer.Exit(code=130) from exc


def machine_from_argv(argv: Sequence[str]) -> bool:
    """从原始 argv 里 best-effort 认出 ``-j`` / ``--json``。

    解析失败时 ``ctx.obj`` 不可信 —— root callback 可能没跑，也可能已经按“没看见 ``-j``”
    的解析结果跑过。模式只能从 argv 判；认不出就当人读。
    """
    return any(arg in ("-j", "--json") for arg in argv)


class _UsageAwareGroup(TyperGroup):
    """把 Click/Typer **自己**解析阶段的 ``UsageError`` 接进同一套失败渲染。

    Typer 只给了 ``TyperGroup`` 这个夹具：真实入口和 ``CliRunner`` 调的都是它生成的
    Click group 的 ``main``，而不是 ``Typer`` 对象本身。这里用 ``standalone_mode=False``
    调用父类 —— 否则 Click 会在父类里就把 ``UsageError`` 打成人读文本并 ``sys.exit``，
    轮不到我们。``--help`` / ``--version`` / 补全抛的是 ``Exit``，照旧穿透。
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
        machine = machine_from_argv(args if args is not None else sys.argv[1:])
        set_machine(machine)
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
            # 子命令解析失败时 root callback 已经跑过、并按“没看见 -j”刷过一遍；
            # 以 argv 扫描为准重新覆盖，再渲染。
            set_machine(machine)
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
