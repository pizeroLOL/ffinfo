"""本地 SQLite —— 用 piccolo ORM 落盘。

**这一层不解密。** Sync 拉下来的 ``payload`` 是加密原文，原样进库；解密在 ``list`` 那边。

表按设计文档决策 12 的"双源分表 + 保留来源标记"来切：``sync_records`` 只放云端来的，
firefox ``places.sqlite`` 来的走另一张表 —— 不硬凑成一张。

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

import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Concatenate, Final

from piccolo.columns import BigInt, DoublePrecision, Integer, Text, Varchar
from piccolo.engine.sqlite import SQLiteEngine
from piccolo.table import Table

from ffinfo.errors import ConfigurationError
from ffinfo.storage import EncryptedBso
from ffinfo.timestamps import from_microseconds, to_microseconds
from ffinfo_cli.portable import PortableCursor, PortableRecord

__all__ = [
    "ApplyResult",
    "CollectionBatch",
    "CursorInfo",
    "FirefoxVisitRow",
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


class FirefoxVisitRow(Table, tablename="firefox_visits"):
    """firefox 源的 ``places.sqlite`` 来的一次访问 —— **明文**，没有解密这回事。

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
    """库里的一次 firefox 访问。"""

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


def _translate_sqlite_error(path: str, exc: sqlite3.Error) -> ConfigurationError:
    """sqlite 错误 → ``ConfigurationError`` —— 与 places/portable 同款收敛。"""
    msg = f"{path} 读不了（{exc}）—— 本地库损坏或不是 SQLite 文件"
    return ConfigurationError(msg)


def _guard_store[**StoreParams, StoreResult](
    method: Callable[Concatenate[Store, StoreParams], Awaitable[StoreResult]],
) -> Callable[Concatenate[Store, StoreParams], Awaitable[StoreResult]]:
    """Store 方法上的 ``sqlite3.Error`` → ``ConfigurationError``。

    库损坏时第一发可能落在 :func:`open_database`，也可能落在之后的某次查询
    （``read_only=True`` 开库不摸库）—— 方法这一层也接住，失败才进得了契约。
    """

    @wraps(method)
    async def wrapper(
        self: Store, *args: StoreParams.args, **kwargs: StoreParams.kwargs
    ) -> StoreResult:
        try:
            return await method(self, *args, **kwargs)
        except sqlite3.Error as exc:
            # pyright 抱怨碰了 Store 的 _engine —— 这个 helper 就是为 Store 写的，路径只读
            path = str(self._engine.path)  # pyright: ignore[reportPrivateUsage]
            raise _translate_sqlite_error(path, exc) from exc

    return wrapper


class Store:
    """本地库的 adapter —— **piccolo 只在这个 module 里出现**。

    构造走 :func:`open_database`。piccolo 的表是类级单例，"绑哪个库"只能挂在类上；
    绑定本身仍是进程级的，所以**每条查询**都过 :meth:`_run` 在执行前重绑一次 ——
    「绑定 → ``Query._run`` 捕获 engine」之间没有 await 点，交错的 Store 偷不走
    这一次的目标库（详见 :func:`_run_on`）。
    """

    __slots__: tuple[str, ...] = ("_engine",)

    def __init__(self, engine: SQLiteEngine) -> None:
        """包一把 engine；一般走 :func:`open_database`。"""
        self._engine = engine

    def _bind(self) -> None:
        """把表绑到这把 engine 上 —— 正常路径走 :meth:`_run`，别绕开它直接跑查询。"""
        _bind(self._engine)

    async def _run[T](self, query: Awaitable[T]) -> T:
        """重绑到本 Store 的 engine 后执行一条 piccolo 查询 / DDL。"""
        return await _run_on(self._engine, query)

    async def _insert_rows[T: Table](self, table: type[T], rows: Sequence[T]) -> None:
        """分批 ``INSERT`` —— 绕过 SQLite 的变量数上限。空列表什么都不做。"""
        for start in range(0, len(rows), _INSERT_CHUNK):
            await self._run(table.insert(*rows[start : start + _INSERT_CHUNK]))

    @_guard_store
    async def store_batches(self, batches: Sequence[CollectionBatch]) -> dict[str, ApplyResult]:
        """**一次事务**里写入多个 collection —— 要么全成，要么一个字节都不写。

        为什么要一次事务：``history`` 写进去了、``crypto/keys`` 没写进去，
        库就处于"有数据但解不开"的半截状态 —— 那比什么都没有更让人困惑。
        """
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
            for row in await self._run(
                SyncRecord.select(SyncRecord.record_id).where(SyncRecord.collection == collection)
            )
        }
        await self._run(SyncRecord.delete().where(SyncRecord.collection == collection))
        if rows:
            await self._insert_rows(SyncRecord, rows)
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
            for row in await self._run(
                SyncRecord.select(SyncRecord.id, SyncRecord.record_id, SyncRecord.modified).where(
                    SyncRecord.collection == collection
                )
            )
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
            await self._insert_rows(SyncRecord, fresh)
        # 逐行 UPDATE 是有意的取舍：增量里真正"变了的"通常是个位数，一条条写最直白；
        # 拼一条 CASE 批量得先证明它值得 —— 现在不值。
        for row_id, record in updates:
            await self._run(
                SyncRecord.update(
                    {
                        SyncRecord.modified: record.modified,
                        SyncRecord.payload: record.payload,
                        SyncRecord.sortindex: record.sortindex,
                        SyncRecord.ttl: record.ttl,
                    }
                ).where(SyncRecord.id == row_id)
            )
        if removals:
            await self._run(SyncRecord.delete().where(SyncRecord.id.is_in(removals)))

        return ApplyResult(inserted=len(fresh), updated=len(updates), deleted=len(removals))

    async def replace_collection(self, collection: str, records: Sequence[EncryptedBso]) -> int:
        """用这一批记录整体替换**一个** collection，返回这次落进去多少条（新插 + 覆盖）。"""
        result = await self.store_batches(
            [CollectionBatch(collection=collection, records=records, full=True)]
        )
        applied = result[collection]
        return applied.inserted + applied.updated

    @_guard_store
    async def load_cursor(self, collection: str) -> float | None:
        """读游标。没有、或者值坏了（不是个数字）都返回 ``None`` —— 调用方回退到全量。

        游标坏了就当没有：全量重拉一次是**安全**的，而拿着一个坏游标往下跑会**静默漏数据**。
        """
        rows = await self._run(
            SyncCursor.select(SyncCursor.last_modified).where(SyncCursor.collection == collection)
        )
        if not rows:
            return None
        value = rows[0]["last_modified"]
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @_guard_store
    async def save_cursor(
        self,
        collection: str,
        *,
        last_modified: float,
        synced_at: float,
        records: int,
    ) -> None:
        """推进游标。**只在一次完整拉取成功之后调**。"""
        async with self._engine.transaction():
            await self._run(SyncCursor.delete().where(SyncCursor.collection == collection))
            await self._run(
                SyncCursor.insert(
                    SyncCursor(
                        collection=collection,
                        last_modified=last_modified,
                        synced_at=synced_at,
                        records=records,
                    )
                )
            )

    @_guard_store
    async def load_records(self, collection: str) -> list[tuple[str, str | None]]:
        """读一个 collection 的 ``(record_id, payload)``。"""
        rows = await self._run(
            SyncRecord.select(SyncRecord.record_id, SyncRecord.payload).where(
                SyncRecord.collection == collection
            )
        )
        return [(str(row["record_id"]), row["payload"]) for row in rows]

    @_guard_store
    async def count_records(self, collection: str) -> int:
        """库里这个 collection 现在有多少条。"""
        return await self._run(SyncRecord.count().where(SyncRecord.collection == collection))

    @_guard_store
    async def load_cursors(self) -> tuple[CursorInfo, ...]:
        """所有 collection 的同步进度 —— 没同步过的 collection 不在里面。"""
        rows = await self._run(SyncCursor.select().order_by(SyncCursor.collection))
        return tuple(
            CursorInfo(
                collection=str(row["collection"]),
                last_modified=float(row["last_modified"]),
                synced_at=float(row["synced_at"]),
                records=int(row["records"]),
            )
            for row in rows
        )

    @_guard_store
    async def store_firefox_visits(self, visits: Sequence[StoredVisit]) -> ApplyResult:
        """写入 firefox 源的访问。**幂等** —— 同一份导出再导一次，条数不会翻倍。

        认"同一次访问"靠 ``(machine, url, visited_at)``：那个自增主键在"删了重插"之后会
        重排（实测），拿它当身份会串行。同一批里重复出现的也在这里顺手去重。

        标题变了算 ``updated``（Firefox 会改标题，那是同一次访问，不该多出一行）。
        """
        existing = {
            (str(row["machine"]), str(row["url"]), int(row["visited_at"])): (
                row["id"],
                str(row["title"]),
            )
            for row in await self._run(
                FirefoxVisitRow.select(
                    FirefoxVisitRow.id,
                    FirefoxVisitRow.machine,
                    FirefoxVisitRow.url,
                    FirefoxVisitRow.visited_at,
                    FirefoxVisitRow.title,
                )
            )
        }

        fresh: list[FirefoxVisitRow] = []
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
                    FirefoxVisitRow(
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
            await self._insert_rows(FirefoxVisitRow, fresh)
        for row_id, title in updates:
            await self._run(
                FirefoxVisitRow.update({FirefoxVisitRow.title: title}).where(
                    FirefoxVisitRow.id == row_id
                )
            )

        return ApplyResult(inserted=len(fresh), updated=len(updates), deleted=0)

    @_guard_store
    async def load_firefox_visits(self) -> tuple[StoredVisit, ...]:
        """读出全部 firefox 访问，按时间升序。**没导入过就是空元组** —— 单源降级走这条路。"""
        rows = await self._run(FirefoxVisitRow.select().order_by(FirefoxVisitRow.visited_at))
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

    @_guard_store
    async def merge_sync_records(
        self, records: Sequence[PortableRecord]
    ) -> tuple[ApplyResult, int]:
        """把导出来的云端记录并进库。返回 ``(落库的账, 被保住没动的条数)``。

        **只在导出的那条更新时才覆盖。** 目标机器可能自己 sync 过、比这份导出还新 ——
        拿旧数据把新数据盖回去是不可逆的损失，所以这里认 ``modified``，不是无脑 upsert。

        墓碑（``payload`` 为 ``None``）直接跳过：库里的约定是"有行 == 这条记录存在"
        （见本模块开头的说明），收下一条空记录会把这个约定捅破。
        """
        existing = {
            (str(row["collection"]), str(row["record_id"])): float(row["modified"])
            for row in await self._run(
                SyncRecord.select(SyncRecord.collection, SyncRecord.record_id, SyncRecord.modified)
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
            await self._insert_rows(SyncRecord, fresh)
        for item in updates:
            await self._run(
                SyncRecord.update(
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
            )

        return ApplyResult(inserted=len(fresh), updated=len(updates), deleted=0), kept

    @_guard_store
    async def load_all_records(self) -> tuple[PortableRecord, ...]:
        """库里全部记录（明文形态）—— 导出便携文件用。"""
        rows = await self._run(
            SyncRecord.select().order_by(SyncRecord.collection, SyncRecord.record_id)
        )
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

    @_guard_store
    async def load_all_cursors(self) -> tuple[PortableCursor, ...]:
        """库里全部游标 —— 导出便携文件用。"""
        rows = await self._run(SyncCursor.select())
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

    piccolo 的表是类级别的单例，"绑哪个库"只能挂在类上 —— 绑定本身没有实例级
    的替代品，所以隔离靠**每条查询前重绑**（见 :func:`_run_on`），测试也才能
    各用各的临时库（见 ``tests/test_store.py``）。
    """
    for table in (FirefoxVisitRow, SyncRecord, SyncCursor):
        # piccolo 没有"换绑数据库"的公开 API —— 只能碰类的 _meta
        table._meta.db = engine  # pyright: ignore[reportPrivateUsage]


async def _run_on[T](engine: SQLiteEngine, query: Awaitable[T]) -> T:
    """把表绑到 ``engine``，然后立刻执行 ``query`` —— **每条查询都过这里**。

    为什么不是"每个 Store 方法绑一次"：绑定是进程级类属性，方法中途另一个 Store
    一重绑，后面几条查询就进了别人的库（交错写路径 + ``store_batches`` 事务失护）。

    为什么这样绑就安全：piccolo 的 ``Query._run`` 在**进入协程的第一段同步代码里**
    就读 ``table._meta.db`` 捕获 engine，绑定和捕获之间没有 await 点 —— 单线程
    asyncio 下别的 task 插不进来。查询 await 期间绑确实可能被换掉，但 SQLite 的
    ``_process_results`` 是恒等变换，无所谓。
    """
    _bind(engine)
    return await query


async def open_database(
    path: Path,
    *,
    read_only: bool = False,
    warn: Callable[[str], None] | None = None,
) -> Store:
    """打开本地库，表不存在就建，返回 :class:`Store`。**不建默认路径** —— 路径由调用者给。

    老库里的 ``local_visits`` 并进 ``firefox_visits``（见 :func:`_migrate_firefox_visits`）：
    只有旧表就就地改名，两表并存就搬行再删旧表。**动手之前不建新表** —— 新表一建，
    "只有旧表"那条改名路就再也没机会走。

    ``read_only=True``：跳过建表、迁移与重复行收敛 —— "把数据读出来带走"的命令
    （``export``）不该改本地状态。**这不是连接级只读**：项目的只读保证只覆盖
    Mozilla 与 firefox 源（见 ``docs/design.md`` 决策 5），不覆盖本地库。

    ``warn``：收敛老库里的重复行时往哪儿说。**不注入就没人知道** —— 删除数据这种事
    必须有个去处，命令行那层接的是 stderr。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = SQLiteEngine(path=str(path))
    try:
        if not read_only:
            await _migrate_firefox_visits(engine, warn)
            await _run_on(engine, SyncRecord.create_table(if_not_exists=True))
            await _run_on(engine, SyncCursor.create_table(if_not_exists=True))
            await _run_on(engine, FirefoxVisitRow.create_table(if_not_exists=True))
            await _enforce_record_identity(engine, warn)
    except sqlite3.Error as exc:
        # engine 构造不摸库；损坏文件在这里的第一次查询才炸（read_only 更晚 —— 在 Store 方法上）
        raise _translate_sqlite_error(str(path), exc) from exc
    _bind(engine)
    return Store(engine)


async def _migrate_firefox_visits(engine: SQLiteEngine, warn: Callable[[str], None] | None) -> None:
    """老库里的 ``local_visits`` 并进 ``firefox_visits``，然后删掉旧表。

    **不动就是静默丢数据**：新表空着没人写、旧表没人读，已经导入的 firefox 访问
    在查询里无声消失。两种情形分开处理：

    * **只有旧表**：就地 ``ALTER TABLE … RENAME TO …`` —— 一个字节都不用搬。
    * **两表并存**（半迁移 / 手工改过）：按身份键 ``(machine, url, visited_at)`` 把旧表
      独有的行补进新表，**已存在的不重复插**，然后删掉旧表。这一步**一个事务**完成 ——
      搬一半、旧表又没了，那是最难收拾的状态。搬了多少条经 ``warn`` 报出去，
      删表这种事不能没人知道。
    """
    tables = {
        str(row["name"])
        for row in await _run_on(
            engine,
            FirefoxVisitRow.raw(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name IN ('local_visits', 'firefox_visits')"
            ),
        )
    }
    if "local_visits" not in tables:
        return
    if "firefox_visits" not in tables:
        await _run_on(
            engine, FirefoxVisitRow.raw("ALTER TABLE local_visits RENAME TO firefox_visits")
        )
        return

    async def count_visits() -> int:
        rows = await _run_on(
            engine, FirefoxVisitRow.raw("SELECT COUNT(*) AS n FROM firefox_visits")
        )
        return int(rows[0]["n"])

    moved = 0
    async with engine.transaction():
        before = await count_visits()
        await _run_on(
            engine,
            FirefoxVisitRow.raw(
                "INSERT INTO firefox_visits (machine, url, title, visited_at, visit_type)"
                " SELECT machine, url, title, visited_at, visit_type FROM ("
                "  SELECT machine, url, title, visited_at, visit_type,"
                "   ROW_NUMBER() OVER ("
                "    PARTITION BY machine, url, visited_at ORDER BY id DESC"
                "   ) AS rank FROM local_visits"
                " ) AS old WHERE old.rank = 1 AND NOT EXISTS ("
                "  SELECT 1 FROM firefox_visits AS fresh"
                "  WHERE fresh.machine = old.machine AND fresh.url = old.url"
                "   AND fresh.visited_at = old.visited_at)"
            ),
        )
        moved = await count_visits() - before
        await _run_on(engine, FirefoxVisitRow.raw("DROP TABLE local_visits"))
    if warn is not None:
        warn(
            f"本地库里 local_visits 与 firefox_visits 两张表并存 —— 已把旧表独有的 "
            f"{moved} 条 firefox 访问补进新表，并删掉旧表。"
        )


_RECORD_IDENTITY_INDEX: Final = "ux_sync_records_collection_record_id"


async def _enforce_record_identity(
    engine: SQLiteEngine, warn: Callable[[str], None] | None
) -> None:
    """让 ``(collection, record_id)`` 真的唯一 —— "库里有行 ⇔ 记录存在"的底座。

    建过就不再动（检查只是一次 ``sqlite_master`` 查询，很便宜）。老库里如果躺着重复行
    （早期版本同一批里同 id 出现两次会插出两行），先收敛：每个
    ``(collection, record_id)`` 只保留 ``modified`` 最新的一条，同值留行号大的。
    **收敛不是悄悄干的** —— 删了几条报给 ``warn``（命令行那层接 stderr）。
    """
    existing = await _run_on(
        engine,
        SyncRecord.raw(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = {}",
            _RECORD_IDENTITY_INDEX,
        ),
    )
    if existing:
        return
    duplicates = await _run_on(
        engine,
        SyncRecord.raw(
            "SELECT COUNT(*) AS folded FROM ("
            "SELECT id, ROW_NUMBER() OVER ("
            "PARTITION BY collection, record_id ORDER BY modified DESC, id DESC"
            ") AS rank FROM sync_records"
            ") WHERE rank > 1"
        ),
    )
    folded = int(duplicates[0]["folded"]) if duplicates else 0
    if folded and warn is not None:
        warn(
            f"本地库里有 {folded} 条重复记录（同一个 collection + 同 id）—— "
            f"已收敛，每个 id 只保留 modified 最新的一条。"
        )
    await _run_on(
        engine,
        SyncRecord.raw(
            "DELETE FROM sync_records WHERE id IN ("
            "SELECT id FROM ("
            "SELECT id, ROW_NUMBER() OVER ("
            "PARTITION BY collection, record_id ORDER BY modified DESC, id DESC"
            ") AS rank FROM sync_records"
            ") WHERE rank > 1)"
        ),
    )
    await _run_on(
        engine,
        SyncRecord.raw(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_RECORD_IDENTITY_INDEX}"
            " ON sync_records (collection, record_id)"
        ),
    )


_INSERT_CHUNK: Final = 100
"""一次 ``INSERT`` 最多塞这么多行。

piccolo 把整批拼成一条多值 ``INSERT``，变量数 = 行数 × 列数；SQLite 的
``SQLITE_MAX_VARIABLE_NUMBER`` 老版本只有 **999**（新版 32766），上万条真实历史
就会撞上 ``too many SQL variables``。按最保守的 999 除以最宽的表（7 列）留足余量取 100。
"""


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
