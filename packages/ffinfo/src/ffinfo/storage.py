"""Sync 存储协议：tokenserver 取凭证 → Hawk 签名 → 分页拉取。

这一层**不解密任何东西** —— 拉下来的就是服务器上的加密原文
（``{"ciphertext","IV","hmac"}`` 字符串，见 ``docs/design.md`` §3.2）。解密是上层的事。

要打**两层** HTTP，别混：

    FxA access token ──► tokenserver ──► (id, key, api_endpoint) ──► Hawk ──► storage

出处：syncstorage-rs 的 ``docs/src/syncstorage/api-1.5.md`` 与
``docs/src/tokenserver/tokenserver-api.md``；参考实现是 app-services 的
``components/sync15/src/client/{token,storage_client}.rs``。

**不写回 Mozilla**：本模块只有 GET，没有任何写操作打到服务器上。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, ClassVar, Final, cast

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from ffinfo.errors import BackoffError, SyncProtocolError

__all__ = [
    "MOZILLA_TOKEN_SERVER",
    "BackoffState",
    "CollectionFetch",
    "EncryptedBso",
    "FetchProgress",
    "HawkCredentials",
    "SyncStorageClient",
    "TokenserverToken",
    "hawk_authorization",
]

MOZILLA_TOKEN_SERVER: Final = "https://token.services.mozilla.com/1.0/sync/1.5"
"""Mozilla 的 tokenserver 端点。**写全整条路径** —— 本库不做 app-services 那种
"自动补 ``/1.0/sync/1.5``" 的魔法，给什么打什么。"""

_DEFAULT_PAGE_SIZE: Final = 100
"""每页最多几条。服务器默认也是 100（``api-1.5.md``），但我们显式传 —— 别赖默认值。"""

_TOKEN_REFRESH_MARGIN: Final = 60.0
"""提前多少秒换 token。token 有效期 3600 秒，留一分钟余量。"""

_RETRY_AFTER_FALLBACK: Final = 10.0
"""服务器没说等多久时的兜底秒数（对齐 app-services 的 ``RETRY_AFTER_DEFAULT_MS``）。"""

_MAX_PAGES: Final = 10_000
"""翻页硬上限。防止服务器给一个永不消失的 ``X-Weave-Next-Offset`` 把我们转死。"""

_NONCE_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_NONCE_LENGTH: Final = 10

_HAWK_PREFIX: Final = "hawk.1.header"
_JSON: Final = "application/json"


@dataclass(frozen=True, slots=True)
class HawkCredentials:
    """tokenserver 发回来的那对 Hawk 凭证：``id`` 明文，``key`` 是 HMAC 密钥。"""

    id: str
    key: bytes


def hawk_authorization(
    *,
    credentials: HawkCredentials,
    method: str,
    url: httpx.URL,
    timestamp: float,
    nonce: str,
) -> str:
    r"""算出一个请求的 ``Authorization`` 头（Hawk 1.0）。

    规范化串（rust-hawk ``mac.rs`` 逐字对齐，**三个换行一个都不能少**）::

        hawk.1.header\n{ts}\n{nonce}\n{METHOD}\n{path?query}\n{host}\n{port}\n\n\n

    最后两个空行分别是 hash 与 ext —— GET 没有请求体，两者都为空。
    """
    normalized = _normalized_request(
        method=method, url=url, timestamp=int(timestamp), nonce=nonce
    ).encode("utf-8")
    mac = base64.b64encode(hmac.new(credentials.key, normalized, hashlib.sha256).digest())
    return (
        f'Hawk id="{credentials.id}", mac="{mac.decode("ascii")}", '
        f'nonce="{nonce}", ts="{int(timestamp)}"'
    )


def _normalized_request(*, method: str, url: httpx.URL, timestamp: int, nonce: str) -> str:
    """Hawk 的规范化请求串。**path 要带 query**，端口要按 scheme 补默认值。"""
    query = url.query.decode("ascii")
    resource = f"{url.path}?{query}" if query else url.path
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    return (
        f"{_HAWK_PREFIX}\n{timestamp}\n{nonce}\n{method.upper()}\n"
        f"{resource}\n{url.host}\n{port}\n\n\n"
    )


def _new_nonce() -> str:
    """10 个随机字母数字 —— 与 rust-hawk 的 ``random_string(10)`` 同规格。"""
    return "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(_NONCE_LENGTH))


def _parse_seconds(raw: str | None) -> float | None:
    """退避头 → 秒数。向上取整；负数与非数字直接丢掉（对齐 app-services 的 ``parse_seconds``）。"""
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(math.ceil(value))


def _endpoint_url(api_endpoint: str, path: str) -> httpx.URL:
    """拼端点下的相对路径。

    刻意**不用** :meth:`httpx.URL.join` —— ``…/1.5/12345`` 的最后一段会被
    当成文件名替换掉，拼出来的是 ``…/1.5/info/…``，少一节用户 id。
    """
    return httpx.URL(f"{api_endpoint.rstrip('/')}/{path}")


class TokenserverToken(BaseModel):
    """tokenserver 的响应。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    id: str
    key: str
    api_endpoint: str
    uid: int = 0
    duration: int = 0
    hashed_fxa_uid: str = ""

    def hawk_credentials(self) -> HawkCredentials:
        """转成 Hawk 凭证。

        ⚠️ 密钥是 ``key`` **字符串本身的字节**，不是 base64 解码后的字节 ——
        app-services 就是 ``Key::new(token.key.as_bytes(), SHA256)``。
        这里解错就是一路 401。
        """
        return HawkCredentials(id=self.id, key=self.key.encode("ascii"))


class EncryptedBso(BaseModel):
    """服务器上的一个 BSO（Basic Storage Object）。

    ``payload`` 是**加密原文** —— 本层原样搬运，一个字节都不碰。
    它是 ``null`` 时表示墓碑（这条记录在别的设备上被删了）。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    id: str
    modified: float
    payload: str | None = None
    sortindex: int | None = None
    ttl: int | None = None

    @property
    def is_tombstone(self) -> bool:
        """墓碑记录 —— 没有 payload。"""
        return self.payload is None


@dataclass(slots=True)
class BackoffState:
    """服务器让等到什么时候。软（``X-Weave-Backoff``）与硬（``Retry-After``）分开记。

    取两者的 **max**：只要有一个没到期就不发请求（对齐 app-services 的
    ``BackoffState::get_required_wait``）。
    """

    soft_until: float = 0.0
    hard_until: float = 0.0

    def note_soft(self, seconds: float, *, now: float) -> None:
        """记下 ``X-Weave-Backoff`` —— 服务器压力大，但还能干活。"""
        self.soft_until = max(self.soft_until, now + seconds)

    def note_hard(self, seconds: float, *, now: float) -> None:
        """记下 ``Retry-After`` —— 服务器维护中或写入冲突，硬性要求等待。"""
        self.hard_until = max(self.hard_until, now + seconds)

    def required_wait(self, *, now: float) -> float:
        """还要等多少秒；``0.0`` 表示可以立刻发请求。"""
        return max(0.0, max(self.soft_until, self.hard_until) - now)

    def reset(self) -> None:
        """忘掉之前的退避（手动重试时用）。"""
        self.soft_until = 0.0
        self.hard_until = 0.0


@dataclass(frozen=True, slots=True)
class CollectionFetch:
    """一次完整拉取的结果。"""

    collection: str
    records: tuple[EncryptedBso, ...]
    last_modified: float
    pages: int
    server_count: int | None = None

    @property
    def count(self) -> int:
        """拉到多少条。"""
        return len(self.records)


@dataclass(frozen=True, slots=True)
class FetchProgress:
    """翻页进度 —— **每翻完一页报一次**。

    只报事实（哪个 collection、第几页、累计多少条），不掺时间也不掺显示 ——
    "用时多久""要不要刷同一行"都是调用方的事（``docs/design.md`` §2.5 的老规矩：
    库不替应用做决定，时钟与输出都由外面注入）。
    """

    collection: str
    pages: int
    records: int


class _CollectionChanged(Exception):
    """读到一半集合被改了（412）。内部信号，外面看到的是 :class:`SyncProtocolError`。"""


class SyncStorageClient:
    """一个 Mozilla 账号的 Sync 存储端点。**只有 GET** —— 没有任何写操作。

    所有外部依赖（HTTP 客户端、端点、时钟）由调用者注入，本类不持有默认路径
    （``docs/design.md`` §2.5）。时钟可注入是为了让退避逻辑能用测试向量驱动，
    不必真的 ``sleep``。
    """

    __slots__: tuple[str, ...] = (
        "_access_token",
        "_backoff",
        "_clock",
        "_http",
        "_key_id",
        "_token",
        "_token_expires_at",
        "_token_server_url",
    )

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        access_token: str,
        key_id: str,
        token_server_url: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``access_token`` 来自 03 的 OAuth 流程，``key_id`` 是 oldsync scoped key 的 ``kid``。

        ``key_id`` 是**必需**的：不带 ``X-KeyID`` 请求 tokenserver 会被直接打回
        （2026-09-14 实测：``401 invalid-key-id / Missing X-KeyID header``）。
        """
        self._http = http
        self._access_token = access_token
        self._key_id = key_id
        self._token_server_url = token_server_url
        self._clock = clock
        self._token: TokenserverToken | None = None
        self._token_expires_at = 0.0
        self._backoff = BackoffState()

    def __repr__(self) -> str:
        """只打印端点，不打印任何凭据。"""
        return f"SyncStorageClient(token_server={self._token_server_url!r})"

    @property
    def backoff(self) -> BackoffState:
        """服务器最近一次要求的退避 —— 调用方一般不用管，``_send`` 会自己拦。"""
        return self._backoff

    async def token(self, *, force: bool = False) -> TokenserverToken:
        """拿 tokenserver 凭证；带缓存，快过期就自动换。"""
        now = self._clock()
        if not force and self._token is not None and now < self._token_expires_at:
            return self._token
        token = await self._fetch_token()
        self._token = token
        self._token_expires_at = max(now, now + token.duration - _TOKEN_REFRESH_MARGIN)
        return token

    async def _fetch_token(self) -> TokenserverToken:
        """打一次 tokenserver。这一层是 Bearer，不是 Hawk。"""
        response = await self._send(
            "GET",
            httpx.URL(self._token_server_url),
            headers={
                "Accept": _JSON,
                "Authorization": f"Bearer {self._access_token}",
                "X-KeyID": self._key_id,
            },
        )
        if response.status_code != HTTPStatus.OK:
            msg = (
                f"tokenserver 拒绝了这次请求（HTTP {response.status_code}）："
                f"{_body_snippet(response)}"
            )
            raise SyncProtocolError(msg)
        try:
            return TokenserverToken.model_validate_json(response.content)
        except ValidationError as exc:
            msg = "tokenserver 的响应不是我们认识的样子"
            raise SyncProtocolError(msg) from exc

    async def collection_counts(self) -> dict[str, int]:
        """各 collection 的条数 —— 验收里"服务器报告的计数"就是它。

        也是 ``/info/collection_counts``，``api-1.5.md`` 里最便宜的那个接口。
        """
        token = await self.token()
        url = _endpoint_url(token.api_endpoint, "info/collection_counts")
        response = await self._authorized_get(url)
        if response.status_code != HTTPStatus.OK:
            msg = (
                f"拿不到 collection 计数（HTTP {response.status_code}）：{_body_snippet(response)}"
            )
            raise SyncProtocolError(msg)
        return _counts_from_json(response)

    async def fetch_collection(
        self,
        collection: str,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        sort: str | None = None,
        retries: int = 3,
        verify_count: bool = True,
        newer: float | None = None,
        on_progress: Callable[[FetchProgress], None] | None = None,
    ) -> CollectionFetch:
        """拉一个 collection 的记录，自动翻页。

        ``newer`` 给了就只拉**严格晚于**这个时间戳的记录（增量同步）；
        不给就是全量。时间戳的来源是上一次的 :attr:`CollectionFetch.last_modified`。

        ``retries`` 是"读到一半集合被改了"（412）时整段重试的次数 —— 连同首次一共
        ``retries + 1`` 次尝试，还是读不到一致快照就报错（默认 3 次重试）。

        ``verify_count=True`` 时拉完会和服务器报告的条数对一下 —— 但**只在全量时**才有意义：
        增量拉回来的只是变更集，条数本来就对不上整个 collection。
        所以 ``newer`` 给了的时候这个开关会被自动关掉。

        ``on_progress`` 给了就每翻完一页调一次（全量拉 50 页时，调用方靠它知道
        自己不是在等一个卡死的进程）。它**只读不写**，返回值一概不管。

        返回的记录**未经解密**。
        """
        incremental = newer is not None
        server_count: int | None = None
        if verify_count and not incremental:
            server_count = (await self.collection_counts()).get(collection, 0)

        for attempt in range(retries + 1):
            try:
                result = await self._fetch_pages(
                    collection,
                    page_size=page_size,
                    sort=sort,
                    server_count=server_count,
                    newer=newer,
                    on_progress=on_progress,
                )
            except _CollectionChanged as exc:
                if attempt < retries:
                    continue
                msg = (
                    f"collection「{collection}」读到一半就被改了（412），"
                    f"重试 {retries} 次都没拿到一致快照 —— "
                    "大概率是别的设备正在同步，稍后再跑一次"
                )
                raise SyncProtocolError(msg) from exc
            if server_count is not None and result.count != server_count:
                msg = (
                    f"collection「{collection}」拉下来 {result.count} 条，"
                    f"服务器报告有 {server_count} 条 —— 对不上，没敢当真。"
                    "可能是别的设备正在写入，重跑一次；若反复出现就是分页漏了"
                )
                raise SyncProtocolError(msg)
            return result

        msg = "拉取流程走到了不该到的地方"  # pragma: no cover - 上面两个分支必有一个 return/raise
        raise SyncProtocolError(msg)

    async def _fetch_pages(
        self,
        collection: str,
        *,
        page_size: int,
        sort: str | None,
        server_count: int | None,
        newer: float | None = None,
        on_progress: Callable[[FetchProgress], None] | None = None,
    ) -> CollectionFetch:
        """真正翻页的那个循环。集合中途被改会抛 :class:`_CollectionChanged` 让上层重试。"""
        token = await self.token()
        records: list[EncryptedBso] = []
        offset: str | None = None
        last_modified: float | None = None
        pages = 0

        while True:
            url = _collection_url(
                token.api_endpoint,
                collection,
                limit=page_size,
                offset=offset,
                sort=sort,
                newer=newer,
            )
            response = await self._authorized_get(url, if_unmodified_since=last_modified)

            if response.status_code == HTTPStatus.PRECONDITION_FAILED:
                raise _CollectionChanged
            if response.status_code != HTTPStatus.OK:
                msg = (
                    f"拉取 collection「{collection}」失败（HTTP {response.status_code}）："
                    f"{_body_snippet(response)}"
                )
                raise SyncProtocolError(msg)

            last_modified = _required_last_modified(response)
            records.extend(_records_from_json(response, collection))
            pages += 1
            if on_progress is not None:
                # 报的是**这一趟**的累计数 —— 412 重试会从头开始，不把上一趟的算进来
                on_progress(FetchProgress(collection=collection, pages=pages, records=len(records)))

            offset = response.headers.get("X-Weave-Next-Offset") or None
            if offset is None:
                break
            if pages >= _MAX_PAGES:
                msg = f"collection「{collection}」翻了 {pages} 页还没到头，停下来看看怎么回事"
                raise SyncProtocolError(msg)

        return CollectionFetch(
            collection=collection,
            records=tuple(records),
            # while True 保证循环至少跑一轮、每轮都会赋值 —— 到这里它一定不是 None
            last_modified=last_modified,
            pages=pages,
            server_count=server_count,
        )

    async def _authorized_get(
        self, url: httpx.URL, *, if_unmodified_since: float | None = None
    ) -> httpx.Response:
        """带 Hawk 签名的 GET。401 时换一个 token 再试一次（token 过期或节点重分配）。"""
        token = await self.token()
        response = await self._hawk_get(url, token, if_unmodified_since=if_unmodified_since)
        if response.status_code != HTTPStatus.UNAUTHORIZED:
            return response

        fresh = await self.token(force=True)
        if fresh.api_endpoint != token.api_endpoint:
            msg = (
                f"Sync 节点重分配了（{token.api_endpoint} → {fresh.api_endpoint}），"
                "本次拉取作废，重新跑一次"
            )
            raise SyncProtocolError(msg)
        return await self._hawk_get(url, fresh, if_unmodified_since=if_unmodified_since)

    async def _hawk_get(
        self, url: httpx.URL, token: TokenserverToken, *, if_unmodified_since: float | None
    ) -> httpx.Response:
        """签一次名、发一次请求。"""
        headers = {
            "Accept": _JSON,
            "Authorization": hawk_authorization(
                credentials=token.hawk_credentials(),
                method="GET",
                url=url,
                timestamp=self._clock(),
                nonce=_new_nonce(),
            ),
        }
        if if_unmodified_since is not None:
            headers["X-If-Unmodified-Since"] = f"{if_unmodified_since}"
        return await self._send("GET", url, headers=headers)

    async def _send(
        self, method: str, url: httpx.URL, *, headers: dict[str, str]
    ) -> httpx.Response:
        """唯一的出口。**退避在这里拦下** —— 服务器说别来就别来，连请求都不发。"""
        wait = self._backoff.required_wait(now=self._clock())
        if wait > 0:
            soft = self._backoff.soft_until >= self._backoff.hard_until
            msg = (
                f"服务器要求退避，还需等待 {wait:.0f} 秒"
                f"（{'X-Weave-Backoff' if soft else 'Retry-After'}）"
            )
            raise BackoffError(msg, wait_seconds=wait, soft=soft)

        try:
            response = await self._http.request(method, url, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"连不上 Sync 服务器：{exc}"
            raise SyncProtocolError(msg) from exc

        self._note_backoff(response)
        if response.status_code in {HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.CONFLICT}:
            self._raise_hard_backoff()
        return response

    def _note_backoff(self, response: httpx.Response) -> None:
        """读退避头。软的可以挂在 200 上，所以每个响应都要看。"""
        now = self._clock()
        soft = _parse_seconds(response.headers.get("X-Weave-Backoff"))
        if soft is not None:
            self._backoff.note_soft(soft, now=now)
        hard = _parse_seconds(response.headers.get("Retry-After"))
        if hard is not None:
            self._backoff.note_hard(hard, now=now)

    def _raise_hard_backoff(self) -> None:
        """503 / 409 —— 服务器明说要等，别硬刚。"""
        now = self._clock()
        wait = self._backoff.hard_until - now
        if wait <= 0:
            wait = _RETRY_AFTER_FALLBACK
            self._backoff.note_hard(wait, now=now)
        msg = f"服务器要求退避，还需等待 {wait:.0f} 秒（Retry-After）"
        raise BackoffError(msg, wait_seconds=wait, soft=False)


def _collection_url(
    api_endpoint: str,
    collection: str,
    *,
    limit: int,
    offset: str | None,
    sort: str | None,
    newer: float | None = None,
) -> httpx.URL:
    """拼一页的 URL。``offset`` 是服务器给的不透明串，原样传回去。"""
    params = {"full": "1", "limit": str(limit)}
    if sort is not None:
        params["sort"] = sort
    if offset is not None:
        params["offset"] = offset
    if newer is not None:
        params["newer"] = format_timestamp(newer)
    return _endpoint_url(api_endpoint, f"storage/{collection}").copy_with(params=params)


def format_timestamp(value: float) -> str:
    """把时间戳格式化成服务器要的样子（两位小数）。

    **向下取整，不四舍五入。** ``newer`` 的语义是"严格大于"，向上取整会**跳过**
    落在中间那零点几秒里的记录 —— 宁可多拉一条（反正 upsert 幂等），也不能漏。
    """
    return f"{math.floor(value * 100) / 100:.2f}"


def _required_last_modified(response: httpx.Response) -> float:
    """取 ``X-Last-Modified``。所有成功响应都该有它（``api-1.5.md``），没有就是不对劲。"""
    raw = response.headers.get("X-Last-Modified")
    if raw is None:
        msg = "响应里没有 X-Last-Modified —— 拿不到集合的修改时间，翻页的一致性没法保证"
        raise SyncProtocolError(msg)
    try:
        return float(raw)
    except ValueError as exc:
        msg = f"X-Last-Modified 不是个数字：{raw!r}"
        raise SyncProtocolError(msg) from exc


def _records_from_json(response: httpx.Response, collection: str) -> list[EncryptedBso]:
    """把一页的响应体解析成 BSO 列表。"""
    try:
        payload: object = response.json()
    except ValueError as exc:
        msg = f"collection「{collection}」的响应不是合法 JSON"
        raise SyncProtocolError(msg) from exc
    if not isinstance(payload, list):
        msg = f"collection「{collection}」的响应不是一个列表，拿到 {type(payload).__name__}"
        raise SyncProtocolError(msg)
    try:
        return [EncryptedBso.model_validate(item) for item in cast(list[Any], payload)]
    except ValidationError as exc:
        msg = f"collection「{collection}」里有认不出来的记录"
        raise SyncProtocolError(msg) from exc


def _counts_from_json(response: httpx.Response) -> dict[str, int]:
    """``/info/collection_counts`` 的响应 → ``{collection: 条数}``。"""
    try:
        payload: object = response.json()
    except ValueError as exc:
        msg = "collection 计数的响应不是合法 JSON"
        raise SyncProtocolError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"collection 计数的响应不是一个对象，拿到 {type(payload).__name__}"
        raise SyncProtocolError(msg)
    try:
        return {str(name): int(count) for name, count in cast(dict[str, Any], payload).items()}
    except (TypeError, ValueError) as exc:
        msg = "collection 计数的响应里有不是数字的值"
        raise SyncProtocolError(msg) from exc


def _body_snippet(response: httpx.Response) -> str:
    """错误信息里带一小段响应体 —— 够定位就行，别把整页灌进日志。"""
    text = response.text.strip()
    return text[:200] if text else "（空响应体）"
