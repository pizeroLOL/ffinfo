"""本地落盘：piccolo 表、整体替换、幂等。

全部用临时目录 —— 不碰 ``~/.local/share/ffinfo-cli`` 里的真库。
"""

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
    replace_collection,
    store_batches,
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
    engine = await open_database(tmp_path / "db.sqlite")

    stored = await replace_collection(engine, "history", [record("a"), record("b")])

    assert stored == 2
    rows = await SyncRecord.select().order_by(SyncRecord.record_id)
    assert [row["record_id"] for row in rows] == ["a", "b"]
    assert rows[0]["payload"] == "encrypted"
    assert rows[0]["collection"] == "history"


async def test_tombstones_are_not_stored(tmp_path: Path) -> None:
    """墓碑表示"这条在别的设备上被删了" —— 它的归宿是**那一行不存在**。

    这样"库里有行"就等于"这条记录在服务器上存在"，消费方不用再判空。
    """
    engine = await open_database(tmp_path / "db.sqlite")

    stored = await replace_collection(
        engine, "history", [record("a"), record("gone", payload=None)]
    )

    assert stored == 1
    rows = await SyncRecord.select()
    assert [row["record_id"] for row in rows] == ["a"]


async def test_payload_is_stored_verbatim(tmp_path: Path) -> None:
    """硬要求：加密原文一个字节都不动。"""
    engine = await open_database(tmp_path / "db.sqlite")
    encrypted = '{"ciphertext":"AAAA","IV":"BBBB","hmac":"CCCC"}'

    await replace_collection(engine, "history", [record("a", payload=encrypted)])

    rows = await SyncRecord.select()
    assert rows[0]["payload"] == encrypted


async def test_extra_fields_round_trip(tmp_path: Path) -> None:
    """sortindex / ttl 也要留着 —— 05、07 用得上。"""
    engine = await open_database(tmp_path / "db.sqlite")

    await replace_collection(engine, "bookmarks", [record("a", sortindex=7, ttl=60)])

    rows = await SyncRecord.select()
    assert rows[0]["sortindex"] == 7
    assert rows[0]["ttl"] == 60
    assert rows[0]["modified"] == 1.0


async def test_replace_collection_is_idempotent(tmp_path: Path) -> None:
    """重跑一次 sync 不该翻倍 —— 全量拉取是"替换"语义。"""
    engine = await open_database(tmp_path / "db.sqlite")

    await replace_collection(engine, "history", [record("a"), record("b")])
    await replace_collection(engine, "history", [record("a")])

    assert await SyncRecord.count() == 1


async def test_replace_collection_drops_stale_rows(tmp_path: Path) -> None:
    """上一轮有、这一轮没有的记录要清掉 —— 否则库里会留下已经删掉的历史。"""
    engine = await open_database(tmp_path / "db.sqlite")

    await replace_collection(engine, "history", [record("a"), record("gone")])
    await replace_collection(engine, "history", [record("a")])

    rows = await SyncRecord.select()
    assert [row["record_id"] for row in rows] == ["a"]


async def test_replace_collection_keeps_other_collections(tmp_path: Path) -> None:
    """替换是"按 collection"的，别把别人的数据一起端了。"""
    engine = await open_database(tmp_path / "db.sqlite")

    await replace_collection(engine, "history", [record("a")])
    await replace_collection(engine, "bookmarks", [record("b")])

    assert await SyncRecord.count() == 2
    assert await SyncRecord.count().where(SyncRecord.collection == "history") == 1


async def test_replace_collection_accepts_empty(tmp_path: Path) -> None:
    """空集合也是合法结果（账号上这个 collection 就是没有东西）。"""
    engine = await open_database(tmp_path / "db.sqlite")
    await replace_collection(engine, "history", [record("a")])

    stored = await replace_collection(engine, "history", [])

    assert stored == 0
    assert await SyncRecord.count() == 0


async def test_full_replace_reports_deleted_and_updated(tmp_path: Path) -> None:
    """``--full`` 的账要能对得上：agent 问"什么被删了"，答案不能永远是 0。"""
    engine = await open_database(tmp_path / "db.sqlite")
    await store_batches(
        engine,
        [
            CollectionBatch(
                collection="history", records=[record("a"), record("b"), record("c")], full=True
            )
        ],
    )

    results = await store_batches(
        engine,
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
    engine = await open_database(tmp_path / "db.sqlite")

    results = await store_batches(
        engine,
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
    engine = await open_database(tmp_path / "db.sqlite")
    await store_batches(
        engine,
        [CollectionBatch(collection="history", records=[record("a", modified=5.0)], full=False)],
    )

    results = await store_batches(
        engine,
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
    engine = await open_database(tmp_path / "db.sqlite")
    await replace_collection(engine, "history", [record("a")])

    with pytest.raises(sqlite3.IntegrityError):
        await SyncRecord.insert(
            SyncRecord(collection="history", record_id="a", modified=2.0, payload="dup")
        )


async def test_old_databases_with_duplicate_rows_are_repaired(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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

    await open_database(path)

    rows = await SyncRecord.select().order_by(SyncRecord.id)
    assert [(row["collection"], row["record_id"], row["payload"]) for row in rows] == [
        ("history", "a", "new"),
        ("bookmarks", "a", "other"),
    ]
    assert "1 条重复记录" in capsys.readouterr().err


async def test_clean_databases_do_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """没重复就别吓人 —— 建过索引之后也不会再查。"""
    path = tmp_path / "db.sqlite"

    await open_database(path)
    await open_database(path)

    assert capsys.readouterr().err == ""


class _FailsMidway:
    """读到一半就炸的 records —— 模拟第二个 batch 写到一半出事。"""

    def __iter__(self) -> Iterator[EncryptedBso]:
        msg = "磁盘满了"
        raise RuntimeError(msg)


async def test_store_batches_rolls_back_when_a_later_batch_fails(tmp_path: Path) -> None:
    """**全成或全不写**：第二个 batch 炸了，第一个 batch 的字节也不许留下。"""
    engine = await open_database(tmp_path / "db.sqlite")

    with pytest.raises(RuntimeError, match="磁盘满了"):
        await store_batches(
            engine,
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
