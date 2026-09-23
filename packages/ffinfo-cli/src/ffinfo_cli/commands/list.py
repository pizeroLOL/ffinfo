"""``ffinfo-cli list`` —— 查询浏览数据，三种类型各是一个子命令。

``list history`` / ``list bookmarks`` / ``list tabs`` 替代了旧的 ``list --data-type X``：
一个字符串穿过校验、dispatch、字段抹除的那条路已经拆掉。筛选项按类型**各给一套** ——
history 是 ``--since`` / ``--domain`` / ``--search``，bookmarks 是 ``--path``，
tabs 是 ``--device``；``--limit`` 三类都有。输出默认人读，``-j/--json`` 切机器 JSON。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import typer

from ffinfo.bookmarks import BookmarkNode, parse_bookmarks
from ffinfo.crypto import KeyBundle
from ffinfo.errors import ConfigurationError
from ffinfo.tabs import parse_tabs
from ffinfo_cli.failures import fail_usage, guard, machine_mode, warn
from ffinfo_cli.list import (
    completion_values,
    parse_since,
    run_bookmarks,
    run_history,
    run_tabs,
)
from ffinfo_cli.paths import credentials_path, database_path, identity_path
from ffinfo_cli.render import render
from ffinfo_cli.runner import run

# 缺子命令走 UsageError（"Missing command."）进失败契约 —— 不能用 no_args_is_help：
# NoArgsIsHelpError 构造时 ctx.get_help() 会把 help 打到 stdout（rich 副作用），
# 而失败契约要求 stdout 为空。
list_app = typer.Typer(
    name="list",
    help="查询库里的浏览数据。纯本地，不联网；默认人读，-j 输出 JSON。",
)


def _device_candidates(records: Sequence[tuple[str, str | None]], key: KeyBundle) -> list[str]:
    """``tabs`` 记录里的设备名 + ``clientId`` —— 补全候选。"""
    report = parse_tabs(records, key)
    return [client.client_name for client in report.clients] + [
        client.client_id for client in report.clients
    ]


def complete_device(incomplete: str) -> list[str]:
    """``list tabs --device`` 的数据感知补全。静默失败在 :func:`completion_values` 里。"""
    return completion_values(
        database_path=database_path(),
        identity_path=identity_path(),
        credentials_path=credentials_path(),
        collection="tabs",
        extract=_device_candidates,
        incomplete=incomplete,
    )


def _folder_paths(records: Sequence[tuple[str, str | None]], key: KeyBundle) -> list[str]:
    """``bookmarks`` 树里所有文件夹的 ``/`` 路径 —— 带祖先，从 root 起算。"""
    report = parse_bookmarks(records, key)
    paths: list[str] = []
    stack: list[tuple[BookmarkNode, str]] = [(node, node.title) for node in reversed(report.roots)]
    while stack:
        node, path = stack.pop()
        if node.type == "folder":
            paths.append(path)
            stack.extend((child, f"{path}/{child.title}") for child in reversed(node.children))
    return paths


def complete_bookmark_path(incomplete: str) -> list[str]:
    """``list bookmarks --path`` 的数据感知补全。静默失败在 :func:`completion_values` 里。"""
    return completion_values(
        database_path=database_path(),
        identity_path=identity_path(),
        credentials_path=credentials_path(),
        collection="bookmarks",
        extract=_folder_paths,
        incomplete=incomplete,
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
        lambda: run(
            lambda: run_history(
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
    )

    typer.echo(render(report, machine=machine_mode()))


def bookmarks(
    path: str | None = typer.Option(
        None,
        "--path",
        help="只看这个文件夹里的书签（/ 分隔的文件夹标题，从任意 root 起算，区分大小写）",
        autocompletion=complete_bookmark_path,
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", help="最多返回多少条书签"),
) -> None:
    """书签：保留父子层级的树；``--path`` 命中后从该文件夹重新生根。"""
    _check_limit(limit)
    report = guard(
        lambda: run(
            lambda: run_bookmarks(
                identity_path=identity_path(),
                credentials_path=credentials_path(),
                database_path=database_path(),
                path=path,
                limit=limit,
                warn=warn,
            )
        )
    )

    typer.echo(render(report, machine=machine_mode()))


def tabs(
    device: str | None = typer.Option(
        None,
        "--device",
        help="只看这台设备（设备名不区分大小写，或 clientId；不做子串）",
        autocompletion=complete_device,
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", help="最多返回多少个标签页"),
) -> None:
    """标签页：按设备（一台设备一条记录）分组；``--device`` 只留命中的那台。"""
    _check_limit(limit)
    report = guard(
        lambda: run(
            lambda: run_tabs(
                identity_path=identity_path(),
                credentials_path=credentials_path(),
                database_path=database_path(),
                device=device,
                limit=limit,
                warn=warn,
            )
        )
    )

    typer.echo(render(report, machine=machine_mode()))


def register(app: typer.Typer) -> None:
    """把 ``list`` 子命令组挂到 root app 上。"""
    list_app.command()(history)
    list_app.command()(bookmarks)
    list_app.command()(tabs)
    app.add_typer(list_app, name="list")
