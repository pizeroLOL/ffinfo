"""``ffinfo-cli list``：解密、过滤、JSON 输出。

全程不联网：库是临时目录里现造的，密文用本库自己的加密方向生成
（加密方向已对着官方向量逐字节验过）。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar, get_args

import pytest
from pydantic import BaseModel

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import ConfigurationError
from ffinfo.keys import OLD_SYNC_SCOPE, ScopedKey
from ffinfo.oauth import Credentials
from ffinfo.storage import EncryptedBso
from ffinfo_cli import list as list_module
from ffinfo_cli.commands import list as list_command
from ffinfo_cli.list import (
    BookmarksReport,
    HistoryReport,
    ListReport,
    TabsReport,
    matches_domain,
    matches_search,
    parse_since,
)
from ffinfo_cli.render import render
from ffinfo_cli.store import (
    CollectionBatch,
    StoredVisit,
    open_database,
)
from support import us_of as micros

KSYNC = bytes(range(64))
"""凭据里那把 kSync —— 与 ``test_sync.py`` 用的是同一把。"""
ROOT = KeyBundle.from_ksync_bytes(KSYNC)
"""根密钥：scoped key 直接切出来的那一对。"""

KEY = KeyBundle(encryption_key=b"e" * 32, hmac_key=b"h" * 32)
"""``crypto/keys`` 里 ``default`` 那一对 —— history 没有覆盖就用它。"""

NOW = 1_789_320_612.0
DAY = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def b64(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def write_credentials(tmp_path: Path) -> tuple[Path, Path]:
    """造一份真的 age 加密凭据。"""
    identity = AgeIdentity.generate()
    identity_path = tmp_path / "age-key.txt"
    identity.to_file(identity_path)

    credentials = Credentials(
        access_token="ACCESS-TOKEN",
        expires_at=NOW + 3600,
        scoped_keys={
            OLD_SYNC_SCOPE: ScopedKey(
                kty="EC",
                scope=OLD_SYNC_SCOPE,
                k=base64.urlsafe_b64encode(KSYNC).decode("ascii").rstrip("="),
                kid="KID-123",
            )
        },
    )
    credentials_path = tmp_path / "credentials.age"
    CredentialStore(identity=identity, path=credentials_path).save(credentials.to_json())
    return identity_path, credentials_path


def keys_record(*, key: KeyBundle = KEY, root: KeyBundle = ROOT) -> EncryptedBso:
    """``crypto/keys`` —— 用根密钥加密的一条记录。"""
    cleartext = json.dumps(
        {
            "id": "keys",
            "collection": "crypto",
            "default": [b64(key.encryption_key), b64(key.hmac_key)],
            "collections": {},
        }
    )
    return EncryptedBso(
        id="keys", modified=1.0, payload=EncryptedPayload.from_cleartext(root, cleartext).to_json()
    )


def history_record(
    record_id: str,
    *,
    url: str = "https://example.com/",
    title: str = "Example",
    visits: list[tuple[datetime, int]] | None = None,
    key: KeyBundle = KEY,
) -> EncryptedBso:
    """一条历史记录 —— 用 history 的密钥加密。"""
    moments = visits if visits is not None else [(DAY, 1)]
    cleartext = json.dumps(
        {
            "id": record_id,
            "histUri": url,
            "title": title,
            "visits": [{"date": micros(when), "type": kind} for when, kind in moments],
        }
    )
    return EncryptedBso(
        id=record_id,
        modified=2.0,
        payload=EncryptedPayload.from_cleartext(key, cleartext).to_json(),
    )


ADDED_MILLIS = int(DAY.timestamp() * 1_000)
"""书签的 ``dateAdded`` 是**毫秒** —— 跟历史（微秒）、标签页（秒）不是一个单位。"""


def bookmark_record(
    record_id: str,
    *,
    parent_id: str | None = "folder",
    title: str = "书签",
    url: str | None = "https://example.com/",
    kind: str = "bookmark",
    date_added: int | None = ADDED_MILLIS,
    key: KeyBundle = KEY,
) -> EncryptedBso:
    """一条书签记录（测试里三个 collection 共用一把 ``KEY``）。"""
    payload: dict[str, object] = {"id": record_id, "type": kind, "title": title}
    if parent_id is not None:
        payload["parentid"] = parent_id
    if url is not None:
        payload["bmkUri"] = url
    if date_added is not None:
        payload["dateAdded"] = date_added
    return EncryptedBso(
        id=record_id,
        modified=2.0,
        payload=EncryptedPayload.from_cleartext(key, json.dumps(payload)).to_json(),
    )


def tabs_record(
    record_id: str,
    *,
    client_name: str = "device",
    entries: list[tuple[str, str, int]] | None = None,
    key: KeyBundle = KEY,
) -> EncryptedBso:
    """一条标签页记录（一个 BSO = 一台设备）。``entries`` 是 ``(标题, URL, lastUsed 秒)``。"""
    tabs = entries if entries is not None else [("标签页", "https://example.com/", 1_700_000_000)]
    cleartext = json.dumps(
        {
            "id": record_id,
            "clientName": client_name,
            "tabs": [
                {"title": title, "urlHistory": [url], "lastUsed": last_used}
                for title, url, last_used in tabs
            ],
        }
    )
    return EncryptedBso(
        id=record_id,
        modified=2.0,
        payload=EncryptedPayload.from_cleartext(key, cleartext).to_json(),
    )


async def build_db(
    tmp_path: Path,
    records: list[EncryptedBso],
    *,
    keys: EncryptedBso | None = None,
    bookmarks: list[EncryptedBso] | None = None,
    tabs: list[EncryptedBso] | None = None,
) -> None:
    """把库造出来：一条 crypto/keys + 若干记录（默认只有 history）。"""
    batches = [
        CollectionBatch(
            collection="crypto",
            records=[keys if keys is not None else keys_record()],
            full=True,
        ),
        CollectionBatch(collection="history", records=records, full=True),
    ]
    if bookmarks is not None:
        batches.append(CollectionBatch(collection="bookmarks", records=bookmarks, full=True))
    if tabs is not None:
        batches.append(CollectionBatch(collection="tabs", records=tabs, full=True))
    store = await open_database(tmp_path / "db.sqlite")
    await store.store_batches(batches)


async def _run(
    entry: Callable[..., Awaitable[ReportT]], tmp_path: Path, **kwargs: object
) -> ReportT:
    identity_path, credentials_path = write_credentials(tmp_path)
    return await entry(
        identity_path=identity_path,
        credentials_path=credentials_path,
        database_path=tmp_path / "db.sqlite",
        clock=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


ReportT = TypeVar("ReportT", HistoryReport, BookmarksReport, TabsReport)


async def run(tmp_path: Path, **kwargs: object) -> HistoryReport:
    """历史查询 —— ``run`` 是历史的那个入口，另两个各有自己的夹具。"""
    return await _run(list_module.run_history, tmp_path, **kwargs)


async def run_bookmarks(tmp_path: Path, **kwargs: object) -> BookmarksReport:
    return await _run(list_module.run_bookmarks, tmp_path, **kwargs)


async def run_tabs(tmp_path: Path, **kwargs: object) -> TabsReport:
    return await _run(list_module.run_tabs, tmp_path, **kwargs)


async def test_lists_visits_newest_first(tmp_path: Path) -> None:
    """最新的排前面 —— 看历史就该从最近看起。"""
    await build_db(
        tmp_path,
        [
            history_record("old", url="https://old.test/", visits=[(DAY, 1)]),
            history_record("new", url="https://new.test/", visits=[(DAY + timedelta(hours=1), 2)]),
        ],
    )

    report = await run(tmp_path)

    assert [item.url for item in report.items] == ["https://new.test/", "https://old.test/"]
    assert report.items[0].visit_type_name == "typed"
    assert report.records == 2
    assert report.visits == 2


async def test_one_record_with_many_visits_becomes_many_rows(tmp_path: Path) -> None:
    """一条记录多次访问 —— 拍平。"""
    await build_db(
        tmp_path,
        [
            history_record(
                "rec",
                visits=[(DAY, 1), (DAY + timedelta(minutes=5), 1), (DAY + timedelta(hours=2), 1)],
            )
        ],
    )

    report = await run(tmp_path)

    assert report.records == 1
    assert report.visits == 3
    assert report.returned == 3


async def test_timestamps_are_utc_iso(tmp_path: Path) -> None:
    await build_db(tmp_path, [history_record("rec", visits=[(DAY, 1)])])

    report = await run(tmp_path)

    assert report.items[0].visited_at == "2026-09-13T12:00:00+00:00"


async def test_output_carries_format_version_and_filters(tmp_path: Path) -> None:
    """给 agent 的接口要能演进 —— 版本号必须在。"""
    await build_db(tmp_path, [history_record("rec")])

    report = await run(tmp_path, domain="example.com")
    payload = json.loads(render(report, machine=True))

    assert "format_version" in payload
    assert payload["filters"]["domain"] == "example.com"
    assert payload["filters"]["domain"] == "example.com"
    assert payload["filters"]["limit"] is None
    assert payload["generated_at"] == "2026-09-13T17:30:12+00:00"


async def test_never_synced_says_so_instead_of_pretending(tmp_path: Path) -> None:
    """没同步过的 collection 没有新鲜度可言 —— 是 null，不是 0、也不是"现在"。"""
    await build_db(tmp_path, [history_record("rec")])

    report = await run(tmp_path)
    payload = json.loads(render(report, machine=True))

    assert report.synced_at is None
    assert report.age_seconds is None
    assert payload["synced_at"] is None
    assert payload["age_seconds"] is None


async def test_freshness_reflects_the_last_successful_sync(tmp_path: Path) -> None:
    """同步过的话，"数据有多陈"要说得出来 —— 差一秒都对不上。"""
    await build_db(tmp_path, [history_record("rec")])
    store = await open_database(tmp_path / "db.sqlite")
    await store.save_cursor(
        "history", last_modified=1_789_320_600.0, synced_at=NOW - 3600, records=1
    )

    report = await run(tmp_path)

    assert report.synced_at == "2026-09-13T16:30:12+00:00"
    assert report.age_seconds == 3600.0


async def test_freshness_counts_only_the_listed_collection(tmp_path: Path) -> None:
    """别拿书签的同步时间给历史充数 —— 各 collection 各论各的。"""
    await build_db(tmp_path, [history_record("rec")])
    store = await open_database(tmp_path / "db.sqlite")
    await store.save_cursor("bookmarks", last_modified=1.0, synced_at=NOW - 60, records=0)

    report = await run(tmp_path)

    assert report.synced_at is None
    assert report.age_seconds is None


async def test_tombstones_are_not_listed(tmp_path: Path) -> None:
    """墓碑（别的设备删掉的）不该出现在浏览历史里，也不算"坏掉"。"""
    await build_db(
        tmp_path,
        [history_record("alive"), EncryptedBso(id="gone", modified=2.0, payload=None)],
    )

    report = await run(tmp_path)

    assert [item.record_id for item in report.items] == ["alive"]
    assert report.skipped == 0


async def test_broken_record_is_skipped_and_counted(tmp_path: Path) -> None:
    """硬要求：一条被篡改，不该让你看不到另外两条。"""
    broken = EncryptedBso(
        id="broken",
        modified=2.0,
        payload=json.dumps({"IV": "AAAA", "hmac": "00" * 32, "ciphertext": "AAAA"}),
    )
    await build_db(
        tmp_path,
        [
            history_record("a", url="https://a.test/"),
            broken,
            history_record("b", url="https://b.test/"),
        ],
    )

    report = await run(tmp_path)

    assert len(report.items) == 2
    assert report.skipped == 1
    assert report.skipped_details[0]["record_id"] == "broken"


async def test_everything_unreadable_means_the_wrong_account(tmp_path: Path) -> None:
    """全部解不开时别装没事 —— 多半是换了账号。"""
    stranger = KeyBundle(encryption_key=b"z" * 32, hmac_key=b"z" * 32)
    await build_db(tmp_path, [history_record("rec", key=stranger)])

    with pytest.raises(ConfigurationError, match="不是同一个账号"):
        await run(tmp_path)


async def test_missing_keys_record_says_what_to_do(tmp_path: Path) -> None:
    """库里没有 crypto/keys —— 得说清楚"先跑 sync"，而不是抛个 KeyError。"""
    store = await open_database(tmp_path / "db.sqlite")
    await store.store_batches(
        [CollectionBatch(collection="history", records=[history_record("rec")], full=True)],
    )

    with pytest.raises(ConfigurationError, match="ffinfo-cli sync"):
        await run(tmp_path)


async def test_keys_encrypted_for_another_account_is_reported(tmp_path: Path) -> None:
    """crypto/keys 本身解不开 —— 同样是"凭据对不上"。"""
    other_root = KeyBundle.from_ksync_bytes(bytes(range(64, 128)))
    await build_db(tmp_path, [history_record("rec")], keys=keys_record(root=other_root))

    with pytest.raises(ConfigurationError, match="crypto/keys 解不开"):
        await run(tmp_path)


async def test_since_filter(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [
            history_record("old", url="https://old.test/", visits=[(DAY, 1)]),
            history_record("new", url="https://new.test/", visits=[(DAY + timedelta(days=2), 1)]),
        ],
    )

    report = await run(tmp_path, since=DAY + timedelta(days=1))

    assert [item.record_id for item in report.items] == ["new"]
    assert report.visits == 2  # 过滤前还是两条
    assert report.matched == 1


async def test_domain_filter_includes_subdomains(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [
            history_record("sub", url="https://docs.example.com/x"),
            history_record("apex", url="https://example.com/"),
            history_record("other", url="https://notexample.com/"),
            history_record("evil", url="https://example.com.evil.test/"),
        ],
    )

    report = await run(tmp_path, domain="example.com")

    assert sorted(item.record_id for item in report.items if item.record_id is not None) == [
        "apex",
        "sub",
    ]


async def test_search_filter_covers_title_and_url_case_insensitively(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [
            history_record("bytitle", url="https://a.test/", title="Rust 学习笔记"),
            history_record("byurl", url="https://rust-lang.org/", title="首页"),
            history_record("none", url="https://b.test/", title="别的"),
        ],
    )

    report = await run(tmp_path, search="RUST")

    assert sorted(item.record_id for item in report.items if item.record_id is not None) == [
        "bytitle",
        "byurl",
    ]


async def test_limit_caps_but_reports_the_full_match(tmp_path: Path) -> None:
    """agent 要能看出"还有更多"。"""
    await build_db(
        tmp_path,
        [history_record(f"rec{i}", visits=[(DAY + timedelta(minutes=i), 1)]) for i in range(5)],
    )

    report = await run(tmp_path, limit=2)

    assert report.returned == 2
    assert report.matched == 5
    assert report.items[0].record_id == "rec4"  # 最新的那条


async def test_filters_combine(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [
            history_record(
                "hit",
                url="https://example.com/a",
                title="keep",
                visits=[(DAY + timedelta(days=1), 1)],
            ),
            history_record(
                "wrongday", url="https://example.com/b", title="keep", visits=[(DAY, 1)]
            ),
            history_record(
                "wrongdomain",
                url="https://other.test/c",
                title="keep",
                visits=[(DAY + timedelta(days=1), 1)],
            ),
        ],
    )

    report = await run(
        tmp_path, domain="example.com", since=DAY + timedelta(hours=6), search="keep"
    )

    assert [item.record_id for item in report.items] == ["hit"]


def test_parse_since_bare_date_is_local_time() -> None:
    """裸日期按本机时区理解 —— 人说的是"自己那天"。"""
    parsed = parse_since("2026-09-13")

    assert parsed.tzinfo is not None
    assert parsed.astimezone().replace(tzinfo=None) == datetime(2026, 9, 13)


def test_parse_since_honours_explicit_offset() -> None:
    parsed = parse_since("2026-09-13T10:00:00+08:00")

    assert parsed == datetime(2026, 9, 13, 2, 0, tzinfo=UTC)


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ConfigurationError, match="看不懂"):
        parse_since("上周三")


@pytest.mark.parametrize(
    ("url", "domain", "expected"),
    [
        ("https://example.com/", "example.com", True),
        ("https://www.example.com/", "example.com", True),
        ("https://EXAMPLE.com/", "example.com", True),
        ("https://example.com/", ".example.com", True),
        ("https://notexample.com/", "example.com", False),
        ("https://example.com.evil.test/", "example.com", False),
        ("https://example.com/", "other.com", False),
    ],
)
def test_matches_domain(url: str, domain: str, expected: bool) -> None:
    assert matches_domain(url, domain) is expected


def test_matches_search_covers_several_fields_case_insensitively() -> None:
    """搜索同时看标题和 URL —— 哪个命中都算。"""
    assert matches_search("Rust 学习笔记", "https://x.test/", needle="RUST")
    assert matches_search("标题", "https://rust-lang.org/", needle="rust")
    assert not matches_search("别的", "https://x.test/", needle="rust")
    assert not matches_search(None, None, needle="rust")


def firefox_visit(
    url: str = "https://example.com/",
    *,
    when: datetime = DAY,
    title: str = "Example",
    machine: str = "test-laptop",
    visit_type: int = 1,
) -> StoredVisit:
    return StoredVisit(
        machine=machine, url=url, title=title, visited_at=when, visit_type=visit_type
    )


async def add_firefox(tmp_path: Path, visits: list[StoredVisit]) -> None:
    """往同一个库里写 firefox 源 —— 分表，与 sync_records 互不干扰。"""
    store = await open_database(tmp_path / "db.sqlite")
    await store.store_firefox_visits(visits)


async def test_firefox_source_alone_is_usable(tmp_path: Path) -> None:
    """目标机器上只有 import 进来的 firefox 数据、云端那条记录也没解出访问 —— 照样能查。"""
    await build_db(tmp_path, [])
    await add_firefox(tmp_path, [firefox_visit("https://firefox.test/")])

    report = await run(tmp_path)

    assert [item.url for item in report.items] == ["https://firefox.test/"]
    assert report.items[0].source == "firefox"
    assert report.items[0].record_id is None


async def test_firefox_source_alone_needs_no_credentials(tmp_path: Path) -> None:
    """只导入过 firefox 数据、从没 login 的机器 —— 不该被 age 私钥挡住。

    firefox 源本来就是明文，云端那条解密链一次都不该走。
    """
    await build_db(tmp_path, [])
    await add_firefox(tmp_path, [firefox_visit("https://firefox.test/")])

    report = await list_module.run_history(
        identity_path=tmp_path / "no-such-age-key.txt",
        credentials_path=tmp_path / "no-such-credentials.age",
        database_path=tmp_path / "db.sqlite",
        clock=lambda: NOW,
    )

    assert [item.url for item in report.items] == ["https://firefox.test/"]
    assert report.items[0].source == "firefox"
    assert report.sources == ["firefox"]


async def test_cloud_records_without_credentials_still_fail(tmp_path: Path) -> None:
    """库里有云端记录、却没凭据文件 —— 必须报配置错误，不能静默当空库。"""
    await build_db(tmp_path, [history_record("rec")])

    with pytest.raises(ConfigurationError):
        await list_module.run_history(
            identity_path=tmp_path / "no-such-age-key.txt",
            credentials_path=tmp_path / "no-such-credentials.age",
            database_path=tmp_path / "db.sqlite",
            clock=lambda: NOW,
        )


async def test_empty_database_without_credentials_is_an_empty_report(tmp_path: Path) -> None:
    """空库 + 从没登录：history 返回空报告，不报配置错误。

    没有云端记录就没有密文要解 —— 凭据不该挡路；"从没登录"靠 ``synced_at: null`` 表达。
    """
    report = await list_module.run_history(
        identity_path=tmp_path / "no-such-age-key.txt",
        credentials_path=tmp_path / "no-such-credentials.age",
        database_path=tmp_path / "db.sqlite",
        clock=lambda: NOW,
    )

    assert report.items == []
    assert report.records == 0
    assert report.sources == []
    assert report.synced_at is None


async def test_sync_source_alone_still_works(tmp_path: Path) -> None:
    """**降级到单源**：一台没导入过任何 firefox 数据的机器，查询照常。"""
    await build_db(tmp_path, [history_record("rec", url="https://cloud.test/")])

    report = await run(tmp_path)

    assert [item.url for item in report.items] == ["https://cloud.test/"]
    assert report.items[0].source == "sync"
    assert report.items[0].source_machine is None
    assert report.sources == ["sync"]
    assert report.firefox_records == 0


async def test_the_same_visit_from_both_sources_is_one_row(tmp_path: Path) -> None:
    """**这张票的核心**：同一次访问两个源都有 —— 只出一行，标成 both。

    能对上的前提是两边的微秒**完全相等**：云端那条走 ``date / 1e6`` 的浮点换算，
    firefox 这条走整数换算。这条测试就是那个不变量的看门人。
    """
    await build_db(tmp_path, [history_record("rec", url="https://both.test/", visits=[(DAY, 1)])])
    await add_firefox(tmp_path, [firefox_visit("https://both.test/", when=DAY)])

    report = await run(tmp_path)

    assert report.returned == 1
    assert report.items[0].source == "both"
    assert report.items[0].record_id == "rec"
    assert report.items[0].source_machine == "test-laptop"


async def test_a_different_visit_at_the_same_url_is_a_second_row(tmp_path: Path) -> None:
    """同一个 URL、**不同时刻**是两次访问 —— 不能合并掉。"""
    await build_db(tmp_path, [history_record("rec", url="https://both.test/", visits=[(DAY, 1)])])
    await add_firefox(
        tmp_path, [firefox_visit("https://both.test/", when=DAY + timedelta(minutes=30))]
    )

    report = await run(tmp_path)

    assert report.returned == 2
    assert [item.source for item in report.items] == ["firefox", "sync"]


async def test_merged_rows_stay_newest_first(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [
            history_record("a", url="https://a.test/", visits=[(DAY, 1)]),
            history_record("b", url="https://b.test/", visits=[(DAY + timedelta(hours=2), 1)]),
        ],
    )
    await add_firefox(
        tmp_path,
        [
            firefox_visit("https://a.test/", when=DAY),
            firefox_visit("https://c.test/", when=DAY + timedelta(hours=1)),
        ],
    )

    report = await run(tmp_path)

    assert [item.url for item in report.items] == [
        "https://b.test/",
        "https://c.test/",
        "https://a.test/",
    ]
    assert [item.source for item in report.items] == ["sync", "firefox", "both"]


async def test_sources_field_says_what_contributed(tmp_path: Path) -> None:
    await build_db(tmp_path, [history_record("rec")])
    await add_firefox(tmp_path, [firefox_visit()])

    report = await run(tmp_path)

    assert report.sources == ["sync", "firefox"]
    assert report.firefox_records == 1
    assert report.records == 1


async def test_firefox_visits_go_through_the_same_filters(tmp_path: Path) -> None:
    """过滤器对两个源一视同仁 —— 不然"合并"就成了半成品。"""
    await build_db(tmp_path, [])
    await add_firefox(
        tmp_path,
        [
            firefox_visit("https://wanted.test/page", when=DAY, title="Wanted"),
            firefox_visit("https://other.test/", when=DAY),
            firefox_visit("https://wanted.test/old", when=DAY - timedelta(days=30)),
        ],
    )

    report = await run(tmp_path, domain="wanted.test", since=DAY - timedelta(days=1))

    assert [item.url for item in report.items] == ["https://wanted.test/page"]


async def test_firefox_visit_borrows_the_sync_title_when_it_has_none(tmp_path: Path) -> None:
    """firefox 那条没标题、云端那条有 —— 合并后别把标题丢了。"""
    await build_db(
        tmp_path,
        [history_record("rec", url="https://both.test/", title="云端标题", visits=[(DAY, 1)])],
    )
    await add_firefox(tmp_path, [firefox_visit("https://both.test/", when=DAY, title="")])

    report = await run(tmp_path)

    assert report.items[0].title == "云端标题"


async def test_format_version_bumped_for_the_new_shape(tmp_path: Path) -> None:
    """形状变过四次：``source`` / ``source_machine``（2）、数据新鲜度（3）、
    ``source`` 取值与 ``firefox_records`` 改名（4）、报告拆成三份互不继承的类型（5）。"""
    await build_db(tmp_path, [history_record("rec")])

    report = await run(tmp_path)

    assert report.format_version == 5


async def test_json_shape_of_a_merged_row(tmp_path: Path) -> None:
    await build_db(tmp_path, [history_record("rec", url="https://both.test/", visits=[(DAY, 1)])])
    await add_firefox(tmp_path, [firefox_visit("https://both.test/", when=DAY)])

    report = await run(tmp_path)
    payload = json.loads(render(report, machine=True))

    assert payload["sources"] == ["sync", "firefox"]
    assert payload["firefox_records"] == 1
    assert payload["items"][0] == {
        "url": "https://both.test/",
        "title": "Example",
        "visited_at": "2026-09-13T12:00:00+00:00",
        "visit_type": 1,
        "visit_type_name": "link",
        "record_id": "rec",
        "source": "both",
        "source_machine": "test-laptop",
    }


async def test_bookmarks_keep_the_tree(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("folder", kind="folder", parent_id=None, title="工具", url=None),
            bookmark_record("bmk", parent_id="folder", title="示例", url="https://example.com/"),
        ],
    )

    report = await run_bookmarks(tmp_path)

    assert [node.id for node in report.tree] == ["folder"]
    assert report.tree[0].children[0].id == "bmk"
    assert report.returned == 1
    assert report.matched == 1
    assert report.counts == {"folder": 1, "bookmark": 1}


async def test_bookmark_limit_counts_bookmarks_not_folders(tmp_path: Path) -> None:
    """``--limit 1`` 不能再出现 returned=1 / tree=[] 这种自相矛盾。"""
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("folder", kind="folder", parent_id=None, title="工具", url=None),
            bookmark_record("bmk", parent_id="folder", title="示例"),
        ],
    )

    report = await run_bookmarks(tmp_path, limit=1)

    assert report.returned == 1
    assert [node.id for node in report.tree] == ["folder"]
    assert report.tree[0].children[0].id == "bmk"  # 文件夹是挂书签的结构，不占名额
    assert report.counts == {"folder": 1, "bookmark": 1}


async def test_bookmark_limit_uses_the_budget_on_bookmarks(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("folder", kind="folder", parent_id=None, title="工具", url=None),
            bookmark_record("a", parent_id="folder", title="A", url="https://a.test/"),
            bookmark_record("b", parent_id="folder", title="B", url="https://b.test/"),
        ],
    )

    report = await run_bookmarks(tmp_path, limit=2)

    assert report.returned == 2
    assert report.matched == 2
    assert report.counts == {"folder": 1, "bookmark": 2}


async def test_bookmark_cycles_reach_the_report_as_skipped(tmp_path: Path) -> None:
    """建树时丢掉的病态记录（环）也走 ``skipped`` —— 不静默。"""
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("a", kind="folder", parent_id="b", title="A", url=None),
            bookmark_record("b", kind="folder", parent_id="a", title="B", url=None),
        ],
    )

    report = await run_bookmarks(tmp_path)

    assert report.tree == []
    assert report.skipped == 2


async def test_deep_bookmark_trees_do_not_blow_the_stack(tmp_path: Path) -> None:
    """病态深树：剪枝 / 拍平也是迭代版，不炸栈（库那边 ``_walk`` 已有同一条防线）。"""
    depth = 1_200
    records = [bookmark_record("n0", parent_id=None, kind="folder", title="根", url=None)]
    records += [
        bookmark_record(f"n{i}", parent_id=f"n{i - 1}", kind="folder", title=f"n{i}", url=None)
        for i in range(1, depth)
    ]
    records.append(bookmark_record("leaf", parent_id=f"n{depth - 1}", title="底"))
    await build_db(tmp_path, [], bookmarks=records)

    report = await run_bookmarks(tmp_path)

    assert report.counts == {"folder": depth, "bookmark": 1}
    assert report.returned == 1


async def test_bookmark_json_is_serializable_with_iso_times(tmp_path: Path) -> None:
    """时间字段在模型里是 ``datetime``，出去必须是 ISO 字符串 —— JSON 里形状不变。"""
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("folder", kind="folder", parent_id=None, title="工具", url=None),
            bookmark_record("bmk", parent_id="folder", title="示例"),
        ],
    )

    report = await run_bookmarks(tmp_path)
    payload = json.loads(render(report, machine=True))

    assert payload["tree"][0]["children"][0]["added_at"] == "2026-09-13T12:00:00+00:00"


async def _build_nested_bookmarks(tmp_path: Path) -> None:
    """一棵多层书签树 —— ``--path`` 的祖先链 / 重新生根全靠它。

    ``书签工具栏`` ─┬─ ``工具`` ─┬─ A（书签）
                     │           └─ ``子`` ── B（书签）
                     └─ X（书签）
    ``其他书签``   ─── ``工具`` ─── C（书签）
    """
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("toolbar", kind="folder", parent_id=None, title="书签工具栏", url=None),
            bookmark_record("tools", kind="folder", parent_id="toolbar", title="工具", url=None),
            bookmark_record("a", parent_id="tools", title="A", url="https://a.test/"),
            bookmark_record("nested", kind="folder", parent_id="tools", title="子", url=None),
            bookmark_record("b", parent_id="nested", title="B", url="https://b.test/"),
            bookmark_record("x", parent_id="toolbar", title="X", url="https://x.test/"),
            bookmark_record("menu", kind="folder", parent_id=None, title="其他书签", url=None),
            bookmark_record("tools2", kind="folder", parent_id="menu", title="工具", url=None),
            bookmark_record("c", parent_id="tools2", title="C", url="https://c.test/"),
        ],
    )


async def test_bookmark_path_reroots_at_the_matched_folder(tmp_path: Path) -> None:
    """命中的文件夹当根，祖先一律剪掉 —— 上下文由 ``filters.path`` 表达。"""
    await _build_nested_bookmarks(tmp_path)

    report = await run_bookmarks(tmp_path, path="书签工具栏/工具")

    assert [node.id for node in report.tree] == ["tools"]
    assert report.tree[0].children[0].id == "a"
    assert report.tree[0].children[1].id == "nested"
    assert report.tree[0].children[1].children[0].id == "b"
    assert report.returned == 2
    assert report.matched == 2
    assert report.counts == {"folder": 2, "bookmark": 2}
    assert report.notes == []


async def test_bookmark_path_can_match_a_root(tmp_path: Path) -> None:
    """根名本地化 —— 路径可以是「书签工具栏」本身。"""
    await _build_nested_bookmarks(tmp_path)

    report = await run_bookmarks(tmp_path, path="书签工具栏")

    assert [node.id for node in report.tree] == ["toolbar"]
    assert report.returned == 3


async def test_bookmark_path_ignores_leading_and_trailing_slashes(tmp_path: Path) -> None:
    await _build_nested_bookmarks(tmp_path)

    report = await run_bookmarks(tmp_path, path="/书签工具栏/工具/")

    assert [node.id for node in report.tree] == ["tools"]


async def test_bookmark_path_returns_every_folder_with_that_name(tmp_path: Path) -> None:
    """同名文件夹命中多个都返回 —— 两个兄弟都叫「工具」。"""
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("root", kind="folder", parent_id=None, title="根", url=None),
            bookmark_record("t1", kind="folder", parent_id="root", title="工具", url=None),
            bookmark_record("t2", kind="folder", parent_id="root", title="工具", url=None),
            bookmark_record("a", parent_id="t1", title="A", url="https://a.test/"),
            bookmark_record("b", parent_id="t2", title="B", url="https://b.test/"),
        ],
    )

    report = await run_bookmarks(tmp_path, path="根/工具")

    assert [node.id for node in report.tree] == ["t1", "t2"]
    assert report.returned == 2


async def test_bookmark_path_is_exact_and_case_sensitive(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("root", kind="folder", parent_id=None, title="根", url=None),
            bookmark_record("tools", kind="folder", parent_id="root", title="Tools", url=None),
            bookmark_record("a", parent_id="tools", title="A", url="https://a.test/"),
        ],
    )

    assert (await run_bookmarks(tmp_path, path="根/tools")).tree == []
    assert (await run_bookmarks(tmp_path, path="根/Tool")).tree == []
    assert [node.id for node in (await run_bookmarks(tmp_path, path="根/Tools")).tree] == ["tools"]


async def test_bookmark_path_no_match_is_empty_with_a_note(tmp_path: Path) -> None:
    """没有这个文件夹是数据事实、不是用法错误 —— 空结果 + ``notes``，退出码照旧。"""
    await _build_nested_bookmarks(tmp_path)

    report = await run_bookmarks(tmp_path, path="不存在")

    assert report.tree == []
    assert report.matched == 0
    assert report.returned == 0
    assert len(report.notes) == 1
    assert "不存在" in report.notes[0]


async def test_bookmark_path_limit_still_counts_only_bookmarks(tmp_path: Path) -> None:
    await _build_nested_bookmarks(tmp_path)

    report = await run_bookmarks(tmp_path, path="书签工具栏/工具", limit=1)

    assert report.returned == 1
    assert report.matched == 2


async def test_tabs_are_grouped_by_client(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record("dev1", client_name="alpha", entries=[("一", "https://a.test/", 1)]),
            tabs_record("dev2", client_name="beta", entries=[("二", "https://b.test/", 2)]),
        ],
    )

    report = await run_tabs(tmp_path)

    assert [client.client_name for client in report.clients] == ["alpha", "beta"]
    assert report.returned == 2
    assert report.matched == 2


async def test_tabs_limit_counts_tabs_not_clients(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record(
                "dev1",
                client_name="alpha",
                entries=[("一", "https://a.test/", 1), ("二", "https://b.test/", 2)],
            ),
            tabs_record("dev2", client_name="beta", entries=[("三", "https://c.test/", 3)]),
        ],
    )

    report = await run_tabs(tmp_path, limit=2)

    assert report.matched == 3
    assert report.returned == 2
    assert [client.client_name for client in report.clients] == ["alpha"]
    assert [tab.title for tab in report.clients[0].tabs] == ["一", "二"]


async def test_tabs_device_matches_client_name_case_insensitively(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record("dev1", client_name="Alpha", entries=[("一", "https://a.test/", 1)]),
            tabs_record("dev2", client_name="beta", entries=[("二", "https://b.test/", 2)]),
        ],
    )

    report = await run_tabs(tmp_path, device="alpha")

    assert [client.client_name for client in report.clients] == ["Alpha"]


async def test_tabs_device_matches_client_id(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record("dev1", client_name="Alpha", entries=[("一", "https://a.test/", 1)]),
            tabs_record("dev2", client_name="beta", entries=[("二", "https://b.test/", 2)]),
        ],
    )

    report = await run_tabs(tmp_path, device="dev2")

    assert [client.client_id for client in report.clients] == ["dev2"]


async def test_tabs_device_is_not_a_substring_match(tmp_path: Path) -> None:
    """不做子串 —— 一个子串命中多台会让「筛的是哪台」变含糊。"""
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record("dev1", client_name="alpha", entries=[("一", "https://a.test/", 1)]),
        ],
    )

    report = await run_tabs(tmp_path, device="alp")

    assert report.clients == []


async def test_tabs_device_no_match_is_empty_with_a_note(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record("dev1", client_name="alpha", entries=[("一", "https://a.test/", 1)]),
        ],
    )

    report = await run_tabs(tmp_path, device="nope")

    assert report.clients == []
    assert report.matched == 0
    assert report.returned == 0
    assert len(report.notes) == 1
    assert "nope" in report.notes[0]


async def test_tabs_device_limit_counts_tabs_not_clients(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record(
                "dev1",
                client_name="alpha",
                entries=[("一", "https://a.test/", 1), ("二", "https://b.test/", 2)],
            ),
            tabs_record("dev2", client_name="beta", entries=[("三", "https://c.test/", 3)]),
        ],
    )

    report = await run_tabs(tmp_path, device="alpha", limit=1)

    assert report.matched == 2
    assert report.returned == 1
    assert [client.client_name for client in report.clients] == ["alpha"]


async def test_tabs_json_is_serializable_with_iso_times(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record(
                "dev", client_name="alpha", entries=[("一", "https://a.test/", 1_700_000_000)]
            )
        ],
    )

    report = await run_tabs(tmp_path)
    payload = json.loads(render(report, machine=True))

    assert payload["clients"][0]["tabs"][0]["last_used_at"] == "2023-11-14T22:13:20+00:00"


V4_JSON_KEYS: dict[str, set[str]] = {
    "history": {
        "format_version",
        "data_type",
        "generated_at",
        "filters",
        "records",
        "synced_at",
        "age_seconds",
        "visits",
        "firefox_records",
        "sources",
        "skipped",
        "skipped_details",
        "notes",
        "matched",
        "returned",
        "items",
    },
    "bookmarks": {
        "format_version",
        "data_type",
        "generated_at",
        "filters",
        "records",
        "synced_at",
        "age_seconds",
        "firefox_records",
        "sources",
        "skipped",
        "skipped_details",
        "notes",
        "matched",
        "returned",
        "tree",
        "counts",
    },
    "tabs": {
        "format_version",
        "data_type",
        "generated_at",
        "filters",
        "records",
        "synced_at",
        "age_seconds",
        "firefox_records",
        "sources",
        "skipped",
        "skipped_details",
        "notes",
        "matched",
        "returned",
        "clients",
    },
}
"""v4 的 JSON 键集合 —— 拆模型只是让每份报告**只声明自己的字段**，形状逐字段不变。"""


def test_the_three_reports_have_no_common_base() -> None:
    """三份报告互不继承 —— 共享的是形状，用 union 表达。"""
    assert HistoryReport.__bases__ == (BaseModel,)
    assert BookmarksReport.__bases__ == (BaseModel,)
    assert TabsReport.__bases__ == (BaseModel,)
    assert set(get_args(ListReport.__value__)) == {HistoryReport, BookmarksReport, TabsReport}


@pytest.mark.parametrize("data_type", ["history", "bookmarks", "tabs"])
async def test_json_key_set_matches_v4(tmp_path: Path, data_type: str) -> None:
    """拆成三份模型后 JSON 仍是 v4 的扁平形状，只动 ``format_version``。"""
    if data_type == "history":
        await build_db(tmp_path, [history_record("rec")])
        report = await run(tmp_path)
    elif data_type == "bookmarks":
        await build_db(
            tmp_path,
            [],
            bookmarks=[bookmark_record("bmk", parent_id=None, title="示例")],
        )
        report = await run_bookmarks(tmp_path)
    else:
        await build_db(tmp_path, [], tabs=[tabs_record("dev")])
        report = await run_tabs(tmp_path)

    payload = json.loads(render(report, machine=True))

    assert set(payload) == V4_JSON_KEYS[data_type]
    assert payload["format_version"] == 5


def test_reports_carry_no_to_json_method() -> None:
    """序列化只在 ``render.py``，不挂在报告模型上。"""
    report = HistoryReport(
        data_type="history",
        generated_at="2026-09-13T17:30:12+00:00",
        filters={},
        records=0,
        skipped=0,
        matched=0,
        returned=0,
    )

    assert json.loads(render(report, machine=True))["format_version"] == 5
    assert not hasattr(report, "to_json")


def test_model_fields_and_notes_defaults() -> None:
    """直接构造三份模型 —— 共有字段齐全，``notes`` / ``skipped_details`` 默认空。"""
    history = HistoryReport(
        data_type="history",
        generated_at="2026-09-13T17:30:12+00:00",
        filters={},
        records=0,
        skipped=0,
        matched=0,
        returned=0,
    )
    bookmarks = BookmarksReport(
        data_type="bookmarks",
        generated_at="2026-09-13T17:30:12+00:00",
        filters={},
        records=0,
        skipped=0,
        matched=0,
        returned=0,
    )
    tabs = TabsReport(
        data_type="tabs",
        generated_at="2026-09-13T17:30:12+00:00",
        filters={},
        records=0,
        skipped=0,
        matched=0,
        returned=0,
    )

    for report in (history, bookmarks, tabs):
        assert report.format_version == 5
        assert report.synced_at is None
        assert report.age_seconds is None
        assert report.notes == []
        assert report.skipped_details == []
        assert report.sources == []
        assert report.firefox_records == 0


# --- 数据感知补全：回调给定 incomplete 返回候选，读不到就静默空列表 ---


def point_completion_at(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: Path,
    identity: Path,
    credentials: Path,
) -> None:
    """把 ``commands.list`` 的补全回调指向这个测试的库与凭据。"""
    monkeypatch.setattr("ffinfo_cli.commands.list.database_path", lambda: database)
    monkeypatch.setattr("ffinfo_cli.commands.list.identity_path", lambda: identity)
    monkeypatch.setattr("ffinfo_cli.commands.list.credentials_path", lambda: credentials)


def broken_record(record_id: str) -> EncryptedBso:
    """一条解不开的记录 —— 用来验"坏记录不连坐、整体静默"。"""
    return EncryptedBso(
        id=record_id,
        modified=2.0,
        payload=json.dumps({"IV": "AAAA", "hmac": "00" * 32, "ciphertext": "AAAA"}),
    )


def test_device_completion_lists_names_and_client_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list tabs --device <TAB>``：库里的设备名与 ``clientId`` 都出。"""
    asyncio.run(
        build_db(
            tmp_path,
            [],
            tabs=[
                tabs_record("dev1", client_name="alpha"),
                tabs_record("dev2", client_name="beta"),
            ],
        )
    )
    identity, credentials = write_credentials(tmp_path)
    point_completion_at(
        monkeypatch,
        database=tmp_path / "db.sqlite",
        identity=identity,
        credentials=credentials,
    )

    assert list_command.complete_device("") == ["alpha", "beta", "dev1", "dev2"]
    assert list_command.complete_device("bet") == ["beta"]


def test_bookmark_path_completion_lists_folder_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list bookmarks --path <TAB>``：出**带祖先**的文件夹路径，不是裸标题。"""
    asyncio.run(_build_nested_bookmarks(tmp_path))
    identity, credentials = write_credentials(tmp_path)
    point_completion_at(
        monkeypatch,
        database=tmp_path / "db.sqlite",
        identity=identity,
        credentials=credentials,
    )

    candidates = list_command.complete_bookmark_path("")

    assert "书签工具栏" in candidates
    assert "书签工具栏/工具" in candidates
    assert "书签工具栏/工具/子" in candidates
    assert "其他书签/工具" in candidates
    assert "工具" not in candidates
    assert list_command.complete_bookmark_path("书签工具栏/工") == [
        "书签工具栏/工具",
        "书签工具栏/工具/子",
    ]


def test_completion_without_database_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """库不存在 —— 空候选，不写 stderr。"""
    identity, credentials = write_credentials(tmp_path)
    point_completion_at(
        monkeypatch,
        database=tmp_path / "missing.sqlite",
        identity=identity,
        credentials=credentials,
    )

    assert list_command.complete_device("") == []
    assert list_command.complete_bookmark_path("") == []
    assert capsys.readouterr().err == ""


def test_completion_without_credentials_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """库里有记录、却从没登录 —— 空候选，不写 stderr。"""
    asyncio.run(build_db(tmp_path, [], tabs=[tabs_record("dev1", client_name="alpha")]))
    point_completion_at(
        monkeypatch,
        database=tmp_path / "db.sqlite",
        identity=tmp_path / "no-age-key.txt",
        credentials=tmp_path / "no-credentials.age",
    )

    assert list_command.complete_device("") == []
    assert capsys.readouterr().err == ""


def test_completion_skips_broken_records_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """坏记录不连坐：好设备的候选照出，坏的那条只是不出现，且不写 stderr。"""
    asyncio.run(
        build_db(
            tmp_path,
            [],
            tabs=[tabs_record("dev1", client_name="alpha"), broken_record("broken")],
        )
    )
    identity, credentials = write_credentials(tmp_path)
    point_completion_at(
        monkeypatch,
        database=tmp_path / "db.sqlite",
        identity=identity,
        credentials=credentials,
    )

    assert list_command.complete_device("") == ["alpha", "dev1"]
    assert capsys.readouterr().err == ""


def test_completion_with_only_broken_records_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """一条都解不开也静默 —— 空候选，不抛。"""
    asyncio.run(build_db(tmp_path, [], tabs=[broken_record("broken")]))
    identity, credentials = write_credentials(tmp_path)
    point_completion_at(
        monkeypatch,
        database=tmp_path / "db.sqlite",
        identity=identity,
        credentials=credentials,
    )

    assert list_command.complete_device("") == []
    assert capsys.readouterr().err == ""
