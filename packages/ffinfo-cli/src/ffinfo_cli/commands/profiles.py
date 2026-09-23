"""``ffinfo-cli profiles`` —— 查看本地状态。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from ffinfo_cli.failures import guard, machine_mode, warn
from ffinfo_cli.places import HostContext
from ffinfo_cli.profiles import profiles_blocking
from ffinfo_cli.render import render


def profiles() -> None:
    """查看本地状态：探测到的 Firefox profile、目录、密钥、上次同步时间。"""
    report = guard(
        lambda: profiles_blocking(
            host=HostContext(home=Path.home(), platform=sys.platform, env=os.environ), warn=warn
        )
    )

    typer.echo(render(report, machine=machine_mode()))


def register(app: typer.Typer) -> None:
    """把本命令挂到 root app 上。"""
    app.command()(profiles)
