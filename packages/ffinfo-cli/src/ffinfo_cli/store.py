"""本地 SQLite —— 用 piccolo ORM 落盘。

**这一层不解密。** Sync 拉下来的 ``payload`` 是加密原文，原样进库；解密在 ``list`` 那边。

表按设计文档决策 12 的"双源分表 + 保留来源标记"来切：``sync_records`` 只放云端来的，
将来本地 ``places.sqlite`` 来的走另一张表 —— 不硬凑成一张。

对外的 interface 是 :class:`Store`：piccolo 只在这个 module 里出现，调用方碰不到表类与绑定。

**墓碑不落库。** 服务器上的墓碑（``payload`` 为 ``null``）表示"这条在别的设备上被删了"，
所以它的正确归宿是**让那一行不存在**，而不是存一条空记录。于是"库里有行"就等于
"这条记录在服务器上存在" —— 消费方不用再判空。
"""

# 上面三行：piccolo 没有类型存根（表定义、select/update 的返回都是 unknown）。
# 表类只在这个 module 里出现；调用方拿到的 Store 接口是全类型的。
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownParameterType=false
# pyright: reportUnknownLambdaType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from piccolo.columns import BigInt, DoublePrecision, Integer, Text, Varchar
from piccolo.engine.sqlite import SQLiteEngine
from piccolo.table import Table

from ffinfo.storage import EncryptedBso
from ffinfo.timestamps import from_microseconds, to_microseconds
from ffinfo_cli.portable import PortableCursor, PortableRecord

__all__ = [
    "ApplyResult",
    "CollectionBatch",
    "CursorInfo",
    "LocalVisitRow",
    "Store",
    "StoredVisit",
    "SyncCursor",
    "SyncRecord",
    "open_database",
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


class LocalVisitRow(Table, tablename="local_visits"):
    """本地 ``places.sqlite`` 来的一次访问 —— **明文**，没有解密这回事。

    与 ``sync_records`` 分表是设计文档决策 12 定的：两个源不硬凑成一张，
    查询时才合并。``machine`` 是导出那台机器的名字（同一个库将来可能收下好几台）。
    """

    machine: Varchar = Varchar(length=128, index=True)
    url: Text = Text()
    title: Text = Text()
    visited_at: BigInt = BigInt(index=True)
    """PRTime 微秒 —— 存整数不存浮点，合并时靠它**逐微秒相等**去重。"""
    visit_type: Integer = Integer()


@dataclass(frozen=True, slots=True)
class StoredVisit:
    """库里的一次本地访问。"""

    machine: str
    url: str
    title: str
    visited_at: datetime
    visit_type: int


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """一次落库的账。

    ``inserted`` / ``updated`` / ``deleted`` 是**互斥的三份**：新出现的、本来就有的、
    没了的那部分 —— 加起来正好是这次碰到的记录总数。
    """

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


class Store:
    """本地库的 adapter —— **piccolo 只在这个 module 里出现**。

    构造走 :func:`open_database`。每个操作开始前把表绑到这把 engine 上 ——
    piccolo 的表是类级单例，"绑哪个库"只能挂在类上，所以这一步集中在 :meth:`_bind`。
    """

    __slots__: tuple[str, ...] = ("_engine",)

    def __init__(self, engine: SQLiteEngine) -> None:
        """包一把 engine；一般走 :func:`open_database`。"""
        self._engine = engine

    def _bind(self) -> None:
        """把表绑到这把 engine 上 —— 每个操作先调它，别让上一位调用方的绑定留下来。"""
        _bind(self._engine)

    async def store_batches(self, batches: Sequence[CollectionBatch]) -> dict[str, ApplyResult]:
        """**一次事务**里写入多个 collection —— 要么全成，要么一个字节都不写。

        为什么要一次事务：``history`` 写进去了、``crypto/keys`` 没写进去，
        库就处于"有数据但解不开"的半截状态 —— 那比什么都没有更让人困惑。
        """
        self._bind()
        results: dict[str, ApplyResult] = {}
        async with self._engine.transaction():
            for batch in batches:
                results[batch.collection] = await self._write_one(batch)
        return results

    async def _write_one(self, batch: CollectionBatch) -> ApplyResult:
        """写一个 collection。**调用方负责事务。**"""
        if batch.full:
            return await self._replace(batch.collection, batch.records)
        return await self._apply(batch.collection, batch.records)

    async def _replace(self, collection: str, records: Sequence[EncryptedBso]) -> ApplyResult:
        """整体替换：删了重插。全量拉取用这个 —— 天然幂等，也不会留下上一轮已删的记录。

        ``deleted`` 报的是**真的没了的那部分**（老行里没被重插的）—— ``--full`` 的对账
        就靠它：agent 问"全量之后什么被删了"，答案不能永远是 0。

        同一批里同 id 出现多次时与增量一样只认最新的一条（跨页重复不该把整次同步打翻）。
        """
        live = [record for record in _newest_per_id(records) if record.payload is not None]
        rows = [_row(collection, record) for record in live]
        after = {record.id for record in live}
        before = {
            str(row["record_id"])
            for row in await SyncRecord.select(SyncRecord.record_id).where(
                SyncRecord.collection == collection
            )
        }
        await SyncRecord.delete().where(SyncRecord.collection == collection)
        if rows:
            await SyncRecord.insert(*rows)
        return ApplyResult(
            inserted=len(after - before),
            updated=len(after & before),
            deleted=len(before - after),
        )

    async def _apply(self, collection: str, records: Sequence[EncryptedBso]) -> ApplyResult:
        """增量 upsert：新记录插入、更新的覆盖、墓碑删行。

        增量拉回来的只是**变更集**，所以不能像全量那样"删了重插" ——
        那会把没变更的几千条一起端掉。

        **只在 ``modified`` 更新时才覆盖**：变更集里混进一条旧的（服务器重发、
        两份导出交叉）不能把库里的新数据盖回去。同一批里同 id 出现多次时，
        只有最新的那条算数 —— 库里的唯一索引不接受两行。
        """
        existing = {
            str(row["record_id"]): (int(row["id"]), float(row["modified"]))
            for row in await SyncRecord.select(
                SyncRecord.id, SyncRecord.record_id, SyncRecord.modified
            ).where(SyncRecord.collection == collection)
        }

        fresh: list[SyncRecord] = []
        updates: list[tuple[int, EncryptedBso]] = []
        removals: list[int] = []
        for record in _newest_per_id(records):
            entry = existing.get(record.id)
            if record.payload is None:
                if entry is not None:
                    removals.append(entry[0])
            elif entry is None:
                fresh.append(_row(collection, record))
            elif record.modified > entry[1]:
                updates.append((entry[0], record))

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

    async def replace_collection(self, collection: str, records: Sequence[EncryptedBso]) -> int:
        """用这一批记录整体替换**一个** collection，返回这次落进去多少条（新插 + 覆盖）。"""
        result = await self.store_batches(
            [CollectionBatch(collection=collection, records=records, full=True)]
        )
        applied = result[collection]
        return applied.inserted + applied.updated

    async def load_cursor(self, collection: str) -> float | None:
        """读游标。没有、或者值坏了（不是个数字）都返回 ``None`` —— 调用方回退到全量。

        游标坏了就当没有：全量重拉一次是**安全**的，而拿着一个坏游标往下跑会**静默漏数据**。
        """
        self._bind()
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
        self,
        collection: str,
        *,
        last_modified: float,
        synced_at: float,
        records: int,
    ) -> None:
        """推进游标。**只在一次完整拉取成功之后调**。"""
        self._bind()
        async with self._engine.transaction():
            await SyncCursor.delete().where(SyncCursor.collection == collection)
            await SyncCursor.insert(
                SyncCursor(
                    collection=collection,
                    last_modified=last_modified,
                    synced_at=synced_at,
                    records=records,
                )
            )

    async def load_records(self, collection: str) -> list[tuple[str, str | None]]:
        """读一个 collection 的 ``(record_id, payload)``。"""
        self._bind()
        rows = await SyncRecord.select(SyncRecord.record_id, SyncRecord.payload).where(
            SyncRecord.collection == collection
        )
        return [(str(row["record_id"]), row["payload"]) for row in rows]

    async def count_records(self, collection: str) -> int:
        """库里这个 collection 现在有多少条。"""
        self._bind()
        return await SyncRecord.count().where(SyncRecord.collection == collection)

    async def load_cursors(self) -> tuple[CursorInfo, ...]:
        """所有 collection 的同步进度 —— 没同步过的 collection 不在里面。"""
        self._bind()
        rows = await SyncCursor.select().order_by(SyncCursor.collection)
        return tuple(
            CursorInfo(
                collection=str(row["collection"]),
                last_modified=float(row["last_modified"]),
                synced_at=float(row["synced_at"]),
                records=int(row["records"]),
            )
            for row in rows
        )

    async def store_local_visits(self, visits: Sequence[StoredVisit]) -> ApplyResult:
        """写入本地访问。**幂等** —— 同一份导出再导一次，条数不会翻倍。

        认"同一次访问"靠 ``(machine, url, visited_at)``：那个自增主键在"删了重插"之后会
        重排（实测），拿它当身份会串行。同一批里重复出现的也在这里顺手去重。

        标题变了算 ``updated``（Firefox 会改标题，那是同一次访问，不该多出一行）。
        """
        self._bind()
        existing = {
            (str(row["machine"]), str(row["url"]), int(row["visited_at"])): (
                row["id"],
                str(row["title"]),
            )
            for row in await LocalVisitRow.select(
                LocalVisitRow.id,
                LocalVisitRow.machine,
                LocalVisitRow.url,
                LocalVisitRow.visited_at,
                LocalVisitRow.title,
            )
        }

        fresh: list[LocalVisitRow] = []
        updates: list[tuple[int, str]] = []
        seen: set[tuple[str, str, int]] = set()
        for item in visits:
            key = (item.machine, item.url, to_microseconds(item.visited_at))
            if key in seen:
                continue
            seen.add(key)
            found = existing.get(key)
            if found is None:
                fresh.append(
                    LocalVisitRow(
                        machine=item.machine,
                        url=item.url,
                        title=item.title,
                        visited_at=key[2],
                        visit_type=item.visit_type,
                    )
                )
            elif found[1] != item.title:
                updates.append((found[0], item.title))

        if fresh:
            await LocalVisitRow.insert(*fresh)
        for row_id, title in updates:
            await LocalVisitRow.update({LocalVisitRow.title: title}).where(
                LocalVisitRow.id == row_id
            )

        return ApplyResult(inserted=len(fresh), updated=len(updates), deleted=0)

    async def load_local_visits(self) -> tuple[StoredVisit, ...]:
        """读出全部本地访问，按时间升序。**没导入过就是空元组** —— 单源降级走这条路。"""
        self._bind()
        rows = await LocalVisitRow.select().order_by(LocalVisitRow.visited_at)
        return tuple(
            StoredVisit(
                machine=str(row["machine"]),
                url=str(row["url"]),
                title=str(row["title"]),
                visited_at=from_microseconds(int(row["visited_at"])),
                visit_type=int(row["visit_type"]),
            )
            for row in rows
        )

    async def merge_sync_records(
        self, records: Sequence[PortableRecord]
    ) -> tuple[ApplyResult, int]:
        """把导出来的云端记录并进库。返回 ``(落库的账, 被保住没动的条数)``。

        **只在导出的那条更新时才覆盖。** 目标机器可能自己 sync 过、比这份导出还新 ——
        拿旧数据把新数据盖回去是不可逆的损失，所以这里认 ``modified``，不是无脑 upsert。

        墓碑（``payload`` 为 ``None``）直接跳过：库里的约定是"有行 == 这条记录存在"
        （见本模块开头的说明），收下一条空记录会把这个约定捅破。
        """
        self._bind()
        existing = {
            (str(row["collection"]), str(row["record_id"])): float(row["modified"])
            for row in await SyncRecord.select(
                SyncRecord.collection, SyncRecord.record_id, SyncRecord.modified
            )
        }

        fresh: list[SyncRecord] = []
        updates: list[PortableRecord] = []
        kept = 0
        for item in _newest_per_record(records):
            if item.payload is None:
                kept += 1
                continue
            current = existing.get((item.collection, item.record_id))
            if current is None:
                fresh.append(
                    SyncRecord(
                        collection=item.collection,
                        record_id=item.record_id,
                        modified=item.modified,
                        payload=item.payload,
                        sortindex=item.sortindex,
                        ttl=item.ttl,
                    )
                )
            elif item.modified > current:
                updates.append(item)
            else:
                kept += 1

        if fresh:
            await SyncRecord.insert(*fresh)
        for item in updates:
            await SyncRecord.update(
                {
                    SyncRecord.modified: item.modified,
                    SyncRecord.payload: item.payload,
                    SyncRecord.sortindex: item.sortindex,
                    SyncRecord.ttl: item.ttl,
                }
            ).where(
                (SyncRecord.collection == item.collection)
                & (SyncRecord.record_id == item.record_id)
            )

        return ApplyResult(inserted=len(fresh), updated=len(updates), deleted=0), kept

    async def load_all_records(self) -> tuple[PortableRecord, ...]:
        """库里全部记录（明文形态）—— 导出便携文件用。"""
        self._bind()
        rows = await SyncRecord.select().order_by(SyncRecord.collection, SyncRecord.record_id)
        return tuple(
            PortableRecord(
                collection=str(row["collection"]),
                record_id=str(row["record_id"]),
                modified=float(row["modified"]),
                payload=None if row["payload"] is None else str(row["payload"]),
                sortindex=None if row["sortindex"] is None else int(row["sortindex"]),
                ttl=None if row["ttl"] is None else int(row["ttl"]),
            )
            for row in rows
        )

    async def load_all_cursors(self) -> tuple[PortableCursor, ...]:
        """库里全部游标 —— 导出便携文件用。"""
        self._bind()
        rows = await SyncCursor.select()
        return tuple(
            PortableCursor(
                collection=str(row["collection"]),
                last_modified=float(row["last_modified"]),
                synced_at=float(row["synced_at"]),
                records=int(row["records"]),
            )
            for row in rows
        )

    async def merge_sync_cursors(self, cursors: Sequence[PortableCursor]) -> int:
        """推进游标，返回推进了几个。

        **只往前推。** 旧游标会把已经拉过的区间重拉一遍；更糟的是把"上次同步到哪儿"
        这个判断依据改小 —— 那之后真正的增量就再也不会去拉了。
        """
        advanced = 0
        for item in cursors:
            current = await self.load_cursor(item.collection)
            if current is not None and item.last_modified <= current:
                continue
            await self.save_cursor(
                item.collection,
                last_modified=item.last_modified,
                synced_at=item.synced_at,
                records=item.records,
            )
            advanced += 1
        return advanced


@dataclass(frozen=True, slots=True)
class CursorInfo:
    """一个 collection 的同步进度。"""

    collection: str
    last_modified: float
    """服务器给的 collection 时间戳（下次增量拉取的起点）。"""
    synced_at: float
    """上次同步完成的时间（Unix 秒）。"""
    records: int
    """上次同步之后库里有多少条。"""


def _bind(engine: SQLiteEngine) -> None:
    """把表绑到调用者给的 engine 上。

    piccolo 的表是类级别的单例，"绑哪个库"只能挂在类上 —— 所以每次操作都显式重绑一次，
    测试才能各用各的临时库（见 ``tests/test_store.py``）。
    """
    for table in (LocalVisitRow, SyncRecord, SyncCursor):
        # piccolo 没有"换绑数据库"的公开 API —— 只能碰类的 _meta
        table._meta.db = engine  # pyright: ignore[reportPrivateUsage]


async def open_database(path: Path) -> Store:
    """打开本地库，表不存在就建，返回 :class:`Store`。**不建默认路径** —— 路径由调用者给。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = SQLiteEngine(path=str(path))
    _bind(engine)
    await SyncRecord.create_table(if_not_exists=True)
    await SyncCursor.create_table(if_not_exists=True)
    await LocalVisitRow.create_table(if_not_exists=True)
    await _enforce_record_identity(engine)
    return Store(engine)


_RECORD_IDENTITY_INDEX: Final = "ux_sync_records_collection_record_id"


async def _enforce_record_identity(engine: SQLiteEngine) -> None:
    """让 ``(collection, record_id)`` 真的唯一 —— "库里有行 ⇔ 记录存在"的底座。

    建过就不再动（检查只是一次 ``sqlite_master`` 查询，很便宜）。老库里如果躺着重复行
    （早期版本同一批里同 id 出现两次会插出两行），先收敛：每个
    ``(collection, record_id)`` 只保留 ``modified`` 最新的一条，同值留行号大的。
    **收敛不是悄悄干的** —— 删了几条要报给用户（stderr，人看的通道）。
    """
    existing = await SyncRecord.raw(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = {}",
        _RECORD_IDENTITY_INDEX,
    )
    if existing:
        return
    duplicates = await SyncRecord.raw(
        "SELECT COUNT(*) AS folded FROM ("
        "SELECT id, ROW_NUMBER() OVER ("
        "PARTITION BY collection, record_id ORDER BY modified DESC, id DESC"
        ") AS rank FROM sync_records"
        ") WHERE rank > 1"
    )
    folded = int(duplicates[0]["folded"]) if duplicates else 0
    if folded:
        print(
            f"警告：本地库里有 {folded} 条重复记录（同一个 collection + 同 id）—— "
            f"已收敛，每个 id 只保留 modified 最新的一条。",
            file=sys.stderr,
        )
    await SyncRecord.raw(
        "DELETE FROM sync_records WHERE id IN ("
        "SELECT id FROM ("
        "SELECT id, ROW_NUMBER() OVER ("
        "PARTITION BY collection, record_id ORDER BY modified DESC, id DESC"
        ") AS rank FROM sync_records"
        ") WHERE rank > 1)"
    )
    await SyncRecord.raw(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_RECORD_IDENTITY_INDEX}"
        " ON sync_records (collection, record_id)"
    )


def _newest_per_id(records: Sequence[EncryptedBso]) -> list[EncryptedBso]:
    """同一批里同 id 出现多次时只留最新的（``modified`` 大者胜，平手留后面的）。

    服务器理论上不会这么发，但真发了也不该插出两行 —— 唯一索引会拒绝，
    那是比"静默丢一条"更响的失败，只是没必要走到那一步。
    """
    newest: dict[str, EncryptedBso] = {}
    for record in records:
        current = newest.get(record.id)
        if current is None or record.modified >= current.modified:
            newest[record.id] = record
    return list(newest.values())


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


def _newest_per_record(records: Sequence[PortableRecord]) -> list[PortableRecord]:
    """同一份导出里 ``(collection, record_id)`` 出现多次时只留最新的。

    唯一索引不接受两行同 id —— 别让文件里的重复把整次导入打翻。
    """
    newest: dict[tuple[str, str], PortableRecord] = {}
    for item in records:
        key = (item.collection, item.record_id)
        current = newest.get(key)
        if current is None or item.modified >= current.modified:
            newest[key] = item
    return list(newest.values())
