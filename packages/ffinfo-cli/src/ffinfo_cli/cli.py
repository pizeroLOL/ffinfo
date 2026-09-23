"""ffinfo_cli 命令行入口 —— **只做装配**。

命令 adapter 在 ``ffinfo_cli/commands/``，失败契约在 ``ffinfo_cli/failures.py``，
输出渲染在 ``ffinfo_cli/render.py``。这里建 root app、挂 callback（含全局 ``-j/--json``）、
把各命令注册进来。

默认输出给人看；``-j/--json``（写在子命令**之前**）切机器 JSON。
"""

from __future__ import annotations

import typer

from ffinfo_cli import __version__
from ffinfo_cli.commands import list as list_command
from ffinfo_cli.commands import login, profiles, sync, transfer
from ffinfo_cli.failures import CliTyper

app = CliTyper(
    name="ffinfo-cli",
    help="把 Firefox 浏览数据（云端 Sync + firefox places.sqlite）拉到本地 SQLite。",
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
    machine: bool = typer.Option(
        False, "-j", "--json", help="输出机器可读的 JSON（默认人读；放在子命令之前）"
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="显示版本后退出",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ffinfo_cli —— Firefox 数据到本地 SQLite 的搬运工。

    ``machine`` 的值不在这里落第二份 —— 模式在 Click 解析**之前**就由
    ``failures.initialize_mode`` 从 argv 定好（唯一存储）。这里声明选项只为让
    Click 认得 ``-j/--json``、``-h`` 的 help 里列得出来。
    """


login.register(app)
sync.register(app)
list_command.register(app)
transfer.register(app)
profiles.register(app)


if __name__ == "__main__":
    app()
