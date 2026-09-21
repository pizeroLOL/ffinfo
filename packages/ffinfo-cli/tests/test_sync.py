"""``ffinfo-cli sync``：白名单、落盘、失败不写库。

全部离线：HTTP 走 ``httpx.MockTransport``，凭据写进临时目录。

大多数用例只注入 ``history`` 一个 collection（``run_sync`` 的 ``collections`` 形参）——
默认三件套与协议数据另有专测，免得每条断言都被另外两个 collection 的页数搅浑。
"""

# 上面三行：同上 —— 测试直接查表类验证落库结果。
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownParameterType=false
# pyright: reportUnknownLambdaType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.errors import AuthError, BackoffError, ConfigurationError, SyncProtocolError
from ffinfo.keys import OLD_SYNC_SCOPE, ScopedKey
from ffinfo.oauth import Credentials
from ffinfo_cli.render import render
from ffinfo_cli.store import SyncCursor, SyncRecord, open_database
from ffinfo_cli.sync import SYNCABLE_COLLECTIONS, CollectedSync, SyncReport, run_sync

NOW = 1_789_320_612.0

TOKEN_JSON: dict[str, Any] = {
    "id": "eyJub2RlIjoiZmFrZSJ9.SIGNATURE",
    "key": "yLw9efSZimFakeKeyForTests0000000000000000000",
    "uid": 12345,
    "api_endpoint": "https://sync.test/1.5/12345",
    "duration": 3600,
}


def write_credentials(
    tmp_path: Path,
    *,
    expires_at: float = NOW + 3600,
    with_scope: bool = True,
    refresh_token: str | None = None,
) -> tuple[Path, Path]:
    """造一份真的 age 加密凭据 —— 走和 login 一样的路径。"""
    identity = AgeIdentity.generate()
    identity_path = tmp_path / "age-key.txt"
    identity.to_file(identity_path)

    keys: dict[str, ScopedKey] = {}
    if with_scope:
        k = base64.urlsafe_b64encode(bytes(range(64))).decode("ascii").rstrip("=")
        keys[OLD_SYNC_SCOPE] = ScopedKey(kty="EC", scope=OLD_SYNC_SCOPE, k=k, kid="KID-123")

    credentials = Credentials(
        access_token="ACCESS-TOKEN",
        refresh_token=refresh_token,
        expires_at=expires_at,
        scoped_keys=keys,
    )
    credentials_path = tmp_path / "credentials.age"
    CredentialStore(identity=identity, path=credentials_path).save(credentials.to_json())
    return identity_path, credentials_path


class FakeSync:
    """一台假 Sync 服务器。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.pages: list[list[dict[str, Any]]] = []
        self.crypto_pages: list[list[dict[str, Any]]] = []
        self.counts: dict[str, int] = {}
        self.storage_status = 200
        self.storage_headers: dict[str, str] = {}
        self.refresh_response: httpx.Response | None = None
        """token 端点的响应 —— 需要刷新的测试自己塞一个。"""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/token":
            if self.refresh_response is None:
                msg = "这个测试没安排刷新 —— 给 fake.refresh_response 塞一个响应"
                raise AssertionError(msg)
            return self.refresh_response
        if path.endswith("/1.0/sync/1.5"):
            return httpx.Response(200, json=TOKEN_JSON)
        if path.endswith("info/collection_counts"):
            # crypto 是协议数据，每次 sync 都会顺带拉 —— 计数由这个类自己兜住
            return httpx.Response(200, json={"crypto": 1, **self.counts})
        if self.storage_status != 200:
            return httpx.Response(self.storage_status, headers=self.storage_headers)
        if "/storage/crypto" in path:
            # crypto 是每次 sync 都会拉的协议数据，测试不用为它排队
            page = self.crypto_pages.pop(0) if self.crypto_pages else [keys_bso()]
            return httpx.Response(200, json=page, headers={"X-Last-Modified": "1.0"})
        return httpx.Response(
            200, json=self.pages.pop(0), headers={"X-Last-Modified": "1789320600.12"}
        )


def refresh_requests(fake: FakeSync) -> list[httpx.Request]:
    """只看打给 token 端点的那些请求。"""
    return [r for r in fake.requests if r.url.path == "/v1/token"]


def storage_requests(fake: FakeSync, collection: str) -> list[httpx.Request]:
    """只看某个 collection 的存储请求 —— 协议数据会掺进来，别数错。"""
    return [r for r in fake.requests if f"/storage/{collection}" in r.url.path]


def bso(record_id: str) -> dict[str, Any]:
    return {"id": record_id, "modified": 1789320500.5, "payload": '{"ciphertext":"AAAA"}'}


def tombstone(record_id: str) -> dict[str, Any]:
    """墓碑：这条在别的设备上被删了。"""
    return {"id": record_id, "modified": 1789320501.0, "payload": None}


def keys_bso() -> dict[str, Any]:
    """``crypto/keys`` 那条记录 —— 每次 sync 都会顺带拉它。"""
    return {"id": "keys", "modified": 1.0, "payload": '{"ciphertext":"AAAA"}'}


async def stored(tmp_path: Path, collection: str = "history") -> int:
    """库里现在有多少条 —— 库还没建就是 0。"""
    if not (tmp_path / "db.sqlite").exists():
        return 0
    await open_database(tmp_path / "db.sqlite")
    return await SyncRecord.count().where(SyncRecord.collection == collection)


async def cursor(tmp_path: Path, collection: str = "history") -> float | None:
    """当前的同步游标 —— 没有就是 ``None``。"""
    if not (tmp_path / "db.sqlite").exists():
        return None
    store = await open_database(tmp_path / "db.sqlite")
    return await store.load_cursor(collection)


def only(report: SyncReport) -> CollectedSync:
    """只注入了一个 collection 的用例 —— 取出那唯一一份明细。"""
    assert len(report.collections) == 1
    return report.collections[0]


async def sync(
    tmp_path: Path,
    fake: FakeSync,
    collections: tuple[str, ...] = ("history",),
    *,
    credentials: tuple[Path, Path] | None = None,
    **kwargs: Any,
) -> SyncReport:
    """跑一次 sync，用假服务器。"""
    identity_path, credentials_path = credentials or write_credentials(tmp_path)
    return await run_sync(
        identity_path=identity_path,
        credentials_path=credentials_path,
        database_path=tmp_path / "db.sqlite",
        collections=collections,
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        clock=lambda: NOW,
        **kwargs,
    )


@pytest.mark.parametrize(
    "collection", ["forms", "passwords", "creditcards", "addresses", "meta", "crypto", "prefs"]
)
async def test_unsafe_collections_are_refused(tmp_path: Path, collection: str) -> None:
    """``forms`` 里有来历不明的遗留记录（可能含密码）—— 一条都不拉。"""
    fake = FakeSync()
    fake.counts = {collection: 42768}

    with pytest.raises(ConfigurationError, match="只拉这几个"):
        await sync(tmp_path, fake, collections=(collection,))

    assert fake.requests == []  # 连网都不上
    assert not (tmp_path / "db.sqlite").exists()


def test_allowlist_matches_the_documented_scope() -> None:
    """白名单就是设计文档决策 4 那句话：历史 + 书签 + 标签页。"""
    assert sorted(SYNCABLE_COLLECTIONS) == ["bookmarks", "history", "tabs"]
    assert "forms" not in SYNCABLE_COLLECTIONS


async def test_happy_path_stores_records(tmp_path: Path) -> None:
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]

    report = await sync(tmp_path, fake)
    entry = only(report)

    assert entry.collection == "history"
    assert entry.records == 2
    assert entry.pages == 1
    assert entry.tombstones == 0
    assert entry.server_count == 2
    assert report.database == str(tmp_path / "db.sqlite")

    await open_database(tmp_path / "db.sqlite")
    assert await SyncRecord.count().where(SyncRecord.collection == "history") == 2


async def test_protocol_data_is_pulled_alongside(tmp_path: Path) -> None:
    """``crypto/keys`` 是解密要用的 —— 拉 history 时顺带拉下来，不用单独跑一次。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]

    report = await sync(tmp_path, fake)

    assert report.protocol == {"crypto": 1}
    await open_database(tmp_path / "db.sqlite")
    stored_records = await SyncRecord.select().where(SyncRecord.collection == "crypto")
    assert [row["record_id"] for row in stored_records] == ["keys"]


async def test_tombstones_are_counted_and_dropped(tmp_path: Path) -> None:
    """墓碑不进库（那一行不该存在），但报告里要说清楚服务器报了几条删除。"""
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), {"id": "gone", "modified": 1.0, "payload": None}]]

    report = await sync(tmp_path, fake)
    entry = only(report)

    assert entry.tombstones == 1
    assert entry.server_count == 2
    assert await stored(tmp_path) == 1


async def test_report_json_is_machine_readable(tmp_path: Path) -> None:
    """agent 消费的接口 —— 带格式版本，能被 json 直接吃。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]

    report = await sync(tmp_path, fake)
    payload = json.loads(render(report, machine=True))

    assert payload["format_version"] == 2
    assert set(payload) >= {"collections", "elapsed_seconds", "database", "protocol"}
    entry = payload["collections"][0]
    assert entry["collection"] == "history"
    assert set(entry) >= {
        "collection",
        "mode",
        "records",
        "inserted",
        "updated",
        "deleted",
        "pages",
        "tombstones",
        "server_count",
        "cursor_before",
        "cursor_after",
    }


async def test_token_server_request_carries_key_id(tmp_path: Path) -> None:
    """``X-KeyID`` 是从 scoped key 的 kid 来的 —— 实测不带就是 401。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]

    await sync(tmp_path, fake)

    token_request = next(r for r in fake.requests if r.url.path.endswith("/1.0/sync/1.5"))
    assert token_request.headers["X-KeyID"] == "KID-123"


async def test_backoff_leaves_database_untouched(tmp_path: Path) -> None:
    """服务器要求退避 —— 报错走人，库里一个字节都不动。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.storage_status = 503
    fake.storage_headers = {"Retry-After": "30"}

    with pytest.raises(BackoffError) as caught:
        await sync(tmp_path, fake)

    assert caught.value.wait_seconds == 30.0
    assert await stored(tmp_path) == 0
    assert await cursor(tmp_path) is None


async def test_count_mismatch_leaves_database_untouched(tmp_path: Path) -> None:
    """条数对不上 = 没拉全 —— 宁可什么都不写。"""
    fake = FakeSync()
    fake.counts = {"history": 5}
    fake.pages = [[bso("a")]]

    with pytest.raises(SyncProtocolError, match="服务器报告有 5 条"):
        await sync(tmp_path, fake)

    assert await stored(tmp_path) == 0
    assert await cursor(tmp_path) is None


async def test_expired_credentials_are_refreshed_instead_of_asking_again(tmp_path: Path) -> None:
    """token 过期不劳烦用户 —— refresh token 自己续上（RFC 6749 §6），续完照常拉。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    fake.refresh_response = httpx.Response(
        200,
        json={
            "access_token": "FRESH-TOKEN",
            "expires_in": 3600,
            "refresh_token": "ROTATED-REFRESH",
            "scope": OLD_SYNC_SCOPE,
        },
    )
    credentials = write_credentials(tmp_path, expires_at=NOW - 1, refresh_token="OLD-REFRESH")
    identity_path, credentials_path = credentials
    store = CredentialStore(identity=AgeIdentity.from_file(identity_path), path=credentials_path)
    keys_before = Credentials.from_json(store.load()).scoped_keys

    report = await sync(tmp_path, fake, credentials=credentials)

    assert only(report).inserted == 1
    sent = refresh_requests(fake)[0]
    body = parse_qs(sent.content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["OLD-REFRESH"]

    saved = Credentials.from_json(store.load())
    assert saved.access_token == "FRESH-TOKEN"
    assert saved.refresh_token == "ROTATED-REFRESH"  # 轮换过的那份要存回去
    assert saved.expires_at == NOW + 3600
    assert saved.scoped_keys == keys_before  # 密钥没动（它本来不过期）


async def test_dead_refresh_token_says_to_login_again(tmp_path: Path) -> None:
    """refresh token 也失效了 —— 别让人猜，直接说"重新授权一次"。"""
    fake = FakeSync()
    fake.refresh_response = httpx.Response(400, json={"error": "invalid_grant"})
    credentials = write_credentials(tmp_path, expires_at=NOW - 1, refresh_token="DEAD")

    with pytest.raises(AuthError, match="重新授权"):
        await sync(tmp_path, fake, credentials=credentials)

    assert storage_requests(fake, "history") == []  # 没拿一份坏 token 去撞存储端点


async def test_expired_credentials_without_a_refresh_token_say_to_login(tmp_path: Path) -> None:
    """老凭据里没有 refresh token —— 连试都不试，消息说清只能重新 login。"""
    fake = FakeSync()
    credentials = write_credentials(tmp_path, expires_at=NOW - 1)

    with pytest.raises(AuthError, match="没有 refresh token"):
        await sync(tmp_path, fake, credentials=credentials)

    assert fake.requests == []


async def test_credentials_without_sync_scope_are_refused(tmp_path: Path) -> None:
    """凭据里没有 oldsync 的密钥 —— 拿不到同步数据。"""
    fake = FakeSync()
    credentials = write_credentials(tmp_path, with_scope=False)

    with pytest.raises(ConfigurationError, match="oldsync"):
        await sync(tmp_path, fake, credentials=credentials)

    assert fake.requests == []


async def test_first_sync_is_full(tmp_path: Path) -> None:
    """第一次没有游标 —— 全量，请求里不带 ``newer``。"""
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]

    report = await sync(tmp_path, fake)
    entry = only(report)

    assert entry.mode == "full"
    assert entry.cursor_before is None
    assert entry.cursor_after == 1789320600.12
    assert "newer" not in storage_requests(fake, "history")[0].url.params
    assert await cursor(tmp_path) == 1789320600.12


async def test_second_sync_is_incremental(tmp_path: Path) -> None:
    """第二次带上游标 —— 只请求变更，而且**不能**把上一轮的数据端掉。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    await sync(tmp_path, fake)

    fake.pages = [[bso("b")]]
    report = await sync(tmp_path, fake)
    entry = only(report)

    assert entry.mode == "incremental"
    assert entry.cursor_before == 1789320600.12
    assert entry.inserted == 1
    assert storage_requests(fake, "history")[1].url.params["newer"] == "1789320600.12"
    assert await stored(tmp_path) == 2


async def test_incremental_with_no_changes(tmp_path: Path) -> None:
    """第三次一条变更都没有 —— 库里不动，游标照常推进（服务器说了算）。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    await sync(tmp_path, fake)

    fake.pages = [[]]
    report = await sync(tmp_path, fake)
    entry = only(report)

    assert entry.mode == "incremental"
    assert entry.inserted == 0
    assert entry.updated == 0
    assert entry.deleted == 0
    assert await stored(tmp_path) == 1


async def test_incremental_updates_a_changed_record(tmp_path: Path) -> None:
    """同一条记录变了 —— 覆盖，不是插一条新的。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    await sync(tmp_path, fake)

    fake.pages = [[{"id": "a", "modified": 1789320999.0, "payload": '{"ciphertext":"BBBB"}'}]]
    report = await sync(tmp_path, fake)
    entry = only(report)

    assert entry.updated == 1
    assert entry.inserted == 0
    assert await stored(tmp_path) == 1


async def test_incremental_drops_tombstoned_records(tmp_path: Path) -> None:
    """墓碑在增量里出现 —— 那一行要被删掉。"""
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]
    await sync(tmp_path, fake)

    fake.pages = [[tombstone("b")]]
    report = await sync(tmp_path, fake)
    entry = only(report)

    assert entry.deleted == 1
    assert await stored(tmp_path) == 1


async def test_full_sync_reports_what_disappeared(tmp_path: Path) -> None:
    """``--full`` 的对账：服务器上没了的记录，报告里要看得见（不能恒报 0）。"""
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]
    await sync(tmp_path, fake)

    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    report = await sync(tmp_path, fake, full=True)
    entry = only(report)

    assert entry.mode == "full"
    assert entry.deleted == 1
    assert entry.updated == 1
    assert entry.inserted == 0
    assert await stored(tmp_path) == 1


async def test_backoff_does_not_advance_the_cursor(tmp_path: Path) -> None:
    """退避时游标**原地不动** —— 推了它，那段窗口里的变更就永远丢了。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    await sync(tmp_path, fake)

    fake.storage_status = 503
    fake.storage_headers = {"Retry-After": "30"}
    with pytest.raises(BackoffError):
        await sync(tmp_path, fake)

    assert await cursor(tmp_path) == 1789320600.12
    assert await stored(tmp_path) == 1


async def test_full_flag_ignores_the_cursor(tmp_path: Path) -> None:
    """``--full`` 强制回到全量 —— 增量拉久了需要对一次账。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    await sync(tmp_path, fake)

    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]
    report = await sync(tmp_path, fake, full=True)

    assert only(report).mode == "full"
    assert "newer" not in storage_requests(fake, "history")[1].url.params
    assert await stored(tmp_path) == 2


async def test_missing_cursor_falls_back_to_full(tmp_path: Path) -> None:
    """游标没了（比如被手动清了）—— 安全回退到全量，不是报错。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    await sync(tmp_path, fake)

    await open_database(tmp_path / "db.sqlite")
    await SyncCursor.delete().where(SyncCursor.collection == "history")

    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]
    report = await sync(tmp_path, fake)

    assert only(report).mode == "full"
    assert await stored(tmp_path) == 2


async def test_protocol_collections_are_incremental_too(tmp_path: Path) -> None:
    """``crypto`` 也有自己的游标 —— 协议数据不该每次重拉。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    await sync(tmp_path, fake)

    fake.pages = [[bso("b")]]
    await sync(tmp_path, fake)

    assert storage_requests(fake, "crypto")[1].url.params["newer"] == "1.00"
    assert await cursor(tmp_path, "crypto") == 1.0


async def test_default_sync_pulls_the_whole_allowlist(tmp_path: Path) -> None:
    """白名单三件套一次拉全、一次事务写入；协议数据 crypto 只拉一遍、归顶层。"""
    fake = FakeSync()
    fake.counts = {"history": 1, "bookmarks": 1, "tabs": 1}
    fake.pages = [[bso("h")], [bso("b")], [bso("t")]]

    report = await sync(tmp_path, fake, collections=SYNCABLE_COLLECTIONS)

    assert [entry.collection for entry in report.collections] == list(SYNCABLE_COLLECTIONS)
    assert report.protocol == {"crypto": 1}
    assert len(storage_requests(fake, "crypto")) == 1
    for name in SYNCABLE_COLLECTIONS:
        assert await stored(tmp_path, name) == 1
        assert await cursor(tmp_path, name) == 1789320600.12


async def test_a_failing_collection_leaves_everything_untouched(tmp_path: Path) -> None:
    """三件套里最后一件失败 —— 前两件也不许落库，游标一个都不推进。"""
    fake = FakeSync()
    fake.counts = {"history": 1, "bookmarks": 1, "tabs": 5}
    fake.pages = [[bso("h")], [bso("b")], [bso("t")]]

    with pytest.raises(SyncProtocolError):
        await sync(tmp_path, fake, collections=SYNCABLE_COLLECTIONS)

    for name in SYNCABLE_COLLECTIONS:
        assert await stored(tmp_path, name) == 0
        assert await cursor(tmp_path, name) is None
    assert await cursor(tmp_path, "crypto") is None


async def test_full_reaches_every_target_including_crypto(tmp_path: Path) -> None:
    """``--full`` 没有特例：用户三件套与协议数据全部走全量（请求里不带 ``newer``）。"""
    fake = FakeSync()
    fake.counts = {"history": 1, "bookmarks": 1, "tabs": 1}
    fake.pages = [[bso("h")], [bso("b")], [bso("t")]]
    await sync(tmp_path, fake, collections=SYNCABLE_COLLECTIONS)

    fake.pages = [[bso("h")], [bso("b")], [bso("t")]]
    report = await sync(tmp_path, fake, collections=SYNCABLE_COLLECTIONS, full=True)

    assert all(entry.mode == "full" for entry in report.collections)
    for name in (*SYNCABLE_COLLECTIONS, "crypto"):
        assert "newer" not in storage_requests(fake, name)[-1].url.params
