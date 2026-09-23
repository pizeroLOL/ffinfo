# pyright: reportPrivateUsage=false
# 上面这条：Hawk 签名的关键步骤没有公开面，测试只能直接验私有函数。
"""Sync 存储协议：Hawk 签名、分页、退避。

全部离线可测：Hawk 那条用的是 **rust-hawk 自己的测试向量**
（app-services ``rc_crypto/src/hawk_crypto.rs`` 里逐字抄过来的），
分页与退避用 ``httpx.MockTransport`` 回放**录制的 HTTP 夹具**，不打真实网络。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ffinfo.errors import BackoffError, SyncProtocolError
from ffinfo.storage import (
    BackoffState,
    EncryptedBso,
    FetchProgress,
    HawkCredentials,
    SyncStorageClient,
    _normalized_request,
    _parse_seconds,
    format_timestamp,
    hawk_authorization,
)

FIXTURES = Path(__file__).parent / "fixtures"

TOKEN_JSON: dict[str, Any] = {
    "id": "eyJub2RlIjoiZmFrZSJ9.SIGNATURE",
    "key": "yLw9efSZimFakeKeyForTests0000000000000000000",
    "uid": 12345,
    "api_endpoint": "https://sync.test/1.5/12345",
    "duration": 3600,
    "hashed_fxa_uid": "deadbeefdeadbeefdeadbeefdeadbeef",
    "hashalg": "sha256",
    "node_type": "spanner",
}

NOW = 1_789_320_612.0


class FakeSync:
    """一台假 Sync 服务器：按路径分发，按顺序回放编排好的页。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.token_status = 200
        self.token_json: dict[str, Any] = dict(TOKEN_JSON)
        self.token_headers: dict[str, str] = {}
        self.rotate_endpoint_to: str | None = None
        self.counts: dict[str, int] = {"history": 3}
        self.counts_status = 200
        self.pages: list[httpx.Response] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """MockTransport 的入口。"""
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/1.0/sync/1.5"):
            self.token_calls += 1
            body = dict(self.token_json)
            if self.rotate_endpoint_to is not None and self.token_calls > 1:
                body["api_endpoint"] = self.rotate_endpoint_to
            return httpx.Response(self.token_status, json=body, headers=self.token_headers)
        if path.endswith("/info/collection_counts"):
            return httpx.Response(self.counts_status, json=self.counts)
        if "/storage/" in path:
            if not self.pages:
                raise AssertionError(f"页用完了，但又来了一个请求：{request.url}")
            return self.pages.pop(0)
        raise AssertionError(f"没编排过的请求：{request.url}")

    def client(self, *, clock: Any = None) -> SyncStorageClient:
        """造一个指向这台假服务器的客户端。"""
        return SyncStorageClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            access_token="ACCESS-TOKEN",
            key_id="163327099951-abcdef",
            token_server_url="https://token.test/1.0/sync/1.5",
            clock=clock or (lambda: NOW),
        )


def page(records: list[dict[str, Any]], *, next_offset: str | None = None) -> httpx.Response:
    """造一页响应。``X-Last-Modified`` 是所有成功响应都该有的头。"""
    headers = {"X-Last-Modified": "1789320600.12"}
    if next_offset is not None:
        headers["X-Weave-Next-Offset"] = next_offset
    return httpx.Response(200, json=records, headers=headers)


def bso(record_id: str) -> dict[str, Any]:
    """一条正常的加密记录。"""
    return {
        "id": record_id,
        "modified": 1789320500.5,
        "payload": json.dumps({"ciphertext": "AAAA", "IV": "BBBB", "hmac": "CCCC"}),
    }


def storage_requests(fake: FakeSync, collection: str) -> list[httpx.Request]:
    """只看某个 collection 的存储请求。"""
    return [r for r in fake.requests if f"/storage/{collection}" in r.url.path]


def test_hawk_matches_rust_hawk_vector() -> None:
    """rust-hawk 的官方测试向量 —— 规范化串错一个字符这里就红。"""
    credentials = HawkCredentials(
        id="some-id",
        key=bytes(
            [
                11,
                19,
                228,
                209,
                79,
                189,
                200,
                59,
                166,
                47,
                86,
                254,
                235,
                184,
                120,
                197,
                75,
                152,
                201,
                79,
                115,
                61,
                111,
                242,
                219,
                187,
                173,
                14,
                227,
                108,
                60,
                232,
            ]
        ),
    )
    header = hawk_authorization(
        credentials=credentials,
        method="POST",
        url=httpx.URL("https://mysite.com/v1/api"),
        timestamp=1000.1,
        nonce="nonny",
    )

    assert 'id="some-id"' in header
    assert 'ts="1000"' in header  # as_secs 向下取整
    mac = base64.b64decode(header.split('mac="')[1].split('"')[0])
    assert mac == bytes(
        [
            192,
            227,
            235,
            121,
            157,
            185,
            197,
            79,
            189,
            214,
            235,
            139,
            9,
            232,
            99,
            55,
            67,
            30,
            68,
            0,
            150,
            187,
            192,
            238,
            21,
            200,
            209,
            107,
            245,
            159,
            243,
            178,
        ]
    )


def test_normalized_request_shape() -> None:
    """规范化串的确切形状：query 要带上，hash/ext 两个空行不能少。"""
    normalized = _normalized_request(
        method="GET",
        url=httpx.URL("https://sync.test/1.5/42/storage/history?full=1&limit=100"),
        timestamp=1789320612,
        nonce="abcdefghij",
    )
    assert normalized == (
        "hawk.1.header\n"
        "1789320612\n"
        "abcdefghij\n"
        "GET\n"
        "/1.5/42/storage/history?full=1&limit=100\n"
        "sync.test\n"
        "443\n"
        "\n"
        "\n"
    )


def test_normalized_request_without_query() -> None:
    """没有 query 时不留问号。"""
    normalized = _normalized_request(
        method="GET",
        url=httpx.URL("https://sync.test/1.5/42/info/collection_counts"),
        timestamp=1,
        nonce="n",
    )
    assert "\n/1.5/42/info/collection_counts\n" in normalized


def test_normalized_request_uses_explicit_port() -> None:
    """URL 里写了端口就用写的那个。"""
    normalized = _normalized_request(
        method="GET", url=httpx.URL("http://localhost:8080/x"), timestamp=1, nonce="n"
    )
    assert "\nlocalhost\n8080\n" in normalized


def test_non_ascii_token_key_raises_sync_protocol_error() -> None:
    """非 ASCII ``key`` → ``SyncProtocolError``，不再逃出裸 ``UnicodeEncodeError``。

    ``key`` 是服务端可控输入；畸形响应必须落在 ``FfinfoError`` 异常层次里。
    """
    from ffinfo.errors import FfinfoError
    from ffinfo.storage import TokenserverToken

    token = TokenserverToken(id="id", key="ключ", api_endpoint="https://sync.test/")

    with pytest.raises(SyncProtocolError, match="token 响应不合预期") as excinfo:
        token.hawk_credentials()

    assert isinstance(excinfo.value, FfinfoError)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1.0),
        ("1.4", 2.0),  # 向上取整
        ("3600.0", 3600.0),
        ("0", 0.0),
        ("-1", None),
        ("inf", None),
        ("nan", None),
        ("soon", None),
        (None, None),
    ],
)
def test_parse_seconds(raw: str | None, expected: float | None) -> None:
    assert _parse_seconds(raw) == expected


def test_backoff_state_takes_max_and_expires() -> None:
    state = BackoffState()
    state.note_soft(5, now=100.0)
    state.note_soft(2, now=100.0)  # 小的不覆盖大的
    state.note_hard(30, now=100.0)

    assert state.required_wait(now=100.0) == 30.0
    assert state.required_wait(now=125.0) == 5.0
    assert state.required_wait(now=200.0) == 0.0


async def test_token_request_carries_bearer_and_key_id() -> None:
    """``X-KeyID`` 是必需的 —— 实测不带就是 401 ``invalid-key-id``。"""
    fake = FakeSync()
    client = fake.client()

    token = await client.token()

    request = fake.requests[0]
    assert request.headers["Authorization"] == "Bearer ACCESS-TOKEN"
    assert request.headers["X-KeyID"] == "163327099951-abcdef"
    assert token.api_endpoint == "https://sync.test/1.5/12345"


async def test_token_is_cached_until_it_expires() -> None:
    """同一次拉取里不该反复问 tokenserver 要凭证。"""
    fake = FakeSync()
    fake.pages = [page([bso("a")])]
    fake.counts = {"history": 1}
    clock = [NOW]
    client = fake.client(clock=lambda: clock[0])

    await client.fetch_collection("history", verify_count=False)
    clock[0] = NOW + 10  # 还在有效期内
    await client.collection_counts()

    assert fake.token_calls == 1


async def test_token_is_refreshed_before_expiry() -> None:
    """有效期 3600 秒，但要提前换 —— 别拿着快过期的去撞墙。"""
    fake = FakeSync()
    clock = [NOW]
    client = fake.client(clock=lambda: clock[0])

    await client.token()
    clock[0] = NOW + 3600  # 名义上还没过期，但已经进了 60 秒余量
    await client.token()

    assert fake.token_calls == 2


async def test_token_error_is_reported() -> None:
    fake = FakeSync()
    fake.token_status = 401
    fake.token_json = {"status": "invalid-key-id"}  # type: ignore[assignment]
    client = fake.client()

    with pytest.raises(SyncProtocolError, match="tokenserver 拒绝了"):
        await client.token()


async def test_pages_until_next_offset_disappears() -> None:
    """翻页到底：第二页没有 ``X-Weave-Next-Offset`` 就收工。"""
    fake = FakeSync()
    fake.pages = [page([bso("a"), bso("b")], next_offset="OFFSET-1"), page([bso("c")])]
    fake.counts = {"history": 3}

    result = await fake.client().fetch_collection("history")

    assert [record.id for record in result.records] == ["a", "b", "c"]
    assert result.pages == 2
    assert result.count == 3
    assert result.server_count == 3

    storage_requests = [r for r in fake.requests if "/storage/" in r.url.path]
    assert storage_requests[0].url.params["full"] == "1"
    assert storage_requests[0].url.params["limit"] == "100"
    assert "offset" not in storage_requests[0].url.params
    assert storage_requests[1].url.params["offset"] == "OFFSET-1"


async def test_second_page_carries_if_unmodified_since() -> None:
    """翻页要带上第一页的 ``X-Last-Modified`` —— 集合中途被改才能发现。"""
    fake = FakeSync()
    fake.pages = [page([bso("a")], next_offset="OFFSET-1"), page([bso("b")])]
    fake.counts = {"history": 2}

    await fake.client().fetch_collection("history")

    first, second = [r for r in fake.requests if "/storage/" in r.url.path]
    assert "X-If-Unmodified-Since" not in first.headers
    assert second.headers["X-If-Unmodified-Since"] == "1789320600.12"


async def test_incremental_request_carries_newer() -> None:
    """给了 ``newer`` 就只拉变更 —— 时间戳要按服务器要的格式（两位小数）带上。"""
    fake = FakeSync()
    fake.pages = [page([bso("a")])]

    result = await fake.client().fetch_collection("history", newer=1789320619.514)

    request = storage_requests(fake, "history")[0]
    assert request.url.params["newer"] == "1789320619.51"  # 向下取整
    assert result.count == 1


async def test_incremental_skips_the_count_check() -> None:
    """增量拉回来的只是变更集，条数跟整个 collection 对不上 —— 比了没意义。"""
    fake = FakeSync()
    fake.pages = [page([bso("a")])]

    result = await fake.client().fetch_collection("history", newer=1.0)

    assert result.server_count is None
    assert not [r for r in fake.requests if "/info/" in r.url.path]


def test_timestamp_is_floored_never_rounded_up() -> None:
    """``newer`` 是"严格大于" —— 向上取整会**跳过**落在中间那零点几秒里的记录。"""
    assert format_timestamp(1789320619.51) == "1789320619.51"
    assert format_timestamp(1789320619.514) == "1789320619.51"
    assert format_timestamp(1789320619.516) == "1789320619.51"  # 不是 .52
    assert format_timestamp(1789320619.999) == "1789320619.99"


async def test_incremental_pages_keep_newer_on_every_page() -> None:
    """翻页时 ``newer`` 不能丢 —— 丢了第二页就变成全量了。"""
    fake = FakeSync()
    fake.pages = [page([bso("a")], next_offset="O1"), page([bso("b")])]

    await fake.client().fetch_collection("history", newer=1789320619.51)

    first, second = storage_requests(fake, "history")
    assert first.url.params["newer"] == "1789320619.51"
    assert second.url.params["newer"] == "1789320619.51"
    assert second.url.params["offset"] == "O1"


async def test_records_are_left_encrypted() -> None:
    """硬要求：拉下来的 payload 原样保留，不解密。"""
    fake = FakeSync()
    encrypted = json.dumps({"ciphertext": "AAAA", "IV": "BBBB", "hmac": "CCCC"})
    fake.pages = [page([{"id": "a", "modified": 1.0, "payload": encrypted}])]
    fake.counts = {"history": 1}

    result = await fake.client().fetch_collection("history")

    assert result.records[0].payload == encrypted


async def test_tombstone_has_no_payload() -> None:
    """墓碑记录（别的设备删掉的）也是记录，只是没有 payload。"""
    fake = FakeSync()
    fake.pages = [page([{"id": "gone", "modified": 1.0, "payload": None}])]
    fake.counts = {"history": 1}

    result = await fake.client().fetch_collection("history")

    assert result.records[0].is_tombstone


async def test_storage_request_is_hawk_signed() -> None:
    """存储请求走 Hawk，不是 Bearer。"""
    fake = FakeSync()
    fake.pages = [page([bso("a")])]
    fake.counts = {"history": 1}

    await fake.client().fetch_collection("history")

    storage = next(r for r in fake.requests if "/storage/" in r.url.path)
    authorization = storage.headers["Authorization"]
    assert authorization.startswith('Hawk id="eyJub2RlIjoiZmFrZSJ9.SIGNATURE", mac="')
    assert 'nonce="' in authorization


async def test_count_mismatch_is_an_error() -> None:
    """拉到的条数和服务器报告的对不上 —— 宁可报错，也不假装拉全了。"""
    fake = FakeSync()
    fake.pages = [page([bso("a"), bso("b")])]
    fake.counts = {"history": 5}

    with pytest.raises(SyncProtocolError, match="服务器报告有 5 条"):
        await fake.client().fetch_collection("history")


async def test_verify_count_can_be_skipped() -> None:
    """关掉校验就不问计数接口。"""
    fake = FakeSync()
    fake.pages = [page([bso("a")])]

    result = await fake.client().fetch_collection("history", verify_count=False)

    assert result.server_count is None
    assert not [r for r in fake.requests if "/info/" in r.url.path]


async def test_unknown_collection_counts_as_empty() -> None:
    """服务器没提过的 collection 就是 0 条。"""
    fake = FakeSync()
    fake.counts = {"bookmarks": 7}
    fake.pages = [page([])]

    result = await fake.client().fetch_collection("history")

    assert result.count == 0
    assert result.server_count == 0


async def test_soft_backoff_stops_paging() -> None:
    """``X-Weave-Backoff`` 挂在 200 上：这一页收下，下一页不发了。"""
    fake = FakeSync()
    fake.pages = [
        httpx.Response(
            200,
            json=[bso("a")],
            headers={
                "X-Last-Modified": "1789320600.12",
                "X-Weave-Next-Offset": "O1",
                "X-Weave-Backoff": "2",
            },
        ),
    ]
    fake.counts = {"history": 3}

    with pytest.raises(BackoffError) as caught:
        await fake.client().fetch_collection("history")

    assert caught.value.soft is True
    assert caught.value.wait_seconds == 2.0
    assert len([r for r in fake.requests if "/storage/" in r.url.path]) == 1


async def test_hard_backoff_on_503() -> None:
    """503 + ``Retry-After`` —— 硬退避，别硬刚。"""
    fake = FakeSync()
    fake.pages = [httpx.Response(503, headers={"Retry-After": "30"}, text="maintenance")]
    fake.counts = {"history": 3}

    with pytest.raises(BackoffError) as caught:
        await fake.client().fetch_collection("history")

    assert caught.value.soft is False
    assert caught.value.wait_seconds == 30.0


async def test_backoff_window_is_remembered() -> None:
    """退避窗口内连请求都不发 —— 省得被服务器当没听见。"""
    fake = FakeSync()
    fake.pages = [httpx.Response(503, headers={"Retry-After": "30"})]
    clock = [NOW]
    client = fake.client(clock=lambda: clock[0])

    with pytest.raises(BackoffError):
        await client.fetch_collection("history", verify_count=False)
    requests_before = len(fake.requests)

    clock[0] = NOW + 10  # 还在窗口里
    with pytest.raises(BackoffError, match="还需等待"):
        await client.collection_counts()
    assert len(fake.requests) == requests_before

    clock[0] = NOW + 31  # 窗口过了，放行
    await client.collection_counts()
    assert len(fake.requests) == requests_before + 1


async def test_503_without_retry_after_uses_fallback() -> None:
    """服务器没说等多久就按 10 秒算（对齐 app-services 的默认值）。"""
    fake = FakeSync()
    fake.pages = [httpx.Response(503)]
    fake.counts = {"history": 1}

    with pytest.raises(BackoffError) as caught:
        await fake.client().fetch_collection("history")

    assert caught.value.wait_seconds == 10.0


async def test_412_retries_the_whole_fetch() -> None:
    """412 是"读到一半集合被改了" —— 整段重来，这次拿到干净快照。"""
    fake = FakeSync()
    fake.pages = [
        page([bso("a")], next_offset="O1"),
        httpx.Response(412),  # 第一次：翻到第二页时集合被改了
        page([bso("a"), bso("b")]),  # 重试：一次读完
    ]
    fake.counts = {"history": 2}

    result = await fake.client().fetch_collection("history")

    assert [record.id for record in result.records] == ["a", "b"]
    assert result.pages == 1
    assert len([r for r in fake.requests if "/storage/" in r.url.path]) == 3


async def test_412_gives_up_after_retries() -> None:
    """重试 3 次还是被改就报错，不无限转。"""
    fake = FakeSync()
    fake.pages = [httpx.Response(412) for _ in range(4)]
    fake.counts = {"history": 1}

    with pytest.raises(SyncProtocolError, match="重试 3 次都没拿到一致快照"):
        await fake.client().fetch_collection("history")

    assert len([r for r in fake.requests if "/storage/" in r.url.path]) == 4


async def test_401_with_same_endpoint_retries_once() -> None:
    """401 但端点没变 —— 换个 token 再试一次就好。"""
    fake = FakeSync()
    fake.pages = [httpx.Response(401), page([bso("a")])]
    fake.counts = {"history": 1}

    result = await fake.client().fetch_collection("history")

    assert result.count == 1
    assert fake.token_calls == 2


async def test_401_with_new_endpoint_aborts() -> None:
    """端点变了 = 节点重分配，这次拉取作废。"""
    fake = FakeSync()
    fake.rotate_endpoint_to = "https://sync-2.test/1.5/12345"
    fake.pages = [httpx.Response(401)]
    fake.counts = {"history": 1}

    with pytest.raises(SyncProtocolError, match="节点重分配"):
        await fake.client().fetch_collection("history")


async def test_missing_last_modified_is_an_error() -> None:
    """没有 ``X-Last-Modified`` 就没法保证翻页一致性，直接失败。"""
    fake = FakeSync()
    fake.pages = [httpx.Response(200, json=[bso("a")])]
    fake.counts = {"history": 1}

    with pytest.raises(SyncProtocolError, match="X-Last-Modified"):
        await fake.client().fetch_collection("history")


async def test_non_list_body_is_an_error() -> None:
    fake = FakeSync()
    fake.pages = [httpx.Response(200, json={"oops": True}, headers={"X-Last-Modified": "1"})]
    fake.counts = {"history": 1}

    with pytest.raises(SyncProtocolError, match="不是一个列表"):
        await fake.client().fetch_collection("history")


async def test_http_status_other_than_ok_is_an_error() -> None:
    fake = FakeSync()
    fake.pages = [httpx.Response(500, text="boom")]
    fake.counts = {"history": 1}

    with pytest.raises(SyncProtocolError, match="HTTP 500"):
        await fake.client().fetch_collection("history")


async def test_network_failure_is_wrapped() -> None:
    """连不上时抛的是我们的异常，不是 httpx 的。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = SyncStorageClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        access_token="AT",
        key_id="KID",
        token_server_url="https://token.test/1.0/sync/1.5",
        clock=lambda: NOW,
    )

    with pytest.raises(SyncProtocolError, match="连不上 Sync 服务器"):
        await client.token()


async def test_replays_recorded_fixture() -> None:
    """回放**真实录制**的响应（token / counts / 两页历史），脱敏后入库。

    这条不打网络 —— 它保证的是"真实响应的形状"我们能吃下去，
    而上面的用例保证的是逻辑对。
    """
    recording = json.loads((FIXTURES / "storage_history.json").read_text(encoding="utf-8"))
    pages: list[Any] = list(recording["pages"])
    offsets: list[str] = list(recording["next_offsets"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/1.0/sync/1.5"):
            return httpx.Response(200, json=recording["token"])
        if path.endswith("/info/collection_counts"):
            return httpx.Response(200, json=recording["counts"])
        headers = {"X-Last-Modified": recording["last_modified"]}
        if offsets:
            headers["X-Weave-Next-Offset"] = offsets.pop(0)
        return httpx.Response(200, json=pages.pop(0), headers=headers)

    client = SyncStorageClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        access_token="AT",
        key_id="KID",
        token_server_url="https://token.test/1.0/sync/1.5",
        clock=lambda: NOW,
    )

    result = await client.fetch_collection("history")

    assert result.count == recording["expected_count"]
    assert result.server_count == recording["expected_count"]
    assert all(record.payload is not None for record in result.records)


def test_encrypted_bso_ignores_unknown_fields() -> None:
    """服务器多给字段不该把我们打挂。"""
    record = EncryptedBso.model_validate(
        {"id": "a", "modified": 1.0, "payload": "x", "sortindex": 5, "ttl": 60, "future": True}
    )
    assert record.sortindex == 5


async def test_progress_callback_fires_once_per_page() -> None:
    """每翻完一页报一次 —— 命令行那层才有东西可显示。

    库只管**报事实**（第几页、多少条）；"怎么显示、显示不显示"是调用方的事
    （``docs/design.md`` §2.5 的老规矩：库不替应用做决定）。
    """
    fake = FakeSync()
    fake.counts = {"history": 3}
    fake.pages = [
        page([bso("a")], next_offset="O1"),
        page([bso("b")], next_offset="O2"),
        page([bso("c")]),
    ]
    seen: list[FetchProgress] = []

    result = await fake.client().fetch_collection("history", on_progress=seen.append)

    assert result.pages == 3
    assert [(item.pages, item.records) for item in seen] == [(1, 1), (2, 2), (3, 3)]
    assert {item.collection for item in seen} == {"history"}


async def test_progress_is_optional() -> None:
    """不给回调就照常拉 —— 进度是锦上添花，不是必经之路。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [page([bso("a")])]

    result = await fake.client().fetch_collection("history")

    assert result.count == 1


async def test_progress_reports_after_a_retry_not_before() -> None:
    """412 重试时，进度报的是**这一趟**翻了几页 —— 不把上一趟的页数累进去。"""
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [
        page([bso("a")], next_offset="O1"),
        httpx.Response(412),
        page([bso("a")], next_offset="O1"),
        page([bso("b")]),
    ]
    seen: list[FetchProgress] = []

    result = await fake.client().fetch_collection("history", on_progress=seen.append)

    assert result.count == 2
    assert [(item.pages, item.records) for item in seen] == [(1, 1), (1, 1), (2, 2)]
