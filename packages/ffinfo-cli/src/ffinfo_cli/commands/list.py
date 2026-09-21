"""``ffinfo-cli list`` —— 把库里的加密记录解密成人能看的浏览数据，输出 JSON。"""

from __future__ import annotations

import typer

from ffinfo.errors import ConfigurationError
from ffinfo_cli.failures import fail_usage, guard, warn
from ffinfo_cli.list import DATA_TYPES, list_blocking, parse_since
from ffinfo_cli.paths import credentials_path, database_path, identity_path


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
    if data_type not in DATA_TYPES:
        allowed = "、".join(DATA_TYPES)
        fail_usage(f"不认识的 --data-type「{data_type}」—— 只能是：{allowed}")
    if limit is not None and limit < 0:
        fail_usage(f"--limit 不能是负数，收到 {limit}")
    try:
        parsed_since = parse_since(since) if since is not None else None
    except ConfigurationError as exc:
        fail_usage(str(exc))

    report = guard(
        lambda: list_blocking(
            identity_path=identity_path(),
            credentials_path=credentials_path(),
            database_path=database_path(),
            data_type=data_type,
            since=parsed_since,
            domain=domain,
            search=search,
            limit=limit,
            warn=warn,
        )
    )

    typer.echo(report.to_json())


def register(app: typer.Typer) -> None:
    """把本命令挂到 root app 上。"""
    app.command(name="list")(list_command)
