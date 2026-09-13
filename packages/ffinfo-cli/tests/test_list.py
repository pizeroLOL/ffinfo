"""``ffinfo-cli list``：解密、过滤、JSON 输出。

全程不联网：库是临时目录里现造的，密文用本库自己的加密方向生成
（加密方向已对着官方向量逐字节验过）。
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import ConfigurationError
from ffinfo.keys import OLD_SYNC_SCOPE, ScopedKey
from ffinfo.oauth import Credentials
from ffinfo.storage import EncryptedBso
from ffinfo_cli.list import ListReport, matches_domain, matches_search, parse_since, run_list
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


async def run(tmp_path: Path, **kwargs: object) -> ListReport:
    identity_path, credentials_path = write_credentials(tmp_path)
    return await run_list(
        identity_path=identity_path,
        credentials_path=credentials_path,
        database_path=tmp_path / "db.sqlite",
        clock=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


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
    payload = json.loads(report.to_json())

    assert "format_version" in payload
    assert payload["filters"]["domain"] == "example.com"
    assert payload["filters"]["domain"] == "example.com"
    assert payload["filters"]["limit"] is None
    assert payload["generated_at"] == "2026-09-13T17:30:12+00:00"


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


def local_visit(
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


async def add_local(tmp_path: Path, visits: list[StoredVisit]) -> None:
    """往同一个库里写本地源 —— 分表，与 sync_records 互不干扰。"""
    store = await open_database(tmp_path / "db.sqlite")
    await store.store_local_visits(visits)


async def test_local_source_alone_is_usable(tmp_path: Path) -> None:
    """目标机器上只有 import 进来的本地数据、云端那条记录也没解出访问 —— 照样能查。"""
    await build_db(tmp_path, [])
    await add_local(tmp_path, [local_visit("https://local.test/")])

    report = await run(tmp_path)

    assert [item.url for item in report.items] == ["https://local.test/"]
    assert report.items[0].source == "local"
    assert report.items[0].record_id is None


async def test_sync_source_alone_still_works(tmp_path: Path) -> None:
    """**降级到单源**：一台没导入过任何本地数据的机器，查询照常。"""
    await build_db(tmp_path, [history_record("rec", url="https://cloud.test/")])

    report = await run(tmp_path)

    assert [item.url for item in report.items] == ["https://cloud.test/"]
    assert report.items[0].source == "sync"
    assert report.items[0].source_machine is None
    assert report.sources == ["sync"]
    assert report.local_records == 0


async def test_the_same_visit_from_both_sources_is_one_row(tmp_path: Path) -> None:
    """**这张票的核心**：同一次访问两个源都有 —— 只出一行，标成 both。

    能对上的前提是两边的微秒**完全相等**：云端那条走 ``date / 1e6`` 的浮点换算，
    本地这条走整数换算。这条测试就是那个不变量的看门人。
    """
    await build_db(tmp_path, [history_record("rec", url="https://both.test/", visits=[(DAY, 1)])])
    await add_local(tmp_path, [local_visit("https://both.test/", when=DAY)])

    report = await run(tmp_path)

    assert report.returned == 1
    assert report.items[0].source == "both"
    assert report.items[0].record_id == "rec"
    assert report.items[0].source_machine == "test-laptop"


async def test_a_different_visit_at_the_same_url_is_a_second_row(tmp_path: Path) -> None:
    """同一个 URL、**不同时刻**是两次访问 —— 不能合并掉。"""
    await build_db(tmp_path, [history_record("rec", url="https://both.test/", visits=[(DAY, 1)])])
    await add_local(tmp_path, [local_visit("https://both.test/", when=DAY + timedelta(minutes=30))])

    report = await run(tmp_path)

    assert report.returned == 2
    assert [item.source for item in report.items] == ["local", "sync"]


async def test_merged_rows_stay_newest_first(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [
            history_record("a", url="https://a.test/", visits=[(DAY, 1)]),
            history_record("b", url="https://b.test/", visits=[(DAY + timedelta(hours=2), 1)]),
        ],
    )
    await add_local(
        tmp_path,
        [
            local_visit("https://a.test/", when=DAY),
            local_visit("https://c.test/", when=DAY + timedelta(hours=1)),
        ],
    )

    report = await run(tmp_path)

    assert [item.url for item in report.items] == [
        "https://b.test/",
        "https://c.test/",
        "https://a.test/",
    ]
    assert [item.source for item in report.items] == ["sync", "local", "both"]


async def test_sources_field_says_what_contributed(tmp_path: Path) -> None:
    await build_db(tmp_path, [history_record("rec")])
    await add_local(tmp_path, [local_visit()])

    report = await run(tmp_path)

    assert report.sources == ["sync", "local"]
    assert report.local_records == 1
    assert report.records == 1


async def test_local_visits_go_through_the_same_filters(tmp_path: Path) -> None:
    """过滤器对两个源一视同仁 —— 不然"合并"就成了半成品。"""
    await build_db(tmp_path, [])
    await add_local(
        tmp_path,
        [
            local_visit("https://wanted.test/page", when=DAY, title="Wanted"),
            local_visit("https://other.test/", when=DAY),
            local_visit("https://wanted.test/old", when=DAY - timedelta(days=30)),
        ],
    )

    report = await run(tmp_path, domain="wanted.test", since=DAY - timedelta(days=1))

    assert [item.url for item in report.items] == ["https://wanted.test/page"]


async def test_local_visit_borrows_the_sync_title_when_it_has_none(tmp_path: Path) -> None:
    """本地那条没标题、云端那条有 —— 合并后别把标题丢了。"""
    await build_db(
        tmp_path,
        [history_record("rec", url="https://both.test/", title="云端标题", visits=[(DAY, 1)])],
    )
    await add_local(tmp_path, [local_visit("https://both.test/", when=DAY, title="")])

    report = await run(tmp_path)

    assert report.items[0].title == "云端标题"


async def test_format_version_bumped_for_the_new_shape(tmp_path: Path) -> None:
    """输出多了 source / source_machine、record_id 也可能为 null —— 形状变了就得报。"""
    await build_db(tmp_path, [history_record("rec")])

    report = await run(tmp_path)

    assert report.format_version == 2


async def test_json_shape_of_a_merged_row(tmp_path: Path) -> None:
    await build_db(tmp_path, [history_record("rec", url="https://both.test/", visits=[(DAY, 1)])])
    await add_local(tmp_path, [local_visit("https://both.test/", when=DAY)])

    report = await run(tmp_path)
    payload = json.loads(report.to_json())

    assert payload["sources"] == ["sync", "local"]
    assert payload["local_records"] == 1
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

    report = await run(tmp_path, data_type="bookmarks")

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

    report = await run(tmp_path, data_type="bookmarks", limit=1)

    assert report.returned == 1
    assert [node.id for node in report.tree] == ["folder"]
    assert report.tree[0].children[0].id == "bmk"  # 文件夹是挂书签的结构，不占名额
    assert report.counts == {"folder": 1, "bookmark": 1}


async def test_bookmark_filters_prune_empty_folders(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record("tools", kind="folder", parent_id=None, title="工具", url=None),
            bookmark_record(
                "keep", parent_id="tools", title="Rust 笔记", url="https://rust-lang.org/"
            ),
            bookmark_record("drop", parent_id="tools", title="别家", url="https://other.test/"),
            bookmark_record("empty", kind="folder", parent_id=None, title="空文件夹", url=None),
        ],
    )

    report = await run(tmp_path, data_type="bookmarks", domain="rust-lang.org")

    assert report.returned == 1
    assert report.matched == 1
    assert [node.id for node in report.tree] == ["tools"]
    assert report.tree[0].children[0].id == "keep"
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

    report = await run(tmp_path, data_type="bookmarks", limit=2)

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

    report = await run(tmp_path, data_type="bookmarks")

    assert report.tree == []
    assert report.skipped == 2


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

    report = await run(tmp_path, data_type="bookmarks")
    payload = json.loads(report.to_json())

    assert payload["tree"][0]["children"][0]["added_at"] == "2026-09-13T12:00:00+00:00"


async def test_tabs_are_grouped_by_client(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record("dev1", client_name="alpha", entries=[("一", "https://a.test/", 1)]),
            tabs_record("dev2", client_name="beta", entries=[("二", "https://b.test/", 2)]),
        ],
    )

    report = await run(tmp_path, data_type="tabs")

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

    report = await run(tmp_path, data_type="tabs", limit=2)

    assert report.matched == 3
    assert report.returned == 2
    assert [client.client_name for client in report.clients] == ["alpha"]
    assert [tab.title for tab in report.clients[0].tabs] == ["一", "二"]


async def test_bookmark_since_filter_compares_real_times(tmp_path: Path) -> None:
    """``--since`` 对书签走真时间比较 —— 与 history 同一个口径。"""
    await build_db(
        tmp_path,
        [],
        bookmarks=[
            bookmark_record(
                "old", parent_id=None, title="旧", date_added=ADDED_MILLIS - 86_400_000
            ),
            bookmark_record(
                "new", parent_id=None, title="新", date_added=ADDED_MILLIS + 86_400_000
            ),
        ],
    )

    report = await run(tmp_path, data_type="bookmarks", since=DAY)

    assert [node.id for node in report.tree] == ["new"]
    assert report.returned == 1


async def test_tabs_since_filter_compares_real_times(tmp_path: Path) -> None:
    seconds = int(DAY.timestamp())
    await build_db(
        tmp_path,
        [],
        tabs=[
            tabs_record(
                "dev",
                client_name="alpha",
                entries=[
                    ("旧", "https://a.test/", seconds - 3_600),
                    ("新", "https://b.test/", seconds + 3_600),
                ],
            ),
        ],
    )

    report = await run(tmp_path, data_type="tabs", since=DAY)

    assert [tab.title for tab in report.clients[0].tabs] == ["新"]
    assert report.returned == 1


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

    report = await run(tmp_path, data_type="tabs")
    payload = json.loads(report.to_json())

    assert payload["clients"][0]["tabs"][0]["last_used_at"] == "2023-11-14T22:13:20+00:00"
