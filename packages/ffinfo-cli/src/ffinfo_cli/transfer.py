"""``ffinfo-cli export`` / ``ffinfo-cli import`` —— 把本地历史在机器之间搬。

**为什么需要这一对命令**（设计文档决策 3）：本地 ``places.sqlite`` 才是"非常大"的那一半
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

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ffinfo_cli import __version__
from ffinfo_cli.places import PLACES_FILENAME, find_profile, read_visits, snapshot_places
from ffinfo_cli.portable import (
    ExportSource,
    PortableCursor,
    PortableRecord,
    read_portable,
    write_portable,
)
from ffinfo_cli.store import (
    StoredVisit,
    open_database,
)

__all__ = [
    "ExportReport",
    "ImportReport",
    "export_blocking",
    "import_blocking",
    "run_export",
    "run_import",
]


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

    def to_json(self) -> str:
        """给 agent 消费的 JSON。"""
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


class ImportReport(BaseModel):
    """一次 import 的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 1
    source: str
    machine: str
    """导出那台机器的名字 —— 合并两个源时靠它区分。"""
    profile: str
    exported_at: str
    visits_inserted: int
    visits_skipped: int
    """已经有了、这次没动的。"""
    records_inserted: int
    records_updated: int
    records_kept: int
    """本地那份更新，**没被这份导出盖回去**的条数。"""
    cursors_advanced: int
    warnings: list[str] = []
    """文件对不上账的地方（不完整、WAL 没带出来）—— **非空就要让人看见**。"""
    elapsed_seconds: float

    def to_json(self) -> str:
        """给 agent 消费的 JSON。"""
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


async def run_export(
    *,
    database_path: Path,
    destination: Path,
    home: Path,
    platform: str,
    env: Mapping[str, str],
    machine: str,
    profile_path: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> ExportReport:
    """在**有 Firefox 的机器**上跑：读 profile 的 ``places.sqlite``，写出便携文件。

    顺带把本地库里的云端记录与游标一起搬走 —— 这样目标机器不用为了同一批数据再拉一次。
    库还不存在（没 login 过）也照样能 export，那就只有本地那部分。
    """
    started = clock()
    profile = find_profile(home=home, platform=platform, env=env, explicit=profile_path)

    with TemporaryDirectory(prefix="ffinfo-export-") as workdir:
        snapshot = snapshot_places(profile.path / PLACES_FILENAME, into=Path(workdir))
        visits = read_visits(snapshot.database)
        records, cursors = await _cloud_state(database_path)
        meta = write_portable(
            destination,
            source=ExportSource(
                machine=machine,
                profile=profile.name,
                generator=f"ffinfo-cli {__version__}",
                wal_bytes=snapshot.wal_bytes,
                wal_carried="-wal" in snapshot.sidecars,
            ),
            visits=visits,
            records=records,
            cursors=cursors,
            exported_at=datetime.fromtimestamp(clock(), tz=UTC).isoformat(),
        )

    return ExportReport(
        destination=str(destination),
        machine=machine,
        profile=profile.name,
        profile_path=str(profile.path),
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
    source: Path,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> ImportReport:
    """在**目标机器**上跑：把便携文件并进本地库。

    三样东西各按各的规矩合并：

    * 本地访问 —— 按 ``(机器, url, 访问时刻)`` 认，重复导入幂等
    * 云端记录 —— **只在导出的那条更新时才覆盖**，不拿旧数据盖新数据
    * 同步游标 —— **只往前推**

    ``read_portable`` 发现的告警**原样带进报告**，绝不吞掉：文件不完整、WAL 没带出来，
    这些都得让调用方看见再决定。
    """
    started = clock()
    portable = read_portable(source)
    store = await open_database(database_path, warn=warn)

    visits = await store.store_local_visits(
        [
            StoredVisit(
                machine=portable.meta.machine,
                url=item.url,
                title=item.title,
                visited_at=item.visited_at,
                visit_type=item.visit_type,
            )
            for item in portable.visits
        ],
    )
    records, kept = await store.merge_sync_records(portable.records)
    advanced = await store.merge_sync_cursors(portable.cursors)

    return ImportReport(
        source=str(source),
        machine=portable.meta.machine,
        profile=portable.meta.profile,
        exported_at=portable.meta.exported_at,
        visits_inserted=visits.inserted,
        visits_skipped=len(portable.visits) - visits.inserted - visits.updated,
        records_inserted=records.inserted,
        records_updated=records.updated,
        records_kept=kept,
        cursors_advanced=advanced,
        warnings=list(portable.warnings),
        elapsed_seconds=clock() - started,
    )


async def _cloud_state(
    database_path: Path,
) -> tuple[tuple[PortableRecord, ...], tuple[PortableCursor, ...]]:
    """把本地库里的云端状态读出来。库还不存在（没 login / sync 过）就返回空的。

    **只读打开** —— "读出来带走"的命令不该顺手建表、更不该顺手收敛重复行。
    """
    if not database_path.is_file():
        return (), ()
    store = await open_database(database_path, read_only=True)
    records = await store.load_all_records()
    cursors = await store.load_all_cursors()
    return records, cursors


def export_blocking(
    *,
    database_path: Path,
    destination: Path,
    home: Path,
    platform: str,
    env: Mapping[str, str],
    machine: str,
    profile_path: Path | None = None,
) -> ExportReport:
    """:func:`run_export` 的同步外壳。"""
    return asyncio.run(
        run_export(
            database_path=database_path,
            destination=destination,
            home=home,
            platform=platform,
            env=env,
            machine=machine,
            profile_path=profile_path,
        )
    )


def import_blocking(
    *, database_path: Path, source: Path, warn: Callable[[str], None] | None = None
) -> ImportReport:
    """:func:`run_import` 的同步外壳。"""
    return asyncio.run(run_import(database_path=database_path, source=source, warn=warn))
