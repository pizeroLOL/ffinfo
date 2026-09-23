"""``ffinfo-cli login`` —— 登录 Mozilla 账号。"""

from __future__ import annotations

import typer

from ffinfo_cli.failures import guard, machine_mode
from ffinfo_cli.login import LoginReport, login_sync
from ffinfo_cli.paths import credentials_path, identity_path
from ffinfo_cli.render import render


def _login() -> LoginReport:
    """登录并取出同步密钥 —— **两步都在契约里**。

    ``sync_key_bundle()`` 缺 oldsync scope 时抛 ``AuthError``（拿不到密钥同样是认证失败）。
    漏在 ``guard`` 外面就变成 traceback + 退出码 1，把 README 那张表破掉一格。
    """
    credentials = login_sync(identity_path=identity_path(), credentials_path=credentials_path())
    bundle = credentials.sync_key_bundle()
    return LoginReport(
        credentials=str(credentials_path()),
        encryption_key_bytes=len(bundle.encryption_key),
        hmac_key_bytes=len(bundle.hmac_key),
    )


def login() -> None:
    """登录 Mozilla 账号：在浏览器里授权，密码不经过本工具。"""
    report = guard(_login)

    typer.echo(render(report, machine=machine_mode()))


def register(app: typer.Typer) -> None:
    """把本命令挂到 root app 上。"""
    app.command()(login)
