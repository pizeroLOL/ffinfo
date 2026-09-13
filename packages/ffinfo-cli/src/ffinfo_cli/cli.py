"""ffinfo_cli 命令行入口。

真正的用户是 agent —— 输出以 JSON 为主，人类可读的呈现是次要目标。
"""

from __future__ import annotations

import typer

from ffinfo.errors import FfinfoError
from ffinfo_cli import __version__
from ffinfo_cli.login import login_sync
from ffinfo_cli.paths import credentials_path, identity_path

app = typer.Typer(
    name="ffinfo_cli",
    help="把 Firefox 浏览数据（云端 Sync + 本地 places.sqlite）拉到本地 SQLite，输出 JSON。",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ffinfo {__version__}")
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
def sync() -> None:
    """从 Firefox Sync 拉取数据（显式触发，不做自动同步）。"""
    typer.echo("sync: 尚未实现", err=True)
    raise typer.Exit(code=1)


@app.command()
def profiles() -> None:
    """查看本地状态：配置目录、数据目录、密钥、上次同步时间。"""
    typer.echo("profiles: 尚未实现", err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
