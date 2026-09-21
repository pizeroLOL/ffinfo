"""ffinfo_cli 命令行入口 —— **只做装配**。

命令 adapter 在 ``ffinfo_cli/commands/``，失败契约在 ``ffinfo_cli/failures.py``。
这里建 root app、挂 callback、把各命令注册进来。

真正的用户是 agent —— 输出以 JSON 为主，人类可读的呈现是次要目标。
"""

from __future__ import annotations

import typer

from ffinfo_cli import __version__
from ffinfo_cli.commands import list as list_command
from ffinfo_cli.commands import login, profiles, sync, transfer

app = typer.Typer(
    name="ffinfo-cli",
    help="把 Firefox 浏览数据（云端 Sync + firefox places.sqlite）拉到本地 SQLite，输出 JSON。",
    no_args_is_help=True,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
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


login.register(app)
sync.register(app)
list_command.register(app)
transfer.register(app)
profiles.register(app)


if __name__ == "__main__":
    app()
