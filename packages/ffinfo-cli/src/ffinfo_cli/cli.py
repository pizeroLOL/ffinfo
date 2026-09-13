"""ffinfo_cli 命令行入口。

真正的用户是 agent —— 输出以 JSON 为主，人类可读的呈现是次要目标。
"""

from __future__ import annotations

import typer

from ffinfo.errors import BackoffError, FfinfoError
from ffinfo_cli import __version__
from ffinfo_cli.list import list_blocking, parse_since
from ffinfo_cli.login import login_sync
from ffinfo_cli.paths import credentials_path, database_path, identity_path
from ffinfo_cli.sync import sync_blocking

app = typer.Typer(
    name="ffinfo-cli",
    help="把 Firefox 浏览数据（云端 Sync + 本地 places.sqlite）拉到本地 SQLite，输出 JSON。",
    no_args_is_help=True,
    add_completion=False,
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
) -> None:
    """从 Firefox Sync 拉取数据并落盘。输出 JSON，拉全了才写库。"""
    try:
        report = sync_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            collection=collection,
            page_size=page_size,
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
    """把库里的浏览历史解密后输出 JSON。纯本地，不联网。"""
    try:
        report = list_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
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
def profiles() -> None:
    """查看本地状态：配置目录、数据目录、密钥、上次同步时间。"""
    typer.echo("profiles: 尚未实现", err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
