"""本地落盘（04 号 ticket）：piccolo 表、整体替换、幂等。

全部用临时目录 —— 不碰 ``~/.local/share/ffinfo-cli`` 里的真库。
"""

from __future__ import annotations

from pathlib import Path

from ffinfo.storage import EncryptedBso
from ffinfo_cli.store import SyncRecord, open_database, replace_collection


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
    """存进去，读得回来 —— 包括墓碑那条。"""
    engine = await open_database(tmp_path / "db.sqlite")

    stored = await replace_collection(engine, "history", [record("a"), record("b", payload=None)])

    assert stored == 2
    rows = await SyncRecord.select().order_by(SyncRecord.record_id)
    assert [row["record_id"] for row in rows] == ["a", "b"]
    assert rows[0]["payload"] == "encrypted"
    assert rows[1]["payload"] is None  # 墓碑：payload 是 NULL，不是"没拉到"
    assert rows[0]["collection"] == "history"


async def test_payload_is_stored_verbatim(tmp_path: Path) -> None:
    """04 号 ticket 的硬要求：加密原文一个字节都不动。"""
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
