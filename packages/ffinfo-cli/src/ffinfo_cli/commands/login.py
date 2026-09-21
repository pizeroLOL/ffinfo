"""``ffinfo-cli login`` —— 登录 Mozilla 账号。"""

from __future__ import annotations

import typer

from ffinfo.crypto import KeyBundle
from ffinfo_cli.failures import guard
from ffinfo_cli.login import login_sync
from ffinfo_cli.paths import credentials_path, identity_path


def _login() -> KeyBundle:
    """登录并取出同步密钥 —— **两步都在契约里**。

    ``sync_key_bundle()`` 缺 oldsync scope 时抛 ``AuthError``（拿不到密钥同样是认证失败）。
    漏在 ``guard`` 外面就变成 traceback + 退出码 1，把 README 那张表破掉一格。
    """
    credentials = login_sync(identity_path=identity_path(), credentials_path=credentials_path())
    return credentials.sync_key_bundle()


def login() -> None:
    """登录 Mozilla 账号：在浏览器里授权，密码不经过本工具。"""
    bundle = guard(_login)

    typer.echo()
    typer.echo(
        "登录成功 —— 同步密钥已就绪"
        f"（{len(bundle.encryption_key)} 字节加密密钥 + {len(bundle.hmac_key)} 字节签名密钥）。"
    )
    typer.echo(f"凭据已加密存到 {credentials_path()}")


def register(app: typer.Typer) -> None:
    """把本命令挂到 root app 上。"""
    app.command()(login)
