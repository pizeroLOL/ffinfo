"""``ffinfo-cli list``（05 号 ticket）：解密、过滤、JSON 输出。

全程不联网：库是临时目录里现造的，密文用本库自己的加密方向生成
（加密方向已由 01 号 ticket 对着官方向量逐字节验过）。
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
from ffinfo_cli.list import matches_domain, matches_search, parse_since, run_list
from ffinfo_cli.store import CollectionBatch, open_database, store_batches

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


def micros(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000)


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


async def build_db(
    tmp_path: Path, records: list[EncryptedBso], *, keys: EncryptedBso | None = None
) -> None:
    """把库造出来：一条 crypto/keys + 若干 history 记录。"""
    engine = await open_database(tmp_path / "db.sqlite")
    await store_batches(
        engine,
        [
            CollectionBatch(
                collection="crypto",
                records=[keys if keys is not None else keys_record()],
                full=True,
            ),
            CollectionBatch(collection="history", records=records, full=True),
        ],
    )


async def run(tmp_path: Path, **kwargs: object) -> object:
    identity_path, credentials_path = write_credentials(tmp_path)
    return await run_list(
        identity_path=identity_path,
        credentials_path=credentials_path,
        database_path=tmp_path / "db.sqlite",
        clock=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


# ── 解密 + 输出 ───────────────────────────────────────────────────────────


async def test_lists_visits_newest_first(tmp_path: Path) -> None:
    """最新的排前面 —— 看历史就该从最近看起。"""
    await build_db(
        tmp_path,
        [
            history_record("old", url="https://old.test/", visits=[(DAY, 1)]),
            history_record("new", url="https://new.test/", visits=[(DAY + timedelta(hours=1), 2)]),
        ],
    )

    report = await run(tmp_path)  # type: ignore[assignment]

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

    report = await run(tmp_path)  # type: ignore[assignment]

    assert report.records == 1
    assert report.visits == 3
    assert report.returned == 3


async def test_timestamps_are_utc_iso(tmp_path: Path) -> None:
    await build_db(tmp_path, [history_record("rec", visits=[(DAY, 1)])])

    report = await run(tmp_path)  # type: ignore[assignment]

    assert report.items[0].visited_at == "2026-09-13T12:00:00+00:00"


async def test_output_carries_format_version_and_filters(tmp_path: Path) -> None:
    """给 agent 的接口要能演进 —— 版本号必须在。"""
    await build_db(tmp_path, [history_record("rec")])

    report = await run(tmp_path, domain="example.com")  # type: ignore[assignment]
    payload = json.loads(report.to_json())

    assert payload["format_version"] == 1
    assert payload["filters"]["domain"] == "example.com"
    assert payload["filters"]["limit"] is None
    assert payload["generated_at"] == "2026-09-13T17:30:12+00:00"


async def test_tombstones_are_not_listed(tmp_path: Path) -> None:
    """墓碑（别的设备删掉的）不该出现在浏览历史里，也不算"坏掉"。"""
    await build_db(
        tmp_path,
        [history_record("alive"), EncryptedBso(id="gone", modified=2.0, payload=None)],
    )

    report = await run(tmp_path)  # type: ignore[assignment]

    assert [item.record_id for item in report.items] == ["alive"]
    assert report.skipped == 0


# ── 单条坏掉不连坐 ────────────────────────────────────────────────────────


async def test_broken_record_is_skipped_and_counted(tmp_path: Path) -> None:
    """ticket 05 的硬要求：一条被篡改，不该让你看不到另外两条。"""
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

    report = await run(tmp_path)  # type: ignore[assignment]

    assert len(report.items) == 2
    assert report.skipped == 1
    assert report.skipped_details[0]["record_id"] == "broken"


async def test_everything_unreadable_means_the_wrong_account(tmp_path: Path) -> None:
    """全部解不开时别装没事 —— 多半是换了账号。"""
    stranger = KeyBundle(encryption_key=b"z" * 32, hmac_key=b"z" * 32)
    await build_db(tmp_path, [history_record("rec", key=stranger)])

    with pytest.raises(ConfigurationError, match="不是同一个账号"):
        await run(tmp_path)


# ── 缺东西时的提示 ────────────────────────────────────────────────────────


async def test_missing_keys_record_says_what_to_do(tmp_path: Path) -> None:
    """库里没有 crypto/keys —— 得说清楚"先跑 sync"，而不是抛个 KeyError。"""
    engine = await open_database(tmp_path / "db.sqlite")
    await store_batches(
        engine,
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


# ── 过滤 ──────────────────────────────────────────────────────────────────


async def test_since_filter(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [
            history_record("old", url="https://old.test/", visits=[(DAY, 1)]),
            history_record("new", url="https://new.test/", visits=[(DAY + timedelta(days=2), 1)]),
        ],
    )

    report = await run(tmp_path, since=DAY + timedelta(days=1))  # type: ignore[assignment]

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

    report = await run(tmp_path, domain="example.com")  # type: ignore[assignment]

    assert sorted(item.record_id for item in report.items) == ["apex", "sub"]


async def test_search_filter_covers_title_and_url_case_insensitively(tmp_path: Path) -> None:
    await build_db(
        tmp_path,
        [
            history_record("bytitle", url="https://a.test/", title="Rust 学习笔记"),
            history_record("byurl", url="https://rust-lang.org/", title="首页"),
            history_record("none", url="https://b.test/", title="别的"),
        ],
    )

    report = await run(tmp_path, search="RUST")  # type: ignore[assignment]

    assert sorted(item.record_id for item in report.items) == ["bytitle", "byurl"]


async def test_limit_caps_but_reports_the_full_match(tmp_path: Path) -> None:
    """agent 要能看出"还有更多"。"""
    await build_db(
        tmp_path,
        [history_record(f"rec{i}", visits=[(DAY + timedelta(minutes=i), 1)]) for i in range(5)],
    )

    report = await run(tmp_path, limit=2)  # type: ignore[assignment]

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

    report = await run(  # type: ignore[assignment]
        tmp_path, domain="example.com", since=DAY + timedelta(hours=6), search="keep"
    )

    assert [item.record_id for item in report.items] == ["hit"]


# ── 纯函数 ────────────────────────────────────────────────────────────────


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
