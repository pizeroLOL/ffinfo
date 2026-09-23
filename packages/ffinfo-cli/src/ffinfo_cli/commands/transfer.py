"""``ffinfo-cli export`` / ``ffinfo-cli import`` —— 把 firefox 历史在机器之间搬。"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Final

import typer

from ffinfo_cli.failures import fail_usage, guard, machine_mode, warn
from ffinfo_cli.paths import database_path
from ffinfo_cli.places import HostContext, discover_profiles
from ffinfo_cli.render import render
from ffinfo_cli.transfer import (
    FirefoxImport,
    ImportInput,
    PortableImport,
    export_blocking,
    import_blocking,
)

_DESTINATION: Final = typer.Argument(..., help="便携文件写到哪（.sqlite）")
_SOURCE: Final = typer.Argument(None, help="export 产出的那份便携文件（与 --from-firefox 二选一）")


def _host() -> HostContext:
    """本机 Firefox 探测用的环境 —— 默认路径只在 CLI 层决定；测试可替换。"""
    return HostContext(home=Path.home(), platform=sys.platform, env=os.environ)


def complete_profile(incomplete: str) -> list[str]:
    """``--profile`` 的数据感知补全：探测到的 profile 目录。

    **静默** —— 探测不到（或探测本身炸了）就给空候选，绝不写 stderr。
    """
    try:
        candidates = [str(profile.path) for profile in discover_profiles(host=_host())]
        return [path for path in dict.fromkeys(candidates) if path.startswith(incomplete)]
    except Exception:
        return []


_PROFILE: Final = typer.Option(
    None,
    "--profile",
    help="手动指定 Firefox profile 目录（自动找不到时用）",
    autocompletion=complete_profile,
)


def export(
    destination: Path = _DESTINATION,
    profile: Path | None = _PROFILE,
) -> None:
    """在**有 Firefox 的机器**上跑：把 firefox 历史导出成一份便携文件。

    会连 ``places.sqlite-wal`` 一起带走 —— 只拷主文件会静默丢掉最近的记录。
    """
    report = guard(
        lambda: export_blocking(
            database_path=database_path(),
            destination=destination,
            host=_host(),
            machine=socket.gethostname(),
            profile_path=profile,
        )
    )

    typer.echo(render(report, machine=machine_mode()))


def import_command(
    source: Path | None = _SOURCE,
    from_firefox: bool = typer.Option(
        False,
        "--from-firefox",
        help="改读本机 firefox 的 places.sqlite（与 <便携文件> 二选一）",
    ),
    profile: Path | None = _PROFILE,
) -> None:
    """在**目标机器**上跑：把便携文件**或**本机 firefox 并进本地库。

    两种输入二选一；查询时与云端数据合并。``--from-firefox`` 那条不碰云端记录与游标。
    """
    import_input: ImportInput
    if from_firefox:
        if source is not None:
            fail_usage("两种输入只能给一种：<便携文件> 或 --from-firefox")
        import_input = FirefoxImport(
            host=_host(),
            machine=socket.gethostname(),
            profile_path=profile,
        )
    else:
        if source is None:
            fail_usage("得给一种输入：<便携文件> 或 --from-firefox")
        if profile is not None:
            fail_usage("--profile 只能跟 --from-firefox 一起用（便携文件里已经带着 profile 名）")
        import_input = PortableImport(path=source)

    report = guard(
        lambda: import_blocking(database_path=database_path(), input=import_input, warn=warn)
    )

    typer.echo(render(report, machine=machine_mode()))
    for warning in report.warnings:
        warn(warning)


def register(app: typer.Typer) -> None:
    """把 export / import 挂到 root app 上。"""
    app.command()(export)
    app.command(name="import")(import_command)
