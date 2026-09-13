"""本地 SQLite —— 用 piccolo ORM 落盘（04 号 ticket）。

**这一层不解密。** Sync 拉下来的 ``payload`` 是加密原文，原样进库；
解密是 05 号的事，双源合并是 08 号的事。

表按设计文档决策 12 的"双源分表 + 保留来源标记"来切：``sync_records`` 只放云端来的，
将来本地 ``places.sqlite`` 来的走另一张表 —— 不硬凑成一张。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from piccolo.columns import BigInt, DoublePrecision, Text, Varchar
from piccolo.engine.sqlite import SQLiteEngine
from piccolo.table import Table

from ffinfo.storage import EncryptedBso

__all__ = [
    "SyncRecord",
    "load_records",
    "open_database",
    "replace_collection",
    "replace_collections",
]


class SyncRecord(Table, tablename="sync_records"):
    """一条 Sync 记录的加密原文。

    ``payload`` 为 ``NULL`` 表示墓碑 —— 这条记录在别的设备上被删了，
    但同步协议仍然要把它传过来。别把墓碑当成"没拉到"。
    """

    collection = Varchar(length=64, index=True)
    record_id = Varchar(length=64, index=True)
    modified = DoublePrecision(index=True)
    payload = Text(null=True)
    sortindex = BigInt(null=True)
    ttl = BigInt(null=True)


def _bind(engine: SQLiteEngine) -> None:
    """把表绑到调用者给的 engine 上。

    piccolo 的表是类级别的单例，"绑哪个库"只能挂在类上 —— 所以每次操作都显式重绑一次，
    测试才能各用各的临时库（见 ``tests/test_store.py``）。
    """
    SyncRecord._meta.db = engine


async def open_database(path: Path) -> SQLiteEngine:
    """打开本地库，表不存在就建。**不建默认路径** —— 路径由调用者给。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = SQLiteEngine(path=str(path))
    _bind(engine)
    await SyncRecord.create_table(if_not_exists=True)
    return engine


async def replace_collections(
    engine: SQLiteEngine, batches: Mapping[str, Sequence[EncryptedBso]]
) -> dict[str, int]:
    """**一次事务**里整体替换多个 collection，返回各自的落库条数。

    为什么要一次事务：``history`` 写进去了、``crypto/keys`` 没写进去，
    库就处于"有数据但解不开"的半截状态 —— 那比什么都没有更让人困惑。
    """
    _bind(engine)
    async with engine.transaction():
        stored: dict[str, int] = {}
        for collection, records in batches.items():
            await SyncRecord.delete().where(SyncRecord.collection == collection)
            rows = [
                SyncRecord(
                    collection=collection,
                    record_id=record.id,
                    modified=record.modified,
                    payload=record.payload,
                    sortindex=record.sortindex,
                    ttl=record.ttl,
                )
                for record in records
            ]
            if rows:
                await SyncRecord.insert(*rows)
            stored[collection] = len(rows)
    return stored


async def replace_collection(
    engine: SQLiteEngine, collection: str, records: Sequence[EncryptedBso]
) -> int:
    """用这一批记录整体替换**一个** collection，返回落库条数。

    全量拉取天然是"替换"语义，所以直接删了重插 —— 重跑一次 ``sync`` 不会翻倍，
    也不会留下上一轮已经被删掉的记录。
    """
    stored = await replace_collections(engine, {collection: records})
    return stored[collection]


async def load_records(engine: SQLiteEngine, collection: str) -> list[tuple[str, str | None]]:
    """读一个 collection 的 ``(record_id, payload)``。``payload`` 为 ``None`` 是墓碑。"""
    _bind(engine)
    rows = await SyncRecord.select(SyncRecord.record_id, SyncRecord.payload).where(
        SyncRecord.collection == collection
    )
    return [(str(row["record_id"]), row["payload"]) for row in rows]
