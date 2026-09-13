"""ffinfo_cli 命令行入口。

真正的用户是 agent —— 输出以 JSON 为主，人类可读的呈现是次要目标。
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
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
from ffinfo_cli import __version__
from ffinfo_cli.list import DATA_TYPES, list_blocking, parse_since
from ffinfo_cli.login import login_sync
from ffinfo_cli.paths import credentials_path, database_path, identity_path
from ffinfo_cli.profiles import profiles_blocking
from ffinfo_cli.progress import reporter_for
from ffinfo_cli.sync import SYNCABLE_COLLECTIONS, sync_blocking
from ffinfo_cli.transfer import export_blocking, import_blocking

app = typer.Typer(
    name="ffinfo-cli",
    help="把 Firefox 浏览数据（云端 Sync + 本地 places.sqlite）拉到本地 SQLite，输出 JSON。",
    no_args_is_help=True,
    add_completion=False,
)


# 带 Path 标注的参数，默认值必须写成模块级单例 ——
# ruff 的 B008 对"默认值是函数调用"的判定在 Path 上会触发（str / int 反而不会），
# 而 typer 的 Argument / Option 本来就是个函数调用。
_DESTINATION: Final = typer.Argument(..., help="便携文件写到哪（.sqlite）")
_SOURCE: Final = typer.Argument(..., help="export 产出的那份文件")
_PROFILE: Final = typer.Option(
    None, "--profile", help="手动指定 Firefox profile 目录（自动找不到时用）"
)

_MAX_PAGE_SIZE: Final = 100
"""服务器每页的上限 —— ``--page-size`` 越界会被**拒**，不是静默夹取。"""

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


def _emit_error(payload: dict[str, Any]) -> None:
    """错误 JSON 走 stderr —— stdout 上永远只有成功的那份结果。"""
    typer.echo(json.dumps(payload, ensure_ascii=False), err=True)


def _fail(exc: FfinfoError, *, note: str = "") -> NoReturn:
    """失败也机器可读：分档退出码 + stderr 上的错误 JSON。"""
    code, payload = error_payload(exc)
    if note:
        payload["error"]["message"] = f"{payload['error']['message']}{note}"
    _emit_error(payload)
    raise typer.Exit(code=code) from exc


def _fail_usage(message: str) -> NoReturn:
    """用法错误 —— 与其它失败共用一套 JSON 外壳；退出码 2 与 typer 自己的口径一致。"""
    _emit_error({"error": {"code": "usage", "message": message}})
    raise typer.Exit(code=2)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ffinfo-cli {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="显示版本后退出",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ffinfo_cli —— Firefox 数据到本地 SQLite 的搬运工。"""


@app.command()
def login() -> None:
    """登录 Mozilla 账号：在浏览器里授权，密码不经过本工具。"""
    try:
        credentials = login_sync(identity_path=identity_path(), credentials_path=credentials_path())
    except FfinfoError as exc:
        _fail(exc)

    bundle = credentials.sync_key_bundle()
    typer.echo()
    typer.echo(
        "登录成功 —— 同步密钥已就绪"
        f"（{len(bundle.encryption_key)} 字节加密密钥 + {len(bundle.hmac_key)} 字节签名密钥）。"
    )
    typer.echo(f"凭据已加密存到 {credentials_path()}")


@app.command()
def sync(
    collection: str = typer.Option(
        "history",
        "--collection",
        "-c",
        help="要拉取的 collection：history / bookmarks / tabs（白名单，其它一律拒绝）",
    ),
    page_size: int = typer.Option(100, "--page-size", help="每页拉多少条（服务器上限 100）"),
    full: bool = typer.Option(
        False,
        "--full",
        help="强制全量重拉（对账用：服务器会清掉很老的墓碑，只有全量才发现那部分删除）",
    ),
    progress: bool = typer.Option(
        True, "--progress/--no-progress", help="要不要在 stderr 上显示拉到第几页了"
    ),
) -> None:
    """从 Firefox Sync 拉取数据并落盘。默认只拉上次同步之后的变更。

    进度走 **stderr**，stdout 上仍然只有那份 JSON。
    """
    if collection not in SYNCABLE_COLLECTIONS:
        allowed = "、".join(sorted(SYNCABLE_COLLECTIONS))
        _fail_usage(f"不拉 collection「{collection}」—— 本项目只拉这几个：{allowed}")
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        _fail_usage(f"--page-size 要在 1..{_MAX_PAGE_SIZE} 之间（服务器上限），收到 {page_size}")

    reporter = reporter_for(sys.stderr, enabled=progress)
    try:
        report = sync_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            collection=collection,
            page_size=page_size,
            full=full,
            on_progress=reporter,
        )
    except BackoffError as exc:
        # 退避不是错误，是"现在别来" —— 顺带告诉 agent 库里没动过，重试是安全的
        _fail(exc, note="。库里没动任何东西。")
    except FfinfoError as exc:
        _fail(exc)
    finally:
        if reporter is not None:
            reporter.finish()

    typer.echo(report.to_json())


@app.command(name="list")
def list_command(
    data_type: str = typer.Option(
        "history",
        "--data-type",
        "-t",
        help="看哪一种：history（一次访问一行）/ bookmarks（树）/ tabs（按设备分组）",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="只看这个时间之后的（YYYY-MM-DD 或 ISO 8601；没写时区就按本机时区算）",
    ),
    domain: str | None = typer.Option(None, "--domain", "-d", help="只看这个域名（子域名也算）"),
    search: str | None = typer.Option(
        None, "--search", "-s", help="在 URL 和标题里搜（不区分大小写）"
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", help="最多返回多少条，最新的优先"),
) -> None:
    """把库里的浏览数据解密后输出 JSON。纯本地，不联网。"""
    if data_type not in DATA_TYPES:
        allowed = "、".join(DATA_TYPES)
        _fail_usage(f"不认识的 --data-type「{data_type}」—— 只能是：{allowed}")
    if limit is not None and limit < 0:
        _fail_usage(f"--limit 不能是负数，收到 {limit}")
    try:
        parsed_since = parse_since(since) if since is not None else None
    except ConfigurationError as exc:
        _fail_usage(str(exc))

    try:
        report = list_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            data_type=data_type,
            since=parsed_since,
            domain=domain,
            search=search,
            limit=limit,
        )
    except FfinfoError as exc:
        _fail(exc)

    typer.echo(report.to_json())


@app.command()
def export(
    destination: Path = _DESTINATION,
    profile: Path | None = _PROFILE,
) -> None:
    """在**有 Firefox 的机器**上跑：把本地历史导出成一份便携文件。

    会连 ``places.sqlite-wal`` 一起带走 —— 只拷主文件会静默丢掉最近的记录。
    """
    try:
        report = export_blocking(
            database_path=database_path(),
            destination=destination,
            home=Path.home(),
            platform=sys.platform,
            env=os.environ,
            machine=socket.gethostname(),
            profile_path=profile,
        )
    except FfinfoError as exc:
        _fail(exc)

    typer.echo(report.to_json())


@app.command(name="import")
def import_command(
    source: Path = _SOURCE,
) -> None:
    """在**目标机器**上跑：把便携文件并进本地库，查询时与云端数据合并。"""
    try:
        report = import_blocking(database_path=database_path(), source=source)
    except FfinfoError as exc:
        _fail(exc)

    typer.echo(report.to_json())
    for warning in report.warnings:
        typer.echo(f"警告：{warning}", err=True)


@app.command()
def profiles() -> None:
    """查看本地状态：探测到的 Firefox profile、目录、密钥、上次同步时间。"""
    try:
        report = profiles_blocking(home=Path.home(), platform=sys.platform, env=os.environ)
    except FfinfoError as exc:
        _fail(exc)

    typer.echo(report.to_json())


if __name__ == "__main__":
    app()
