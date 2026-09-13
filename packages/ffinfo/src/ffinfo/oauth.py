"""Mozilla 账号的 OAuth 2.0 授权（oob 模式）。

**密码永不经过本库**：授权全程在 accounts.firefox.com 的网页上完成，
本库只经手授权码 —— 代码层面没有任何地方能接触到密码。

为什么是 oob（让用户从地址栏复制回调 URL）而不是本地回调：
请求 scoped keys 时 ``redirect_uri`` 必须命中 Mozilla 的显式白名单，
第三方工具进不去（``docs/design.md`` §3.4）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import ClassVar, Final, Protocol, Self
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from ffinfo._encoding import b64url_encode
from ffinfo.crypto import KeyBundle
from ffinfo.errors import AuthError
from ffinfo.jwe import EphemeralKeyPair
from ffinfo.keys import OLD_SYNC_SCOPE, ScopedKey, parse_scoped_keys

__all__ = [
    "FIREFOX_DESKTOP_CLIENT_ID",
    "FIREFOX_IOS_CLIENT_ID",
    "OLD_SYNC_SCOPE",
    "AuthorizationRequest",
    "CodeReceiver",
    "Credentials",
    "OAuthClient",
    "OAuthEndpoints",
    "OAuthTokens",
    "PkcePair",
    "firefox_redirect_uri",
    "parse_callback_url",
]

FIREFOX_IOS_CLIENT_ID: Final = "1b1a3e44c54fbb58"
"""Firefox iOS 的 ``client_id`` —— **实测能用的那个**（2026-09-14）。

它在 ``oldsync`` scope 的 redirect_uri 白名单里注册了普通 HTTPS 地址
（``https://accounts.firefox.com/oauth/success/1b1a3e44c54fbb58``），
授权完成后授权码会出现在地址栏里，第三方工具接得住。

⚠️ 仍然是**借来的**身份，Mozilla 随时可能改（``docs/design.md`` 风险 1）。
:class:`OAuthClient` 要求显式传入，方便换成自己的。
"""

FIREFOX_DESKTOP_CLIENT_ID: Final = "5882386c6d801776"
"""Firefox Desktop 的 ``client_id`` —— **实测用不了**，留着做对照。

2026-09-14 实测：它注册的 redirect_uri 只有
``urn:ietf:wg:oauth:2.0:oob:oauth-redirect-webchannel``（webchannel 通道）。
第三方浏览器没有 webchannel，请求会被导向 ``/pair`` 配对流程，
**授权码不会出现在地址栏里**。
"""

_AUTHORIZATION_ENDPOINT: Final = "https://accounts.firefox.com/authorization"
_TOKEN_ENDPOINT: Final = "https://oauth.accounts.firefox.com/v1/token"

_STATE_BYTES: Final = 32
_PKCE_VERIFIER_BYTES: Final = 64
_RFC7636_MIN_VERIFIER: Final = 43
_RFC7636_MAX_VERIFIER: Final = 128

_ERROR_HINTS: Final[dict[str, str]] = {
    "invalid_grant": "授权码过期或被用过；刷新时出现说明 refresh token 也失效了 —— 重新授权一次",
    "invalid_client": "client_id 不被接受",
    "incorrect_redirect_uri": "redirect_uri 没命中 Mozilla 的白名单",
}

_ERRNO_HINTS: Final[dict[int, str]] = {
    108: "服务器不认这个 token —— 重新授权一次",
    109: "请求参数不合法（刷新时多半是 refresh token 的问题）—— 重新授权一次",
}
"""FxA 不用 RFC 那套 ``invalid_grant``，它有自己的 errno（实测：坏的 refresh token 回的是
``error: "Bad Request"`` + ``errno: 108/109``）。有出处的一对写在这里，其余原样透传。"""


def firefox_redirect_uri(client_id: str) -> str:
    """借来的 ``client_id`` 对应的回调地址 —— 就是命中白名单的那一个。

    只对注册了 HTTPS 回调的 client_id 有效（Desktop 那个只有 ``urn:`` 形式的，不适用）。
    """
    return f"https://accounts.firefox.com/oauth/success/{client_id}"


def default_endpoints() -> OAuthEndpoints:
    """Mozilla 的正式端点。"""
    return OAuthEndpoints(authorization=_AUTHORIZATION_ENDPOINT, token=_TOKEN_ENDPOINT)


@dataclass(frozen=True, slots=True)
class OAuthEndpoints:
    """两个端点。做成参数是为了能对着别的服务器跑（测试、将来的自建）。"""

    authorization: str
    token: str


@dataclass(frozen=True, slots=True)
class PkcePair:
    """RFC 7636 的 PKCE 对：verifier 自己留着，challenge 发出去。

    **只用 S256** —— Mozilla 不支持 ``plain``。
    """

    verifier: str
    challenge: str

    @classmethod
    def from_verifier(cls, verifier: str) -> Self:
        """由既有 verifier 算出 challenge（``BASE64URL(SHA256(ASCII(verifier)))``）。"""
        if not _RFC7636_MIN_VERIFIER <= len(verifier) <= _RFC7636_MAX_VERIFIER:
            msg = (
                f"PKCE verifier 的长度必须在 {_RFC7636_MIN_VERIFIER}–{_RFC7636_MAX_VERIFIER} "
                f"之间，收到 {len(verifier)}"
            )
            raise AuthError(msg)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return cls(verifier=verifier, challenge=b64url_encode(digest))

    @classmethod
    def generate(cls) -> Self:
        """现场生成一对。"""
        return cls.from_verifier(secrets.token_urlsafe(_PKCE_VERIFIER_BYTES))


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """一次授权请求的全部状态：URL 交给用户，其余留着换 token。"""

    url: str
    state: str
    pkce: PkcePair
    key_pair: EphemeralKeyPair


class OAuthTokens(BaseModel):
    """token 端点的响应。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    access_token: str
    token_type: str = "bearer"
    scope: str = ""
    expires_in: int | None = None
    """token 还能用多少秒。**缺字段是 ``None``，不是 0** —— 混成同一个值的话，
    "服务器没给"和"一出生就过期"就分不出来了。"""
    refresh_token: str | None = None
    keys_jwe: str | None = None

    def scoped_keys(self, key_pair: EphemeralKeyPair) -> dict[str, ScopedKey]:
        """解开 ``keys_jwe``，拿到各 scope 的密钥。"""
        if not self.keys_jwe:
            msg = "这次授权没有返回 keys_jwe —— 拿不到同步密钥（scope 没申请对？）"
            raise AuthError(msg)
        return parse_scoped_keys(key_pair.decrypt_jwe(self.keys_jwe))


class CodeReceiver(Protocol):
    """授权码怎么到手 —— 现在是"用户复制地址栏 URL"，将来可以换 localhost 回调。

    OAuth 流程只依赖这个协议，换实现不用改上层。
    """

    def receive(self, authorization_url: str) -> str:
        """把授权 URL 交给用户，拿回他粘回来的回调 URL。"""
        ...


class OAuthClient:
    """Mozilla 账号的 OAuth 客户端。**没有密码可传，也没有密码可存。**"""

    __slots__: tuple[str, ...] = ("_client_id", "_endpoints", "_http", "_redirect_uri")

    def __init__(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        http: httpx.AsyncClient,
        endpoints: OAuthEndpoints,
    ) -> None:
        """全部由调用者注入 —— 库不认识任何默认端点，也不碰磁盘。"""
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._http = http
        self._endpoints = endpoints

    def __repr__(self) -> str:
        """只打印 client_id，不打印任何凭据。"""
        return f"OAuthClient(client_id={self._client_id!r})"

    def start_authorization(self, *, scopes: Sequence[str]) -> AuthorizationRequest:
        """生成授权 URL 与本次请求的状态（PKCE + ``keys_jwk``）。"""
        pkce = PkcePair.generate()
        state = secrets.token_urlsafe(_STATE_BYTES)
        key_pair = EphemeralKeyPair.generate()

        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
                # RFC 6749 的授权码流程要求带上它 —— 不指望服务端的默认值
                "response_type": "code",
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                # Mozilla 要的是 **base64url 编码后的** JWK JSON，不是原始 JSON
                # （见 fxa-client 的 oauth.rs：`URL_SAFE_NO_PAD.encode(jwk_json)`）
                "keys_jwk": b64url_encode(
                    json.dumps(key_pair.public_jwk(), separators=(",", ":")).encode("utf-8")
                ),
            }
        )
        return AuthorizationRequest(
            url=f"{self._endpoints.authorization}?{query}",
            state=state,
            pkce=pkce,
            key_pair=key_pair,
        )

    async def exchange_code(self, *, code: str, request: AuthorizationRequest) -> OAuthTokens:
        """拿授权码换 token（含 ``keys_jwe``）。"""
        return await self._post_token(
            {
                "client_id": self._client_id,
                "code": code,
                "code_verifier": request.pkce.verifier,
                "grant_type": "authorization_code",
                "redirect_uri": self._redirect_uri,
            }
        )

    async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
        """用 refresh token 换一份新的 access token（RFC 6749 §6）。

        请求里**不带** ``keys_jwk`` —— 服务器没有公钥可加密，所以响应不会有 ``keys_jwe``，
        scoped keys 不变（它本来也不过期，见 :meth:`Credentials.is_expired`）。
        响应**可能**带新的 ``refresh_token``（轮换）—— 调用方要把它存回去。
        """
        return await self._post_token(
            {
                "client_id": self._client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    async def _post_token(self, data: dict[str, str]) -> OAuthTokens:
        """POST token 端点并解析 —— 换码与刷新共用的那一段。"""
        try:
            response = await self._http.post(self._endpoints.token, data=data)
        except httpx.HTTPError as exc:
            msg = f"连不上 Mozilla 的 token 端点：{exc}"
            raise AuthError(msg) from exc

        if response.status_code != HTTPStatus.OK:
            raise AuthError(_describe_error(response))

        try:
            return OAuthTokens.model_validate_json(response.content)
        except ValidationError as exc:
            msg = "token 端点的响应不是我们认识的样子"
            raise AuthError(msg) from exc


def parse_callback_url(url: str, *, expected_state: str) -> str:
    """从用户粘回来的 URL 里取出授权码，并校验 ``state``。

    校验顺序是有讲究的：先认 ``state``（确认这条 URL 确实是本次授权的），
    再看 ``error``（用户点了拒绝），最后才取 ``code``。
    """
    query = parse_qs(urlparse(url.strip()).query)

    state = _first(query, "state")
    if state != expected_state:
        msg = (
            "回调 URL 里的 state 和本次授权请求对不上 —— "
            "可能是复制错了 URL，或这次授权不是本工具发起的"
        )
        raise AuthError(msg)

    error = _first(query, "error")
    if error:
        msg = f"Mozilla 拒绝了这次授权：{error}"
        raise AuthError(msg)

    code = _first(query, "code")
    if not code:
        msg = "回调 URL 里没有 code 参数 —— 请把地址栏里完整的那一条复制过来"
        raise AuthError(msg)
    return code


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _describe_error(response: httpx.Response) -> str:
    """把 Mozilla 的错误响应翻译成一句能照着做的话。"""
    error = "unknown_error"
    message = ""
    errno: int | None = None
    try:
        payload = response.json()
        error = str(payload.get("error", error))
        message = str(payload.get("message", ""))
        raw_errno = payload.get("errno")
        errno = (
            raw_errno if isinstance(raw_errno, int) and not isinstance(raw_errno, bool) else None
        )
    except json.JSONDecodeError, AttributeError, ValueError:
        message = response.text[:200]

    hint = _ERROR_HINTS.get(error, "")
    if not hint and errno is not None:
        hint = _ERRNO_HINTS.get(errno, "")
    if not hint and response.status_code == HTTPStatus.UNAUTHORIZED:
        # 上游指南（relying-parties/reference/using-apis.md）：刷新也 401 = 用户已经
        # 把这个应用的授权断开了 —— 该重新授权，而不是继续重试
        hint = "refresh token 也失效了（用户可能已经断开授权）—— 重新授权一次"

    parts = [f"Mozilla 拒绝了换 token 的请求：{error}"]
    if errno is not None:
        parts.append(f"(errno {errno})")
    if hint:
        parts.append(f"（{hint}）")
    if message:
        parts.append(message)
    return " ".join(parts)


def _expires_at(tokens: OAuthTokens, *, now: float) -> float:
    """这份 token 什么时候过期。缺 ``expires_in`` 就别猜 —— 报出来。"""
    if tokens.expires_in is None:
        msg = (
            "token 响应里没有 expires_in —— 没法知道这份凭据能用多久"
            "（不敢存一个「一出生就过期」的凭据）。重新授权一次；如果还这样，那就是服务器变了。"
        )
        raise AuthError(msg)
    return now + tokens.expires_in


class Credentials(BaseModel):
    """一份可以落盘的凭据：token + 各 scope 的密钥。

    本类只管"长什么样"和"怎么序列化"；落盘走 02 的
    :class:`~ffinfo.credentials.CredentialStore`（age 加密 + 权限纪律）。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    access_token: str
    refresh_token: str | None = None
    scope: str = ""
    expires_at: float = 0.0
    scoped_keys: dict[str, ScopedKey] = {}

    @classmethod
    def from_tokens(cls, tokens: OAuthTokens, key_pair: EphemeralKeyPair, *, now: float) -> Self:
        """把 token 响应整理成可以长期保存的形态。

        ``now`` 由调用者给 —— 库不自己去读时钟。
        """
        return cls(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            scope=tokens.scope,
            expires_at=_expires_at(tokens, now=now),
            scoped_keys=tokens.scoped_keys(key_pair),
        )

    def refreshed(self, tokens: OAuthTokens, *, now: float) -> Self:
        """刷新后的凭据：access token 换新，``refresh_token`` 轮换了就跟着换。

        ``keys_jwe`` 这里**故意不看**：刷新请求没带 ``keys_jwk``，服务器没有公钥可加密；
        真带回来了也解不开（登录时的临时私钥早丢了）。而 scoped key 本来不过期 ——
        旧的那份仍然是对的。
        """
        return self.model_copy(
            update={
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token or self.refresh_token,
                "scope": tokens.scope or self.scope,
                "expires_at": _expires_at(tokens, now=now),
            }
        )

    def is_expired(self, *, now: float) -> bool:
        """``access_token`` 过期了没（密钥不过期，token 会）。"""
        return now >= self.expires_at

    def sync_key_bundle(self) -> KeyBundle:
        """oldsync 的同步密钥 —— OAuth 流程的终点。"""
        scoped = self.scoped_keys.get(OLD_SYNC_SCOPE)
        if scoped is None:
            msg = "这份凭据里没有 oldsync scope 的密钥"
            raise AuthError(msg)
        return scoped.to_key_bundle()

    def to_json(self) -> str:
        """序列化成一段文本，交给 :class:`CredentialStore` 去加密。"""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, payload: str) -> Self:
        """从 :class:`CredentialStore` 解出来的文本还原。"""
        try:
            return cls.model_validate_json(payload)
        except ValidationError as exc:
            msg = "凭据内容不是我们认识的样子"
            raise AuthError(msg) from exc
