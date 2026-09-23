"""``ffinfo-cli export`` / ``ffinfo-cli import`` —— 把 firefox 历史在机器之间搬。

**为什么需要这一对命令**（设计文档决策 3）：firefox 的 ``places.sqlite`` 才是"非常大"的那一半
历史（云端同步有 5000 条 / 60 天的硬上限，见 §3.1），但只有装了 Firefox 的机器才有它。
所以在那台机器上 ``export`` 出一份便携文件，拷到目标机器 ``import`` 进去 ——
查询时两个源合并，见 ``list.py``。

分工：

* ``places.py`` 负责"从 Firefox 那儿把数据读出来"（含 WAL 那个坑）
* ``portable.py`` 负责"那份文件长什么样、怎么校验"
* ``store.py`` 负责"并进本地库时怎么不把新数据盖坏"
* 这里只负责把它们串起来，并给出一份能交给 agent 的报告
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from ffinfo_cli import __version__
from ffinfo_cli.places import FirefoxVisit, HostContext, read_firefox_visits
from ffinfo_cli.portable import (
    ExportSource,
    PortableCursor,
    PortableRecord,
    read_portable,
    write_portable,
)
from ffinfo_cli.store import (
    ApplyResult,
    Store,
    StoredVisit,
    open_database,
)

__all__ = [
    "ExportReport",
    "FirefoxImport",
    "ImportInput",
    "ImportReport",
    "PortableImport",
    "run_export",
    "run_import",
]


type ImportInputKind = Literal["portable", "firefox"]
"""``import`` 这次吃的是哪种输入 —— 报告要把它标出来。"""


@dataclass(frozen=True, slots=True)
class PortableImport:
    """``import <便携文件>`` —— ``export`` 产出的那份 SQLite。"""

    path: Path


@dataclass(frozen=True, slots=True)
class FirefoxImport:
    """``import --from-firefox`` —— 直接读本机的 ``places.sqlite``。"""

    host: HostContext
    """这台机器的运行环境（``home`` / ``platform`` / ``env``），由 CLI 注入。"""
    machine: str
    """源机器名。CLI 传主机名 —— 与 ``export`` 同源，同机的同一次访问才对得上。"""
    profile_path: Path | None = None
    """显式指定的 profile 目录（与 ``export --profile`` 同一套发现逻辑）。"""


type ImportInput = PortableImport | FirefoxImport


class ExportReport(BaseModel):
    """一次 export 的结果 —— 直接就是 ``--json`` 的输出。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 1
    destination: str
    machine: str
    profile: str
    profile_path: str
    schema_version: int
    visits: int
    """从 ``places.sqlite`` 读出来多少条访问。"""
    records: int
    """顺带搬走的云端加密记录条数。"""
    cursors: int
    """顺带搬走的同步游标个数。"""
    wal_bytes: int
    """导出时源库还有多少字节没落盘的 WAL。

    **大于 0 说明导出时 Firefox 正开着** —— 那部分数据已经折进快照了（不然就是丢），
    但值得让调用方知道"这份快照是活动库上取的"。
    """
    elapsed_seconds: float


class ImportReport(BaseModel):
    """一次 import 的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 3
    """**3**：加了 ``visits_updated`` —— 三计数恒等 ``inserted + updated + skipped == 输入``。
    **2**：加了 ``input``；``portable_path`` / ``exported_at`` 对 firefox 输入为 null。
    **1**：只有便携文件这一种输入。"""
    input: ImportInputKind
    """这次用的是哪种输入：``portable``（便携文件）· ``firefox``（本机 places.sqlite）。"""
    portable_path: str | None = None
    """便携文件的路径。``input`` 是 ``firefox`` 时为 null —— 那条路径没有文件。"""
    machine: str
    """数据的源机器名 —— 合并两个源时靠它区分。firefox 输入取主机名。"""
    profile: str
    exported_at: str | None = None
    """便携文件里记的导出时刻。``input`` 是 ``firefox`` 时为 null。"""
    visits_inserted: int
    visits_updated: int
    """同一次访问、标题变了 —— 落库时就地改了，不算插入也不算跳过。"""
    visits_skipped: int
    """已经有了、这次没动的。"""
    records_inserted: int
    records_updated: int
    records_kept: int
    """本地那份更新，**没被这份导出盖回去**的条数。firefox 输入没有云端记录，恒为 0。"""
    cursors_advanced: int
    warnings: list[str] = []
    """文件对不上账的地方（不完整、WAL 没带出来）—— **非空就要让人看见**。"""
    elapsed_seconds: float


async def run_export(
    *,
    database_path: Path,
    destination: Path,
    host: HostContext,
    machine: str,
    profile_path: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> ExportReport:
    """在**有 Firefox 的机器**上跑：读 profile 的 ``places.sqlite``，写出便携文件。

    顺带把本地库里的云端记录与游标一起搬走 —— 这样目标机器不用为了同一批数据再拉一次。
    库还不存在（没 login 过）也照样能 export，那就只有 firefox 那部分。
    """
    started = clock()
    collected = read_firefox_visits(host=host, profile_path=profile_path)
    records, cursors = await _cloud_state(database_path)
    meta = write_portable(
        destination,
        source=ExportSource(
            machine=machine,
            profile=collected.profile.name,
            generator=f"ffinfo-cli {__version__}",
            wal_bytes=collected.wal_bytes,
            wal_carried=collected.wal_carried,
        ),
        visits=collected.visits,
        records=records,
        cursors=cursors,
        exported_at=datetime.fromtimestamp(clock(), tz=UTC).isoformat(),
    )

    return ExportReport(
        destination=str(destination),
        machine=machine,
        profile=collected.profile.name,
        profile_path=str(collected.profile.path),
        schema_version=meta.schema_version,
        visits=meta.visits,
        records=meta.sync_records,
        cursors=len(cursors),
        wal_bytes=meta.wal_bytes,
        elapsed_seconds=clock() - started,
    )


async def run_import(
    *,
    database_path: Path,
    input: ImportInput,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> ImportReport:
    """把 firefox 历史并进本地库。**两种输入二选一**（见 :data:`ImportInput`）。

    共通的一件事：firefox 访问按 ``(machine, url, 访问时刻)`` 认，重复导入幂等。

    便携文件那条还多带云端记录与游标，各按各的规矩合并：

    * 云端记录 —— **只在导出的那条更新时才覆盖**，不拿旧数据盖新数据
    * 同步游标 —— **只往前推**

    ``--from-firefox`` 那条**不碰云端记录与游标** —— firefox 那边没有这些，报告里计数为 0。
    ``read_portable`` 发现的告警**原样带进报告**，绝不吞掉：文件不完整、WAL 没带出来，
    这些都得让调用方看见再决定。
    """
    started = clock()

    if isinstance(input, PortableImport):
        portable = read_portable(input.path)
        store = await open_database(database_path, warn=warn)
        visits = await _store_visits(store, portable.meta.machine, portable.visits)
        records, kept = await store.merge_sync_records(portable.records)
        advanced = await store.merge_sync_cursors(portable.cursors)
        return ImportReport(
            input="portable",
            portable_path=str(input.path),
            machine=portable.meta.machine,
            profile=portable.meta.profile,
            exported_at=portable.meta.exported_at,
            visits_inserted=visits.inserted,
            visits_updated=visits.updated,
            visits_skipped=len(portable.visits) - visits.inserted - visits.updated,
            records_inserted=records.inserted,
            records_updated=records.updated,
            records_kept=kept,
            cursors_advanced=advanced,
            warnings=list(portable.warnings),
            elapsed_seconds=clock() - started,
        )

    collected = read_firefox_visits(host=input.host, profile_path=input.profile_path)
    store = await open_database(database_path, warn=warn)
    visits = await _store_visits(store, input.machine, collected.visits)
    return ImportReport(
        input="firefox",
        machine=input.machine,
        profile=collected.profile.name,
        visits_inserted=visits.inserted,
        visits_updated=visits.updated,
        visits_skipped=len(collected.visits) - visits.inserted - visits.updated,
        records_inserted=0,
        records_updated=0,
        records_kept=0,
        cursors_advanced=0,
        elapsed_seconds=clock() - started,
    )


async def _store_visits(store: Store, machine: str, visits: Sequence[FirefoxVisit]) -> ApplyResult:
    """把读出来的 firefox 访问按 ``machine`` 落库 —— 两种输入共用这一段。"""
    return await store.store_firefox_visits(
        [
            StoredVisit(
                machine=machine,
                url=item.url,
                title=item.title,
                visited_at=item.visited_at,
                visit_type=item.visit_type,
            )
            for item in visits
        ]
    )


async def _cloud_state(
    database_path: Path,
) -> tuple[tuple[PortableRecord, ...], tuple[PortableCursor, ...]]:
    """把本地库里的云端状态读出来。库还不存在（没 login / sync 过）就返回空的。

    跳过初始化（不建表、不迁移、不收敛）—— "读出来带走"的命令不该顺手改本地状态。
    """
    if not database_path.is_file():
        return (), ()
    store = await open_database(database_path, read_only=True)
    records = await store.load_all_records()
    cursors = await store.load_all_cursors()
    return records, cursors
