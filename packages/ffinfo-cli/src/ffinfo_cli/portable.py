"""便携文件 —— ``ffinfo-cli export`` 写出去、``ffinfo-cli import`` 收进来的那份 SQLite。

**为什么是 SQLite 而不是 JSON**（设计文档决策 17）：要支持增量 —— 得有 schema 版本、
同步游标、以及够用来校验完整性的计数。

格式是**契约**，不是内部实现：

| 表 | 装什么 |
| --- | --- |
| ``ffinfo_export`` | 元数据：``schema_version`` · 来源 · 导出时间 · WAL 状态 · 计数 |
| ``ffinfo_visits`` | 本地 ``places.sqlite`` 里的每一次访问 |
| ``ffinfo_records`` | 云端拉下来的加密记录（原样，不解密） |
| ``ffinfo_cursors`` | 每个 collection 的同步游标 |

表名统一带 ``ffinfo_`` 前缀，免得跟 Firefox 自己的表（``moz_places`` 之类）撞上。

⚠️ **一件事必须写下来：事后判断不出 ``-wal`` 是不是丢了。** Firefox 正常关闭之后，库照样是
WAL 模式、照样没有 ``-wal`` 文件 —— "WAL 模式 + 没有 -wal" 根本不是证据。所以防线只能在
**导出端**（必须带上附属文件，见 :func:`ffinfo_cli.places.snapshot_places`）。
这里能做的是两件事：把导出当时的 WAL 状态**记进元数据**，以及在这个文件自己**对不上账**时
明确报警（条数不符、schema 版本不认识、压根不是我们的文件）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ffinfo.errors import ConfigurationError
from ffinfo.timestamps import from_microseconds, to_microseconds
from ffinfo_cli.places import LocalVisit

__all__ = [
    "SCHEMA_VERSION",
    "ExportMeta",
    "ExportSource",
    "PortableCursor",
    "PortableFile",
    "PortableRecord",
    "read_portable",
    "write_portable",
]

SCHEMA_VERSION: Final = 1
"""便携文件的格式版本。**改了结构就要往上加** —— 旧版本宁可读不了，也不要读歪。"""

_META_TABLE: Final = "ffinfo_export"
_VISITS_TABLE: Final = "ffinfo_visits"
_RECORDS_TABLE: Final = "ffinfo_records"
_CURSORS_TABLE: Final = "ffinfo_cursors"

_SCHEMA: Final = f"""
CREATE TABLE {_META_TABLE} (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE {_VISITS_TABLE} (
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    visited_at_us INTEGER NOT NULL,
    visit_type INTEGER NOT NULL
);
CREATE TABLE {_RECORDS_TABLE} (
    collection TEXT NOT NULL,
    record_id TEXT NOT NULL,
    modified REAL NOT NULL,
    payload TEXT,
    sortindex INTEGER,
    ttl INTEGER
);
CREATE TABLE {_CURSORS_TABLE} (
    collection TEXT NOT NULL,
    last_modified REAL NOT NULL,
    synced_at REAL NOT NULL,
    records INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ExportSource:
    """这份导出是从哪儿来的 —— 目标机器上翻不出答案的东西，都得记下来。"""

    machine: str
    """源机器的名字。合并两个源时靠它区分"这条是哪台机器看的"。"""

    profile: str
    """Firefox profile 的名字（人看的那个，不是目录名）。"""

    generator: str
    """写出这份文件的程序与版本。"""

    wal_bytes: int = 0
    """导出时源库 ``-wal`` 的字节数。**大于 0 说明导出时 Firefox 正开着**、有没落盘的记录。"""

    wal_carried: bool = False
    """那些 WAL 记录**带出来了没有**。"""


@dataclass(frozen=True, slots=True)
class ExportMeta:
    """``ffinfo_export`` 表里的东西。"""

    schema_version: int
    machine: str
    profile: str
    generator: str
    exported_at: str
    wal_bytes: int
    wal_carried: bool
    visits: int
    sync_records: int
    latest_visit_us: int


@dataclass(frozen=True, slots=True)
class PortableRecord:
    """一条云端记录 —— 加密原文原样搬，不解密。"""

    collection: str
    record_id: str
    modified: float
    payload: str | None
    sortindex: int | None
    ttl: int | None


@dataclass(frozen=True, slots=True)
class PortableCursor:
    """一个 collection 的同步游标。"""

    collection: str
    last_modified: float
    synced_at: float
    records: int


@dataclass(frozen=True, slots=True)
class PortableFile:
    """读出来的一份便携文件。``warnings`` 非空时，收之前得先看一眼。"""

    meta: ExportMeta
    visits: tuple[LocalVisit, ...]
    records: tuple[PortableRecord, ...]
    cursors: tuple[PortableCursor, ...]
    warnings: tuple[str, ...]


def write_portable(
    path: Path,
    *,
    source: ExportSource,
    visits: Sequence[LocalVisit] = (),
    records: Sequence[PortableRecord] = (),
    cursors: Sequence[PortableCursor] = (),
    exported_at: str,
) -> ExportMeta:
    """写一份便携文件。**覆盖写** —— 同名文件直接换掉，别留下上一版的残渣。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    meta = ExportMeta(
        schema_version=SCHEMA_VERSION,
        machine=source.machine,
        profile=source.profile,
        generator=source.generator,
        exported_at=exported_at,
        wal_bytes=source.wal_bytes,
        wal_carried=source.wal_carried,
        visits=len(visits),
        sync_records=len(records),
        latest_visit_us=max((to_microseconds(item.visited_at) for item in visits), default=0),
    )

    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_SCHEMA)
        connection.executemany(
            f"INSERT INTO {_META_TABLE} (key, value) VALUES (?, ?)",
            [
                ("schema_version", str(meta.schema_version)),
                ("machine", meta.machine),
                ("profile", meta.profile),
                ("generator", meta.generator),
                ("exported_at", meta.exported_at),
                ("wal_bytes", str(meta.wal_bytes)),
                ("wal_carried", "1" if meta.wal_carried else "0"),
                ("visits", str(meta.visits)),
                ("sync_records", str(meta.sync_records)),
                ("latest_visit_us", str(meta.latest_visit_us)),
            ],
        )
        connection.executemany(
            f"INSERT INTO {_VISITS_TABLE} (url, title, visited_at_us, visit_type)"
            f" VALUES (?, ?, ?, ?)",
            [
                (item.url, item.title, to_microseconds(item.visited_at), item.visit_type)
                for item in visits
            ],
        )
        connection.executemany(
            f"INSERT INTO {_RECORDS_TABLE}"
            " (collection, record_id, modified, payload, sortindex, ttl)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    item.collection,
                    item.record_id,
                    item.modified,
                    item.payload,
                    item.sortindex,
                    item.ttl,
                )
                for item in records
            ],
        )
        connection.executemany(
            f"INSERT INTO {_CURSORS_TABLE} (collection, last_modified, synced_at, records)"
            f" VALUES (?, ?, ?, ?)",
            [
                (item.collection, item.last_modified, item.synced_at, item.records)
                for item in cursors
            ],
        )
        connection.commit()

    return meta


def read_portable(path: Path) -> PortableFile:
    """读一份便携文件，顺手校验。

    * **读不了**的（不是我们的文件、schema 版本不认识）→ 直接拒收，说清为什么
    * **对不上账**的（条数不符、WAL 没带出来）→ 收下，但把 ``warnings`` 带回去，
      让调用方**必须**决定怎么处理 —— 绝不能静默接受
    """
    if not path.is_file():
        msg = f"{path} 不存在 —— import 要的是 export 产出的那份 SQLite"
        raise ConfigurationError(msg)

    try:
        with closing(_open(path)) as connection:
            _refuse_foreign(connection, path)
            meta = _read_meta(connection)
            visits = _read_visits(connection)
            records = _read_records(connection)
            cursors = _read_cursors(connection)
    except sqlite3.DatabaseError as exc:
        msg = f"{path} 读不了（{exc}）—— 它不像是 ffinfo 导出的文件"
        raise ConfigurationError(msg) from exc

    return PortableFile(
        meta=meta,
        visits=visits,
        records=records,
        cursors=cursors,
        warnings=_warnings(meta, visits, records),
    )


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _refuse_foreign(connection: sqlite3.Connection, path: Path) -> None:
    """不是我们的文件就别硬读 —— 尤其别把 Firefox 的原文件当成我们的导出。"""
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if _META_TABLE in tables:
        return
    if "moz_places" in tables:
        msg = (
            f"{path} 是 Firefox 自己的 places.sqlite 原文件，不是 ffinfo 导出的文件。"
            f"如果你是想直接把它拷过来用：**只拷主文件会丢掉还在 -wal 里的最近记录**"
            f"（Firefox 当时开着的话，那些记录还没落盘）。"
            f"正确做法是在那台机器上跑 `ffinfo-cli export`，再把导出的文件拷过来"
        )
        raise ConfigurationError(msg)
    msg = (
        f"{path} 里没有 {_META_TABLE} 表 —— 它不是 ffinfo 导出的文件（用 `ffinfo-cli export` 生成）"
    )
    raise ConfigurationError(msg)


def _read_meta(connection: sqlite3.Connection) -> ExportMeta:
    raw = {
        str(row["key"]): str(row["value"])
        for row in connection.execute(f"SELECT key, value FROM {_META_TABLE}")
    }
    version = _as_int(raw.get("schema_version"), 0)
    if version != SCHEMA_VERSION:
        msg = (
            f"这份导出的格式版本是 {version}，本程序只认 {SCHEMA_VERSION} —— "
            f"多半是两边版本对不上，各自升级一下再试"
        )
        raise ConfigurationError(msg)

    return ExportMeta(
        schema_version=version,
        machine=raw.get("machine", ""),
        profile=raw.get("profile", ""),
        generator=raw.get("generator", ""),
        exported_at=raw.get("exported_at", ""),
        wal_bytes=_as_int(raw.get("wal_bytes"), 0),
        wal_carried=raw.get("wal_carried") == "1",
        visits=_as_int(raw.get("visits"), 0),
        sync_records=_as_int(raw.get("sync_records"), 0),
        latest_visit_us=_as_int(raw.get("latest_visit_us"), 0),
    )


def _read_visits(connection: sqlite3.Connection) -> tuple[LocalVisit, ...]:
    return tuple(
        LocalVisit(
            url=str(row["url"]),
            title=str(row["title"]),
            visited_at=from_microseconds(int(row["visited_at_us"])),
            visit_type=int(row["visit_type"]),
        )
        for row in connection.execute(
            f"SELECT url, title, visited_at_us, visit_type FROM {_VISITS_TABLE}"
            f" ORDER BY visited_at_us"
        )
    )


def _read_records(connection: sqlite3.Connection) -> tuple[PortableRecord, ...]:
    return tuple(
        PortableRecord(
            collection=str(row["collection"]),
            record_id=str(row["record_id"]),
            modified=float(row["modified"]),
            payload=None if row["payload"] is None else str(row["payload"]),
            sortindex=None if row["sortindex"] is None else int(row["sortindex"]),
            ttl=None if row["ttl"] is None else int(row["ttl"]),
        )
        for row in connection.execute(
            f"SELECT collection, record_id, modified, payload, sortindex, ttl"
            f" FROM {_RECORDS_TABLE} ORDER BY collection, record_id"
        )
    )


def _read_cursors(connection: sqlite3.Connection) -> tuple[PortableCursor, ...]:
    return tuple(
        PortableCursor(
            collection=str(row["collection"]),
            last_modified=float(row["last_modified"]),
            synced_at=float(row["synced_at"]),
            records=int(row["records"]),
        )
        for row in connection.execute(
            f"SELECT collection, last_modified, synced_at, records FROM {_CURSORS_TABLE}"
            f" ORDER BY collection"
        )
    )


def _warnings(
    meta: ExportMeta, visits: tuple[LocalVisit, ...], records: tuple[PortableRecord, ...]
) -> tuple[str, ...]:
    """对不上账的地方 —— 每条都要能让人知道下一步干什么。"""
    found: list[str] = []
    if meta.visits != len(visits):
        found.append(
            f"元数据说有 {meta.visits} 条本地访问，文件里只有 {len(visits)} 条 —— "
            f"这份文件不完整（拷贝中断？还是被改过？）。重新 export 一份更稳妥"
        )
    if meta.sync_records != len(records):
        found.append(
            f"元数据说有 {meta.sync_records} 条同步记录，文件里只有 {len(records)} 条 —— "
            f"这份文件不完整，重新 export 一份更稳妥"
        )
    if meta.wal_bytes > 0 and not meta.wal_carried:
        found.append(
            f"导出时源库还有 {meta.wal_bytes} 字节没落盘的 WAL，而那份 -wal 没带出来 —— "
            f"最近的访问记录缺了。请在那台机器上重跑 export（导出会自动带上 -wal）"
        )
    return tuple(found)


def _as_int(raw: str | None, fallback: int) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return fallback
