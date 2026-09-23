"""``ffinfo_cli login`` —— 走一遍 Mozilla 的 oob 授权，把凭据加密存到本地。

**密码全程不经过本工具**：授权在 accounts.firefox.com 的网页上完成，
我们只经手授权码。
"""

from __future__ import annotations

import asyncio
import time
import webbrowser
from pathlib import Path
from typing import ClassVar, Literal

import httpx
import typer
from pydantic import BaseModel, ConfigDict

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.errors import AuthError
from ffinfo.oauth import (
    FIREFOX_IOS_CLIENT_ID,
    OLD_SYNC_SCOPE,
    CodeReceiver,
    Credentials,
    OAuthClient,
    default_endpoints,
    firefox_redirect_uri,
    parse_callback_url,
)

_HTTP_TIMEOUT_SECONDS: float = 30.0


class LoginReport(BaseModel):
    """一次 login 的结果 —— **成功才会有**，直接就是 ``-j login`` 的输出。

    失败不走这份报告：失败在 ``failures.guard`` 里就变成 stderr 错误 JSON + 分档退出码。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 1
    status: Literal["success"] = "success"
    """成功语义 —— agent 不必只靠「exit 0 且能 parse」推断；失败是另一种形状（``error``）。"""
    credentials: str
    """加密凭据的落点 —— 后续 ``sync`` 用的就是这份文件。"""
    encryption_key_bytes: int
    hmac_key_bytes: int
    """同步密钥已就绪的证据：两个 256 位密钥的字节数（当前都是 32）。"""


class ConsoleCodeReceiver:
    """把授权 URL 交给用户，等他把地址栏里那一条粘回来。"""

    __slots__: tuple[str, ...] = ("_open_browser",)

    def __init__(self, *, open_browser: bool = True) -> None:
        """``open_browser=False`` 时只打印 URL，不尝试拉起浏览器。"""
        self._open_browser = open_browser

    def receive(self, authorization_url: str) -> str:
        """打印 URL（尽量顺手打开浏览器），读回用户粘的那一条。"""
        typer.echo("在浏览器里打开下面这个地址，完成授权：\n")
        typer.echo(f"  {authorization_url}\n")
        if self._open_browser and webbrowser.open(authorization_url):
            typer.echo("（浏览器已经帮你打开了）\n")
        typer.echo("授权完成后，把地址栏里**完整的那一条 URL** 复制下来粘到下面。")
        return typer.prompt("回调 URL")


async def run_login(
    *,
    identity_path: Path,
    credentials_path: Path,
    receiver: CodeReceiver,
    http: httpx.AsyncClient,
    now: float | None = None,
) -> Credentials:
    """跑完整条授权链，把凭据加密落盘。

    HTTP 客户端由调用者给（测试才能塞 mock）；``now`` 同理 —— 库不自己读时钟。
    """
    if identity_path.exists():
        identity = AgeIdentity.from_file(identity_path)
    else:
        identity = AgeIdentity.generate()
        identity.to_file(identity_path)

    client = OAuthClient(
        client_id=FIREFOX_IOS_CLIENT_ID,
        redirect_uri=firefox_redirect_uri(FIREFOX_IOS_CLIENT_ID),
        http=http,
        endpoints=default_endpoints(),
    )
    request = client.start_authorization(scopes=[OLD_SYNC_SCOPE])
    callback_url = receiver.receive(request.url)
    code = parse_callback_url(callback_url, expected_state=request.state)
    tokens = await client.exchange_code(code=code, request=request)

    credentials = Credentials.from_tokens(
        tokens,
        request.key_pair,
        now=now if now is not None else time.time(),
    )
    CredentialStore(identity=identity, path=credentials_path).save(credentials.to_json())
    return credentials


def login_sync(*, identity_path: Path, credentials_path: Path) -> Credentials:
    """:func:`run_login` 的同步外壳：自己开 HTTP 客户端、用控制台接收授权码。"""

    async def _main() -> Credentials:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as http:
            return await run_login(
                identity_path=identity_path,
                credentials_path=credentials_path,
                receiver=ConsoleCodeReceiver(),
                http=http,
            )

    return asyncio.run(_main())


async def refresh_credentials(
    credentials: Credentials,
    *,
    identity_path: Path,
    credentials_path: Path,
    http: httpx.AsyncClient,
    now: float,
) -> Credentials:
    """拿 refresh token 续一份新的，落盘，返回 —— 用户不用再点浏览器。

    ``refresh_token`` 也失效了（``invalid_grant``）就抛 :class:`AuthError`，
    消息里写清只能重新 ``login``。
    """
    if credentials.refresh_token is None:
        msg = "这份凭据里没有 refresh token —— 只能重新跑一次 `ffinfo-cli login`"
        raise AuthError(msg)

    client = OAuthClient(
        client_id=FIREFOX_IOS_CLIENT_ID,
        redirect_uri=firefox_redirect_uri(FIREFOX_IOS_CLIENT_ID),
        http=http,
        endpoints=default_endpoints(),
    )
    tokens = await client.refresh_access_token(refresh_token=credentials.refresh_token)
    refreshed = credentials.refreshed(tokens, now=now)
    store = CredentialStore(
        identity=AgeIdentity.from_file(identity_path),
        path=credentials_path,
    )
    store.save(refreshed.to_json())
    return refreshed
