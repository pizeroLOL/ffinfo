"""ffinfo_cli 命令行入口。

真正的用户是 agent —— 输出以 JSON 为主，人类可读的呈现是次要目标。
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Final

import typer

from ffinfo.errors import BackoffError, FfinfoError
from ffinfo_cli import __version__
from ffinfo_cli.list import list_blocking, parse_since
from ffinfo_cli.login import login_sync
from ffinfo_cli.paths import credentials_path, database_path, identity_path
from ffinfo_cli.sync import sync_blocking
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
        typer.echo(f"登录失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc

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
) -> None:
    """从 Firefox Sync 拉取数据并落盘。默认只拉上次同步之后的变更。"""
    try:
        report = sync_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            collection=collection,
            page_size=page_size,
            full=full,
        )
    except BackoffError as exc:
        scheme = "X-Weave-Backoff" if exc.soft else "Retry-After"
        typer.echo(
            f"服务器要求退避：{exc.wait_seconds:.0f} 秒后再试（{scheme}）。库里没动任何东西。",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except FfinfoError as exc:
        typer.echo(f"同步失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc

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
    try:
        report = list_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            data_type=data_type,
            since=parse_since(since) if since is not None else None,
            domain=domain,
            search=search,
            limit=limit,
        )
    except FfinfoError as exc:
        typer.echo(f"读取失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc

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
        typer.echo(f"导出失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(report.to_json())


@app.command(name="import")
def import_command(
    source: Path = _SOURCE,
) -> None:
    """在**目标机器**上跑：把便携文件并进本地库，查询时与云端数据合并。"""
    try:
        report = import_blocking(database_path=database_path(), source=source)
    except FfinfoError as exc:
        typer.echo(f"导入失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(report.to_json())
    for warning in report.warnings:
        typer.echo(f"警告：{warning}", err=True)


@app.command()
def profiles() -> None:
    """查看本地状态：配置目录、数据目录、密钥、上次同步时间。"""
    typer.echo("profiles: 尚未实现", err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
