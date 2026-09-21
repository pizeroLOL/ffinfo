"""``ffinfo-cli sync`` —— 从 Firefox Sync 拉数据并落盘。"""

from __future__ import annotations

import sys
from typing import Final

import typer

from ffinfo_cli.failures import fail_usage, guard, warn
from ffinfo_cli.paths import credentials_path, database_path, identity_path
from ffinfo_cli.progress import reporter_for
from ffinfo_cli.render import render
from ffinfo_cli.sync import SYNCABLE_COLLECTIONS, sync_blocking

_MAX_PAGE_SIZE: Final = 100
"""服务器每页的上限 —— ``--page-size`` 越界会被**拒**，不是静默夹取。"""


def sync(
    ctx: typer.Context,
    page_size: int = typer.Option(100, "--page-size", help="每页拉多少条（服务器上限 100）"),
    full: bool = typer.Option(
        False,
        "--full",
        help="强制全量重拉（对账用：服务器会清掉很老的墓碑，只有全量才发现那部分删除）",
    ),
    progress: bool | None = typer.Option(
        None,
        "--progress/--no-progress",
        help="要不要在 stderr 上显示拉到第几页了（-j 时默认关，显式给仍生效）",
    ),
) -> None:
    """从 Firefox Sync 拉取白名单三件套（history + bookmarks + tabs）并落盘。

    目标固定，没有 ``--collection``；协议数据 ``crypto`` 顺带拉、归报告的顶层 ``protocol``。
    三件套全拉下来才用一次事务写入 —— 任一失败则全回滚、游标全不动。

    进度走 **stderr**，stdout 上仍然只有结果；``-j`` 时默认关，免得混进 agent 的 stderr。
    """
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        fail_usage(f"--page-size 要在 1..{_MAX_PAGE_SIZE} 之间（服务器上限），收到 {page_size}")

    machine = bool(ctx.obj)
    reporter = reporter_for(sys.stderr, enabled=progress if progress is not None else not machine)
    try:
        report = guard(
            lambda: sync_blocking(
                identity_path=identity_path(),
                credentials_path=credentials_path(),
                database_path=database_path(),
                collections=SYNCABLE_COLLECTIONS,
                page_size=page_size,
                full=full,
                on_progress=reporter,
                warn=warn,
            ),
            backoff_note="。库里没动任何东西。",
        )
    finally:
        if reporter is not None:
            reporter.finish()

    typer.echo(render(report, machine=machine))


def register(app: typer.Typer) -> None:
    """把本命令挂到 root app 上。"""
    app.command()(sync)
