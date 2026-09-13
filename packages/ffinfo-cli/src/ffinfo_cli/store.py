"""本地 SQLite —— 用 piccolo ORM 落盘。

**这一层不解密。** Sync 拉下来的 ``payload`` 是加密原文，原样进库；解密在 ``list`` 那边。

表按设计文档决策 12 的"双源分表 + 保留来源标记"来切：``sync_records`` 只放云端来的，
将来本地 ``places.sqlite`` 来的走另一张表 —— 不硬凑成一张。

**墓碑不落库。** 服务器上的墓碑（``payload`` 为 ``null``）表示"这条在别的设备上被删了"，
所以它的正确归宿是**让那一行不存在**，而不是存一条空记录。于是"库里有行"就等于
"这条记录在服务器上存在" —— 消费方不用再判空。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from piccolo.columns import BigInt, DoublePrecision, Integer, Text, Varchar
from piccolo.engine.sqlite import SQLiteEngine
from piccolo.table import Table

from ffinfo.storage import EncryptedBso

__all__ = [
    "ApplyResult",
    "CollectionBatch",
    "SyncCursor",
    "SyncRecord",
    "load_cursor",
    "load_records",
    "open_database",
    "replace_collection",
    "save_cursor",
    "store_batches",
]


class SyncRecord(Table, tablename="sync_records"):
    """一条 Sync 记录的加密原文。"""

    collection: Varchar = Varchar(length=64, index=True)
    record_id: Varchar = Varchar(length=64, index=True)
    modified: DoublePrecision = DoublePrecision(index=True)
    payload: Text = Text(null=True)
    sortindex: BigInt = BigInt(null=True)
    ttl: BigInt = BigInt(null=True)


class SyncCursor(Table, tablename="sync_cursors"):
    """每个 collection 的**同步游标** —— 服务器给的 collection 时间戳。

    下一次增量拉取拿它当 ``newer``。规则只有一条：**只有一次完整的拉取成功了才推进它**。
    中途退避、被改、条数对不上，游标原地不动 —— 否则那段窗口里的变更就永远丢了。

    ⚠️ **游标不是行号，别挂到 ``sync_records.id`` 上。** 那个自增主键在"删了重插"之后
    会从 1 重排（实测过），拿它当游标会静默漏数据。
    """

    collection: Varchar = Varchar(length=64, unique=True)
    last_modified: DoublePrecision = DoublePrecision()
    synced_at: DoublePrecision = DoublePrecision()
    records: Integer = Integer()


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """一次落库的账。"""

    inserted: int
    updated: int
    deleted: int

    @property
    def total(self) -> int:
        """处理了多少条（含删除）。"""
        return self.inserted + self.updated + self.deleted


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    """一个 collection 这次要写什么、按什么方式写。"""

    collection: str
    records: Sequence[EncryptedBso]
    full: bool
    """``True`` = 整体替换（全量拉取）；``False`` = 增量 upsert。"""


def _bind(engine: SQLiteEngine) -> None:
    """把表绑到调用者给的 engine 上。

    piccolo 的表是类级别的单例，"绑哪个库"只能挂在类上 —— 所以每次操作都显式重绑一次，
    测试才能各用各的临时库（见 ``tests/test_store.py``）。
    """
    for table in (SyncRecord, SyncCursor):
        table._meta.db = engine


async def open_database(path: Path) -> SQLiteEngine:
    """打开本地库，表不存在就建。**不建默认路径** —— 路径由调用者给。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = SQLiteEngine(path=str(path))
    _bind(engine)
    await SyncRecord.create_table(if_not_exists=True)
    await SyncCursor.create_table(if_not_exists=True)
    return engine


# ── 写入 ──────────────────────────────────────────────────────────────────


async def store_batches(
    engine: SQLiteEngine, batches: Sequence[CollectionBatch]
) -> dict[str, ApplyResult]:
    """**一次事务**里写入多个 collection —— 要么全成，要么一个字节都不写。

    为什么要一次事务：``history`` 写进去了、``crypto/keys`` 没写进去，
    库就处于"有数据但解不开"的半截状态 —— 那比什么都没有更让人困惑。
    """
    _bind(engine)
    results: dict[str, ApplyResult] = {}
    async with engine.transaction():
        for batch in batches:
            results[batch.collection] = await _write_one(engine, batch)
    return results


async def _write_one(engine: SQLiteEngine, batch: CollectionBatch) -> ApplyResult:
    """写一个 collection。**调用方负责事务。**"""
    if batch.full:
        return await _replace(engine, batch.collection, batch.records)
    return await _apply(engine, batch.collection, batch.records)


async def _replace(
    engine: SQLiteEngine, collection: str, records: Sequence[EncryptedBso]
) -> ApplyResult:
    """整体替换：删了重插。全量拉取用这个 —— 天然幂等，也不会留下上一轮已删的记录。"""
    rows = [_row(collection, record) for record in records if record.payload is not None]
    await SyncRecord.delete().where(SyncRecord.collection == collection)
    if rows:
        await SyncRecord.insert(*rows)
    return ApplyResult(inserted=len(rows), updated=0, deleted=0)


async def _apply(
    engine: SQLiteEngine, collection: str, records: Sequence[EncryptedBso]
) -> ApplyResult:
    """增量 upsert：新记录插入、已有的覆盖、墓碑删行。

    增量拉回来的只是**变更集**，所以不能像全量那样"删了重插" ——
    那会把没变更的几千条一起端掉。
    """
    existing = {
        str(row["record_id"]): row["id"]
        for row in await SyncRecord.select(SyncRecord.id, SyncRecord.record_id).where(
            SyncRecord.collection == collection
        )
    }

    fresh: list[SyncRecord] = []
    updates: list[tuple[int, EncryptedBso]] = []
    removals: list[int] = []
    for record in records:
        row_id = existing.get(record.id)
        if record.payload is None:
            if row_id is not None:
                removals.append(row_id)
        elif row_id is None:
            fresh.append(_row(collection, record))
        else:
            updates.append((row_id, record))

    if fresh:
        await SyncRecord.insert(*fresh)
    for row_id, record in updates:
        await SyncRecord.update(
            {
                SyncRecord.modified: record.modified,
                SyncRecord.payload: record.payload,
                SyncRecord.sortindex: record.sortindex,
                SyncRecord.ttl: record.ttl,
            }
        ).where(SyncRecord.id == row_id)
    if removals:
        await SyncRecord.delete().where(SyncRecord.id.is_in(removals))

    return ApplyResult(inserted=len(fresh), updated=len(updates), deleted=len(removals))


async def replace_collection(
    engine: SQLiteEngine, collection: str, records: Sequence[EncryptedBso]
) -> int:
    """用这一批记录整体替换**一个** collection，返回落库条数。"""
    result = await store_batches(
        engine, [CollectionBatch(collection=collection, records=records, full=True)]
    )
    return result[collection].inserted


def _row(collection: str, record: EncryptedBso) -> SyncRecord:
    """一条记录 → 一行。"""
    return SyncRecord(
        collection=collection,
        record_id=record.id,
        modified=record.modified,
        payload=record.payload,
        sortindex=record.sortindex,
        ttl=record.ttl,
    )


# ── 游标 ──────────────────────────────────────────────────────────────────


async def load_cursor(engine: SQLiteEngine, collection: str) -> float | None:
    """读游标。没有、或者值坏了（不是个数字）都返回 ``None`` —— 调用方回退到全量。

    游标坏了就当没有：全量重拉一次是**安全**的，而拿着一个坏游标往下跑会**静默漏数据**。
    """
    _bind(engine)
    rows = await SyncCursor.select(SyncCursor.last_modified).where(
        SyncCursor.collection == collection
    )
    if not rows:
        return None
    value = rows[0]["last_modified"]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


async def save_cursor(
    engine: SQLiteEngine,
    collection: str,
    *,
    last_modified: float,
    synced_at: float,
    records: int,
) -> None:
    """推进游标。**只在一次完整拉取成功之后调**。"""
    _bind(engine)
    async with engine.transaction():
        await SyncCursor.delete().where(SyncCursor.collection == collection)
        await SyncCursor.insert(
            SyncCursor(
                collection=collection,
                last_modified=last_modified,
                synced_at=synced_at,
                records=records,
            )
        )


# ── 读取 ──────────────────────────────────────────────────────────────────


async def load_records(engine: SQLiteEngine, collection: str) -> list[tuple[str, str | None]]:
    """读一个 collection 的 ``(record_id, payload)``。"""
    _bind(engine)
    rows = await SyncRecord.select(SyncRecord.record_id, SyncRecord.payload).where(
        SyncRecord.collection == collection
    )
    return [(str(row["record_id"]), row["payload"]) for row in rows]
