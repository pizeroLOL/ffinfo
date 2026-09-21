"""``ffinfo-cli list`` —— 查询浏览数据，三种类型各是一个子命令。

``list history`` / ``list bookmarks`` / ``list tabs`` 替代了旧的 ``list --data-type X``：
一个字符串穿过校验、dispatch、字段抹除的那条路已经拆掉。筛选项按类型**各给一套** ——
history 是 ``--since`` / ``--domain`` / ``--search``，bookmarks 是 ``--path``，
tabs 是 ``--device``；``--limit`` 三类都有。
"""

from __future__ import annotations

from datetime import datetime

import typer

from ffinfo.errors import ConfigurationError
from ffinfo_cli.failures import fail_usage, guard, warn
from ffinfo_cli.list import (
    list_bookmarks_blocking,
    list_history_blocking,
    list_tabs_blocking,
    parse_since,
)
from ffinfo_cli.paths import credentials_path, database_path, identity_path

list_app = typer.Typer(
    name="list",
    help="把库里的浏览数据解密后输出 JSON。纯本地，不联网。",
    no_args_is_help=True,
)


def _check_limit(limit: int | None) -> None:
    if limit is not None and limit < 0:
        fail_usage(f"--limit 不能是负数，收到 {limit}")


def _parsed_since(since: str | None) -> datetime | None:
    try:
        return parse_since(since) if since is not None else None
    except ConfigurationError as exc:
        fail_usage(str(exc))


def history(
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
    """浏览历史：一次访问一行（云端与 firefox 双源合并），最新的在前。"""
    _check_limit(limit)
    parsed_since = _parsed_since(since)
    report = guard(
        lambda: list_history_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            since=parsed_since,
            domain=domain,
            search=search,
            limit=limit,
            warn=warn,
        )
    )

    typer.echo(report.to_json())


def bookmarks(
    path: str | None = typer.Option(
        None,
        "--path",
        help="只看这个文件夹里的书签（/ 分隔的文件夹标题，从任意 root 起算，区分大小写）",
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", help="最多返回多少条书签"),
) -> None:
    """书签：保留父子层级的树；``--path`` 命中后从该文件夹重新生根。"""
    _check_limit(limit)
    report = guard(
        lambda: list_bookmarks_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            path=path,
            limit=limit,
            warn=warn,
        )
    )

    typer.echo(report.to_json())


def tabs(
    device: str | None = typer.Option(
        None, "--device", help="只看这台设备（设备名不区分大小写，或 clientId；不做子串）"
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", help="最多返回多少个标签页"),
) -> None:
    """标签页：按设备（一台设备一条记录）分组；``--device`` 只留命中的那台。"""
    _check_limit(limit)
    report = guard(
        lambda: list_tabs_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            device=device,
            limit=limit,
            warn=warn,
        )
    )

    typer.echo(report.to_json())


def register(app: typer.Typer) -> None:
    """把 ``list`` 子命令组挂到 root app 上。"""
    list_app.command()(history)
    list_app.command()(bookmarks)
    list_app.command()(tabs)
    app.add_typer(list_app, name="list")
