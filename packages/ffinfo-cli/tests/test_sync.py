"""``ffinfo-cli sync``：白名单、落盘、失败不写库。

全部离线：HTTP 走 ``httpx.MockTransport``，凭据写进临时目录。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.errors import BackoffError, ConfigurationError, SyncProtocolError
from ffinfo.keys import OLD_SYNC_SCOPE, ScopedKey
from ffinfo.oauth import Credentials
from ffinfo_cli.store import SyncCursor, SyncRecord, load_cursor, open_database
from ffinfo_cli.sync import SYNCABLE_COLLECTIONS, SyncReport, run_sync

NOW = 1_789_320_612.0

TOKEN_JSON: dict[str, Any] = {
    "id": "eyJub2RlIjoiZmFrZSJ9.SIGNATURE",
    "key": "yLw9efSZimFakeKeyForTests0000000000000000000",
    "uid": 12345,
    "api_endpoint": "https://sync.test/1.5/12345",
    "duration": 3600,
}


def write_credentials(
    tmp_path: Path, *, expires_at: float = NOW + 3600, with_scope: bool = True
) -> tuple[Path, Path]:
    """造一份真的 age 加密凭据 —— 走和 login 一样的路径。"""
    identity = AgeIdentity.generate()
    identity_path = tmp_path / "age-key.txt"
    identity.to_file(identity_path)

    keys: dict[str, ScopedKey] = {}
    if with_scope:
        k = base64.urlsafe_b64encode(bytes(range(64))).decode("ascii").rstrip("=")
        keys[OLD_SYNC_SCOPE] = ScopedKey(kty="EC", scope=OLD_SYNC_SCOPE, k=k, kid="KID-123")

    credentials = Credentials(access_token="ACCESS-TOKEN", expires_at=expires_at, scoped_keys=keys)
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

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
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
    engine = await open_database(tmp_path / "db.sqlite")
    return await load_cursor(engine, collection)


async def sync(
    tmp_path: Path,
    fake: FakeSync,
    collection: str = "history",
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
        collection=collection,
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        clock=lambda: NOW,
        **kwargs,
    )


# ── 白名单：来历不明的 collection 一律不碰 ────────────────────────────────


@pytest.mark.parametrize(
    "collection", ["forms", "passwords", "creditcards", "addresses", "meta", "crypto", "prefs"]
)
async def test_unsafe_collections_are_refused(tmp_path: Path, collection: str) -> None:
    """``forms`` 里有来历不明的遗留记录（可能含密码）—— 一条都不拉。"""
    fake = FakeSync()
    fake.counts = {collection: 42768}

    with pytest.raises(ConfigurationError, match="只拉这几个"):
        await sync(tmp_path, fake, collection=collection)

    assert fake.requests == []  # 连网都不上
    assert not (tmp_path / "db.sqlite").exists()


def test_allowlist_matches_the_documented_scope() -> None:
    """白名单就是设计文档决策 4 那句话：历史 + 书签 + 标签页。"""
    assert sorted(SYNCABLE_COLLECTIONS) == ["bookmarks", "history", "tabs"]
    assert "forms" not in SYNCABLE_COLLECTIONS


# ── 正常路径 ──────────────────────────────────────────────────────────────


async def test_happy_path_stores_records(tmp_path: Path) -> None:
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]

    report = await sync(tmp_path, fake)

    assert report.collection == "history"
    assert report.records == 2
    assert report.pages == 1
    assert report.tombstones == 0
    assert report.server_count == 2
    assert report.database == str(tmp_path / "db.sqlite")

    engine = await open_database(tmp_path / "db.sqlite")
    assert await SyncRecord.count().where(SyncRecord.collection == "history") == 2
    del engine


async def test_protocol_data_is_pulled_alongside(tmp_path: Path) -> None:
    """``crypto/keys`` 是解密要用的 —— 拉 history 时顺带拉下来，不用单独跑一次。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]

    report = await sync(tmp_path, fake)

    assert report.protocol == {"crypto": 1}
    engine = await open_database(tmp_path / "db.sqlite")
    stored = await SyncRecord.select().where(SyncRecord.collection == "crypto")
    assert [row["record_id"] for row in stored] == ["keys"]
    del engine


async def test_tombstones_are_counted_and_dropped(tmp_path: Path) -> None:
    """墓碑不进库（那一行不该存在），但报告里要说清楚服务器报了几条删除。"""
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), {"id": "gone", "modified": 1.0, "payload": None}]]

    report = await sync(tmp_path, fake)

    assert report.records == 1
    assert report.tombstones == 1
    assert report.server_count == 2


async def test_report_json_is_machine_readable(tmp_path: Path) -> None:
    """agent 消费的接口 —— 带格式版本，能被 json 直接吃。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]

    report = await sync(tmp_path, fake)
    payload = json.loads(report.to_json())

    assert payload["format_version"] == 1
    assert payload["collection"] == "history"
    assert set(payload) >= {"records", "pages", "tombstones", "server_count", "database"}


async def test_token_server_request_carries_key_id(tmp_path: Path) -> None:
    """``X-KeyID`` 是从 scoped key 的 kid 来的 —— 实测不带就是 401。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]

    await sync(tmp_path, fake)

    token_request = next(r for r in fake.requests if r.url.path.endswith("/1.0/sync/1.5"))
    assert token_request.headers["X-KeyID"] == "KID-123"


# ── 失败时绝不写库 ────────────────────────────────────────────────────────


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


async def test_expired_credentials_are_refused(tmp_path: Path) -> None:
    """token 过期了就说清楚怎么修，别拿它去撞 401。"""
    fake = FakeSync()
    credentials = write_credentials(tmp_path, expires_at=NOW - 1)

    with pytest.raises(ConfigurationError, match="重新跑一次"):
        await sync(tmp_path, fake, credentials=credentials)

    assert fake.requests == []


async def test_credentials_without_sync_scope_are_refused(tmp_path: Path) -> None:
    """凭据里没有 oldsync 的密钥 —— 拿不到同步数据。"""
    fake = FakeSync()
    credentials = write_credentials(tmp_path, with_scope=False)

    with pytest.raises(ConfigurationError, match="oldsync"):
        await sync(tmp_path, fake, credentials=credentials)

    assert fake.requests == []


# ── 增量同步 ──────────────────────────────────────────────────────────────


async def test_first_sync_is_full(tmp_path: Path) -> None:
    """第一次没有游标 —— 全量，请求里不带 ``newer``。"""
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]

    report = await sync(tmp_path, fake)

    assert report.mode == "full"
    assert report.cursor_before is None
    assert report.cursor_after == 1789320600.12
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

    assert report.mode == "incremental"
    assert report.cursor_before == 1789320600.12
    assert report.inserted == 1
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

    assert report.mode == "incremental"
    assert report.inserted == 0
    assert report.updated == 0
    assert report.deleted == 0
    assert await stored(tmp_path) == 1


async def test_incremental_updates_a_changed_record(tmp_path: Path) -> None:
    """同一条记录变了 —— 覆盖，不是插一条新的。"""
    fake = FakeSync()
    fake.counts = {"history": 1}
    fake.pages = [[bso("a")]]
    await sync(tmp_path, fake)

    fake.pages = [[{"id": "a", "modified": 1789320999.0, "payload": '{"ciphertext":"BBBB"}'}]]
    report = await sync(tmp_path, fake)

    assert report.updated == 1
    assert report.inserted == 0
    assert await stored(tmp_path) == 1


async def test_incremental_drops_tombstoned_records(tmp_path: Path) -> None:
    """墓碑在增量里出现 —— 那一行要被删掉。"""
    fake = FakeSync()
    fake.counts = {"history": 2}
    fake.pages = [[bso("a"), bso("b")]]
    await sync(tmp_path, fake)

    fake.pages = [[tombstone("b")]]
    report = await sync(tmp_path, fake)

    assert report.deleted == 1
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

    assert report.mode == "full"
    assert report.deleted == 1
    assert report.updated == 1
    assert report.inserted == 0
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

    assert report.mode == "full"
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

    assert report.mode == "full"
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
