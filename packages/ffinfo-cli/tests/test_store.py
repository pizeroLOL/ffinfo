"""本地落盘：piccolo 表、整体替换、幂等。

全部用临时目录 —— 不碰 ``~/.local/share/ffinfo-cli`` 里的真库。
"""

# 上面三行：测试要直接看原始行（表类来自 piccolo，没有存根）。
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownParameterType=false
# pyright: reportUnknownLambdaType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import cast

import pytest

from ffinfo.storage import EncryptedBso
from ffinfo_cli.store import (
    CollectionBatch,
    SyncRecord,
    open_database,
)


def record(
    record_id: str,
    *,
    payload: str | None = "encrypted",
    modified: float = 1.0,
    sortindex: int | None = None,
    ttl: int | None = None,
) -> EncryptedBso:
    """造一条记录。``payload=None`` 就是墓碑。"""
    return EncryptedBso(
        id=record_id, modified=modified, payload=payload, sortindex=sortindex, ttl=ttl
    )


async def test_open_database_creates_file_and_table(tmp_path: Path) -> None:
    """库文件与表都按需建出来。"""
    path = tmp_path / "nested" / "ffinfo.sqlite"

    await open_database(path)

    assert path.exists()
    assert await SyncRecord.count() == 0


async def test_replace_collection_round_trip(tmp_path: Path) -> None:
    """存进去，读得回来。"""
    store = await open_database(tmp_path / "db.sqlite")

    stored = await store.replace_collection("history", [record("a"), record("b")])

    assert stored == 2
    rows = await SyncRecord.select().order_by(SyncRecord.record_id)
    assert [row["record_id"] for row in rows] == ["a", "b"]
    assert rows[0]["payload"] == "encrypted"
    assert rows[0]["collection"] == "history"


async def test_tombstones_are_not_stored(tmp_path: Path) -> None:
    """墓碑表示"这条在别的设备上被删了" —— 它的归宿是**那一行不存在**。

    这样"库里有行"就等于"这条记录在服务器上存在"，消费方不用再判空。
    """
    store = await open_database(tmp_path / "db.sqlite")

    stored = await store.replace_collection("history", [record("a"), record("gone", payload=None)])

    assert stored == 1
    rows = await SyncRecord.select()
    assert [row["record_id"] for row in rows] == ["a"]


async def test_payload_is_stored_verbatim(tmp_path: Path) -> None:
    """硬要求：加密原文一个字节都不动。"""
    store = await open_database(tmp_path / "db.sqlite")
    encrypted = '{"ciphertext":"AAAA","IV":"BBBB","hmac":"CCCC"}'

    await store.replace_collection("history", [record("a", payload=encrypted)])

    rows = await SyncRecord.select()
    assert rows[0]["payload"] == encrypted


async def test_extra_fields_round_trip(tmp_path: Path) -> None:
    """sortindex / ttl 也要留着 —— 05、07 用得上。"""
    store = await open_database(tmp_path / "db.sqlite")

    await store.replace_collection("bookmarks", [record("a", sortindex=7, ttl=60)])

    rows = await SyncRecord.select()
    assert rows[0]["sortindex"] == 7
    assert rows[0]["ttl"] == 60
    assert rows[0]["modified"] == 1.0


async def test_replace_collection_is_idempotent(tmp_path: Path) -> None:
    """重跑一次 sync 不该翻倍 —— 全量拉取是"替换"语义。"""
    store = await open_database(tmp_path / "db.sqlite")

    await store.replace_collection("history", [record("a"), record("b")])
    await store.replace_collection("history", [record("a")])

    assert await SyncRecord.count() == 1


async def test_replace_collection_drops_stale_rows(tmp_path: Path) -> None:
    """上一轮有、这一轮没有的记录要清掉 —— 否则库里会留下已经删掉的历史。"""
    store = await open_database(tmp_path / "db.sqlite")

    await store.replace_collection("history", [record("a"), record("gone")])
    await store.replace_collection("history", [record("a")])

    rows = await SyncRecord.select()
    assert [row["record_id"] for row in rows] == ["a"]


async def test_replace_collection_keeps_other_collections(tmp_path: Path) -> None:
    """替换是"按 collection"的，别把别人的数据一起端了。"""
    store = await open_database(tmp_path / "db.sqlite")

    await store.replace_collection("history", [record("a")])
    await store.replace_collection("bookmarks", [record("b")])

    assert await SyncRecord.count() == 2
    assert await SyncRecord.count().where(SyncRecord.collection == "history") == 1


async def test_replace_collection_accepts_empty(tmp_path: Path) -> None:
    """空集合也是合法结果（账号上这个 collection 就是没有东西）。"""
    store = await open_database(tmp_path / "db.sqlite")
    await store.replace_collection("history", [record("a")])

    stored = await store.replace_collection("history", [])

    assert stored == 0
    assert await SyncRecord.count() == 0


async def test_full_replace_reports_deleted_and_updated(tmp_path: Path) -> None:
    """``--full`` 的账要能对得上：agent 问"什么被删了"，答案不能永远是 0。"""
    store = await open_database(tmp_path / "db.sqlite")
    await store.store_batches(
        [
            CollectionBatch(
                collection="history", records=[record("a"), record("b"), record("c")], full=True
            )
        ],
    )

    results = await store.store_batches(
        [
            CollectionBatch(
                collection="history", records=[record("b"), record("c"), record("d")], full=True
            )
        ],
    )

    applied = results["history"]
    assert applied.inserted == 1  # d
    assert applied.updated == 2  # b、c 还在
    assert applied.deleted == 1  # a 没了
    assert applied.total == 4  # 前后并集 {a,b,c,d}


async def test_duplicate_ids_in_one_batch_are_folded(tmp_path: Path) -> None:
    """同一批里同 id 出现两次 —— 只留最新的那条，库里不会出现两行。"""
    store = await open_database(tmp_path / "db.sqlite")

    results = await store.store_batches(
        [
            CollectionBatch(
                collection="history",
                records=[record("a", modified=1.0), record("a", modified=5.0, payload="newer")],
                full=False,
            )
        ],
    )

    assert results["history"].inserted == 1
    assert await SyncRecord.count() == 1
    rows = await SyncRecord.select()
    assert rows[0]["payload"] == "newer"


async def test_incremental_ignores_an_older_record(tmp_path: Path) -> None:
    """变更集里混进旧的 —— 不许拿旧盖新。"""
    store = await open_database(tmp_path / "db.sqlite")
    await store.store_batches(
        [CollectionBatch(collection="history", records=[record("a", modified=5.0)], full=False)],
    )

    results = await store.store_batches(
        [
            CollectionBatch(
                collection="history",
                records=[record("a", modified=1.0, payload="old")],
                full=False,
            )
        ],
    )

    assert results["history"].updated == 0
    rows = await SyncRecord.select()
    assert rows[0]["payload"] == "encrypted"


async def test_record_identity_is_enforced_by_the_database(tmp_path: Path) -> None:
    """``(collection, record_id)`` 唯一 —— 绕过代码直接插第二行会被库拒绝。"""
    store = await open_database(tmp_path / "db.sqlite")
    await store.replace_collection("history", [record("a")])

    with pytest.raises(sqlite3.IntegrityError):
        await SyncRecord.insert(
            SyncRecord(collection="history", record_id="a", modified=2.0, payload="dup")
        )


async def test_old_databases_with_duplicate_rows_are_repaired(tmp_path: Path) -> None:
    """早期版本写出来的库里可能躺着重复行 —— 开库时收敛，**并且喊一声**。"""
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sync_records (
            id INTEGER PRIMARY KEY,
            collection VARCHAR(64) NOT NULL,
            record_id VARCHAR(64) NOT NULL,
            modified DOUBLE PRECISION NOT NULL,
            payload TEXT,
            sortindex BIGINT,
            ttl BIGINT
        );
        INSERT INTO sync_records (collection, record_id, modified, payload) VALUES
            ('history', 'a', 1.0, 'old'),
            ('history', 'a', 5.0, 'new'),
            ('bookmarks', 'a', 2.0, 'other');
        """
    )
    connection.commit()
    connection.close()
    warnings: list[str] = []

    await open_database(path, warn=warnings.append)

    rows = await SyncRecord.select().order_by(SyncRecord.id)
    assert [(row["collection"], row["record_id"], row["payload"]) for row in rows] == [
        ("history", "a", "new"),
        ("bookmarks", "a", "other"),
    ]
    assert len(warnings) == 1
    assert "1 条重复记录" in warnings[0]


async def test_legacy_local_visits_table_is_migrated_on_open(tmp_path: Path) -> None:
    """老库里的 ``local_visits`` 要就地改名 —— 不然已导入的 firefox 访问会静默消失。

    新表是空的没人写、旧表没人读，查询结果无声变少 —— 所以这条测试盯的是
    "打开老库之后，数据仍然能通过新接口读出来"。
    """
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE local_visits (
            id INTEGER PRIMARY KEY,
            machine VARCHAR(128) NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            visited_at BIGINT NOT NULL,
            visit_type INTEGER NOT NULL
        );
        INSERT INTO local_visits (machine, url, title, visited_at, visit_type)
            VALUES ('test-laptop', 'https://a.example/', 'A', 1700000000000000, 1);
        """
    )
    connection.commit()
    connection.close()

    store = await open_database(path)

    stored = await store.load_firefox_visits()
    assert [item.url for item in stored] == ["https://a.example/"]
    assert stored[0].machine == "test-laptop"
    assert stored[0].title == "A"


async def test_coexisting_local_and_firefox_visits_are_merged(tmp_path: Path) -> None:
    """两表并存（半迁移 / 手工改过）时，旧表独有的行要补进新表并删掉旧表。

    以前只在"旧表在、新表不在"时改名 —— 两表并存就直接跳过，旧表数据被静默忽略。
    这里盯的是三件事：**旧行读得到、重复的不重插、搬了多少条经 warn 报出来**。
    """
    path = tmp_path / "half-migrated.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE firefox_visits (
            id INTEGER PRIMARY KEY,
            machine VARCHAR(128) NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            visited_at BIGINT NOT NULL,
            visit_type INTEGER NOT NULL
        );
        CREATE TABLE local_visits (
            id INTEGER PRIMARY KEY,
            machine VARCHAR(128) NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            visited_at BIGINT NOT NULL,
            visit_type INTEGER NOT NULL
        );
        INSERT INTO firefox_visits (machine, url, title, visited_at, visit_type) VALUES
            ('test-laptop', 'https://shared.example/', 'Shared', 1700000000000000, 1),
            ('test-laptop', 'https://new-only.example/', 'New only', 1700000002000000, 1);
        INSERT INTO local_visits (machine, url, title, visited_at, visit_type) VALUES
            ('test-laptop', 'https://shared.example/', 'Shared', 1700000000000000, 1),
            ('test-laptop', 'https://old-only.example/', 'Old only', 1700000001000000, 1);
        """
    )
    connection.commit()
    connection.close()
    warnings: list[str] = []

    store = await open_database(path, warn=warnings.append)

    stored = await store.load_firefox_visits()
    assert [item.url for item in stored] == [
        "https://shared.example/",
        "https://old-only.example/",
        "https://new-only.example/",
    ]
    tables = _table_names(path)
    assert "local_visits" not in tables
    assert "firefox_visits" in tables
    assert len(warnings) == 1
    assert "1 条" in warnings[0]
    assert "local_visits" in warnings[0]


async def test_coexisting_tables_do_not_duplicate_rows_that_are_already_there(
    tmp_path: Path,
) -> None:
    """旧行和新表里的身份键一样时**不重插** —— 搬 0 条也要说清楚。"""
    path = tmp_path / "half-migrated.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE firefox_visits (
            id INTEGER PRIMARY KEY,
            machine VARCHAR(128) NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            visited_at BIGINT NOT NULL,
            visit_type INTEGER NOT NULL
        );
        CREATE TABLE local_visits (
            id INTEGER PRIMARY KEY,
            machine VARCHAR(128) NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            visited_at BIGINT NOT NULL,
            visit_type INTEGER NOT NULL
        );
        INSERT INTO firefox_visits (machine, url, title, visited_at, visit_type) VALUES
            ('test-laptop', 'https://a.example/', 'A', 1700000000000000, 1);
        INSERT INTO local_visits (machine, url, title, visited_at, visit_type) VALUES
            ('test-laptop', 'https://a.example/', 'A', 1700000000000000, 1);
        """
    )
    connection.commit()
    connection.close()
    warnings: list[str] = []

    store = await open_database(path, warn=warnings.append)

    assert len(await store.load_firefox_visits()) == 1
    assert "local_visits" not in _table_names(path)
    assert len(warnings) == 1
    assert "0 条" in warnings[0]


def _table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()


async def test_read_only_open_leaves_a_legacy_database_alone(tmp_path: Path) -> None:
    """``read_only=True``：不建表、不收敛 —— 库里原来什么样，打开后还是什么样。"""
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sync_records (
            id INTEGER PRIMARY KEY,
            collection VARCHAR(64) NOT NULL,
            record_id VARCHAR(64) NOT NULL,
            modified DOUBLE PRECISION NOT NULL,
            payload TEXT,
            sortindex BIGINT,
            ttl BIGINT
        );
        INSERT INTO sync_records (collection, record_id, modified, payload) VALUES
            ('history', 'a', 1.0, 'old'),
            ('history', 'a', 5.0, 'new');
        """
    )
    connection.commit()
    connection.close()

    await open_database(path, read_only=True)

    connection = sqlite3.connect(path)
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    rows = connection.execute("SELECT payload FROM sync_records ORDER BY id").fetchall()
    connection.close()
    assert tables == [("sync_records",)]  # 没顺手建出 sync_cursors / firefox_visits
    assert [row[0] for row in rows] == ["old", "new"]  # 也没顺手收敛


async def test_clean_databases_do_not_warn(tmp_path: Path) -> None:
    """没重复就别吓人 —— 建过索引之后也不会再查。"""
    path = tmp_path / "db.sqlite"
    warnings: list[str] = []

    await open_database(path, warn=warnings.append)
    await open_database(path, warn=warnings.append)

    assert warnings == []


class _FailsMidway:
    """读到一半就炸的 records —— 模拟第二个 batch 写到一半出事。"""

    def __iter__(self) -> Iterator[EncryptedBso]:
        msg = "磁盘满了"
        raise RuntimeError(msg)


async def test_store_batches_rolls_back_when_a_later_batch_fails(tmp_path: Path) -> None:
    """**全成或全不写**：第二个 batch 炸了，第一个 batch 的字节也不许留下。"""
    store = await open_database(tmp_path / "db.sqlite")

    with pytest.raises(RuntimeError, match="磁盘满了"):
        await store.store_batches(
            [
                CollectionBatch(collection="bookmarks", records=[record("keep")], full=True),
                CollectionBatch(
                    collection="history",
                    records=cast(Sequence[EncryptedBso], _FailsMidway()),
                    full=True,
                ),
            ],
        )

    assert await SyncRecord.count() == 0
