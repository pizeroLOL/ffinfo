"""export / import 的编排（08 号 ticket）。

这里测的是**端到端的那条路**：源机器有 Firefox → ``export`` 出便携文件 →
拷到目标机器 → ``import`` 进本地库 → ``list`` 查得到。中间每个零件都有自己的
测试（``test_places*`` / ``test_portable``），这里只关心它们接起来对不对。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ffinfo.errors import ConfigurationError
from ffinfo.storage import EncryptedBso
from ffinfo_cli.places import LocalVisit
from ffinfo_cli.portable import (
    ExportSource,
    PortableCursor,
    PortableRecord,
    read_portable,
    write_portable,
)
from ffinfo_cli.store import (
    CollectionBatch,
    SyncCursor,
    SyncRecord,
    load_cursor,
    load_local_visits,
    open_database,
    store_batches,
)
from ffinfo_cli.transfer import export_blocking, import_blocking, run_export, run_import

US = 1_000_000
DAY = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
MOMENT = "2026-09-14T02:00:00+00:00"
SCHEMA = """
CREATE TABLE moz_places (
    id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR,
    visit_count INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0, typed INTEGER DEFAULT 0
);
CREATE TABLE moz_historyvisits (
    id INTEGER PRIMARY KEY, from_visit INTEGER, place_id INTEGER,
    visit_date INTEGER, visit_type INTEGER, session INTEGER
);
"""


def micros(moment: datetime) -> int:
    return int(moment.timestamp() * US)


def build_places(path: Path, visits: list[tuple[str, str | None, int, int]]) -> sqlite3.Connection:
    """造一个最小但结构真实的 places.sqlite。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    places: dict[str, int] = {}
    for url, title, visit_date, visit_type in visits:
        if url not in places:
            place_id = len(places) + 1
            places[url] = place_id
            connection.execute(
                "INSERT INTO moz_places (id, url, title) VALUES (?, ?, ?)", (place_id, url, title)
            )
        connection.execute(
            "INSERT INTO moz_historyvisits (place_id, visit_date, visit_type) VALUES (?, ?, ?)",
            (places[url], visit_date, visit_type),
        )
    connection.commit()
    return connection


def profile_with_firefox(home: Path, visits: list[tuple[str, str | None, int, int]]) -> Path:
    """造一个"这台机器装过 Firefox"的家目录，返回 profile 目录。"""
    root = home / ".mozilla" / "firefox"
    profile = root / "abc123.default-release"
    build_places(profile / "places.sqlite", visits).close()
    root.mkdir(parents=True, exist_ok=True)
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=default-release\nIsRelative=1\nPath=abc123.default-release\nDefault=1\n",
        encoding="utf-8",
    )
    return profile


async def export_from(
    home: Path, destination: Path, *, database: Path | None = None, **kwargs: object
) -> object:
    return await run_export(
        database_path=database if database is not None else home / "ffinfo.sqlite",
        destination=destination,
        home=home,
        platform="linux",
        env={},
        machine="test-laptop",
        **kwargs,  # type: ignore[arg-type]
    )


# ── export ────────────────────────────────────────────────────────────────


async def test_export_reads_the_profile_and_writes_a_portable_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile_with_firefox(
        home,
        [
            ("https://a.example/", "A", micros(DAY), 1),
            ("https://b.example/", "B", micros(DAY) + 60 * US, 2),
        ],
    )

    report = await export_from(home, tmp_path / "out" / "portable.sqlite")  # type: ignore[assignment]

    portable = read_portable(tmp_path / "out" / "portable.sqlite")
    assert [item.url for item in portable.visits] == ["https://a.example/", "https://b.example/"]
    assert portable.meta.machine == "test-laptop"
    assert portable.meta.profile == "default-release"
    assert portable.meta.wal_carried is False  # Firefox 关着，本来就没有 WAL
    assert report.visits == 2


async def test_export_carries_the_cloud_records_and_cursors(tmp_path: Path) -> None:
    """便携文件要能顶一次 sync —— 加密记录和游标一起走（决策 17）。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])
    database = tmp_path / "ffinfo.sqlite"
    engine = await open_database(database)
    await store_batches(
        engine,
        [CollectionBatch(collection="history", records=[record("rec-1")], full=True)],
    )
    await SyncCursor.insert(
        SyncCursor(collection="history", last_modified=42.5, synced_at=43.0, records=1)
    )

    await export_from(home, tmp_path / "portable.sqlite", database=database)

    portable = read_portable(tmp_path / "portable.sqlite")
    assert [item.record_id for item in portable.records] == ["rec-1"]
    assert [item.collection for item in portable.cursors] == ["history"]
    assert portable.cursors[0].last_modified == 42.5


async def test_export_works_without_any_local_database(tmp_path: Path) -> None:
    """只装过 Firefox、还没 login/sync 的机器也要能 export。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])

    report = await export_from(home, tmp_path / "portable.sqlite")  # type: ignore[assignment]

    portable = read_portable(tmp_path / "portable.sqlite")
    assert portable.records == ()
    assert portable.cursors == ()
    assert report.visits == 1


async def test_export_without_firefox_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        await export_from(tmp_path / "empty-home", tmp_path / "portable.sqlite")

    assert "--profile" in str(caught.value)


async def test_export_report_says_what_happened(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])

    report = await export_from(home, tmp_path / "portable.sqlite")  # type: ignore[assignment]
    payload = json.loads(report.to_json())

    assert payload["destination"].endswith("portable.sqlite")
    assert payload["machine"] == "test-laptop"
    assert payload["profile"] == "default-release"
    assert payload["schema_version"] == 1
    assert payload["visits"] == 1


# ── import ────────────────────────────────────────────────────────────────


def portable_file(path: Path, *, visits: list[tuple[str, int]], machine: str = "src") -> None:
    write_portable(
        path,
        source=ExportSource(machine=machine, profile="default-release", generator="test"),
        visits=[
            LocalVisit(
                url=url,
                title="T",
                visited_at=datetime.fromtimestamp(us // US, tz=UTC).replace(microsecond=us % US),
                visit_type=1,
            )
            for url, us in visits
        ],
        exported_at=MOMENT,
    )


async def test_import_lands_in_the_local_table(tmp_path: Path) -> None:
    portable_file(tmp_path / "portable.sqlite", visits=[("https://a.example/", micros(DAY))])

    report = await run_import(
        database_path=tmp_path / "db.sqlite", source=tmp_path / "portable.sqlite"
    )

    assert report.visits_inserted == 1
    assert report.machine == "src"
    assert report.warnings == []


async def test_import_twice_does_not_duplicate(tmp_path: Path) -> None:
    """**增量导入**：同一份导出再导一次，只处理新增的部分。"""
    portable_file(tmp_path / "portable.sqlite", visits=[("https://a.example/", micros(DAY))])

    await run_import(database_path=tmp_path / "db.sqlite", source=tmp_path / "portable.sqlite")
    second = await run_import(
        database_path=tmp_path / "db.sqlite", source=tmp_path / "portable.sqlite"
    )

    assert second.visits_inserted == 0
    assert second.visits_skipped == 1


async def test_import_surfaces_warnings_instead_of_swallowing_them(tmp_path: Path) -> None:
    """文件对不上账 —— import 必须把它带出来，不能默默收下。"""
    source = tmp_path / "portable.sqlite"
    portable_file(source, visits=[("https://a.example/", micros(DAY))])
    connection = sqlite3.connect(source)
    connection.execute("DELETE FROM ffinfo_visits")
    connection.commit()
    connection.close()

    report = await run_import(database_path=tmp_path / "db.sqlite", source=source)

    assert len(report.warnings) == 1
    assert "不完整" in report.warnings[0]


async def test_import_does_not_downgrade_newer_cloud_data(tmp_path: Path) -> None:
    """目标机器自己 sync 过、比这份导出还新 —— 别被旧数据覆盖回去。"""
    source = tmp_path / "portable.sqlite"
    write_portable(
        source,
        source=ExportSource(machine="src", profile="p", generator="test"),
        records=[portable_record("rec", modified=1.0)],
        cursors=[
            PortableCursor(collection="history", last_modified=10.0, synced_at=11.0, records=1)
        ],
        exported_at=MOMENT,
    )
    database = tmp_path / "db.sqlite"
    engine = await open_database(database)
    await store_batches(
        engine,
        [CollectionBatch(collection="history", records=[record("rec", modified=99.0)], full=True)],
    )
    await SyncCursor.insert(
        SyncCursor(collection="history", last_modified=500.0, synced_at=501.0, records=1)
    )

    await run_import(database_path=database, source=source)

    rows = await SyncRecord.select().where(SyncRecord.record_id == "rec")
    assert float(rows[0]["modified"]) == 99.0
    assert await load_cursor(engine, "history") == 500.0


def record(record_id: str, *, modified: float = 1.0) -> EncryptedBso:
    return EncryptedBso(id=record_id, modified=modified, payload="encrypted")


def portable_record(record_id: str, *, modified: float) -> PortableRecord:
    return PortableRecord(
        collection="history",
        record_id=record_id,
        modified=modified,
        payload="encrypted",
        sortindex=None,
        ttl=None,
    )


# ── 端到端 ────────────────────────────────────────────────────────────────


async def test_export_then_import_then_query(tmp_path: Path) -> None:
    """源机器 export → 目标机器 import → list 查得到。这是这张票的验收面。"""
    source_home = tmp_path / "source"
    profile_with_firefox(
        source_home,
        [
            ("https://a.example/", "A", micros(DAY), 1),
            ("https://b.example/", "B", micros(DAY) + 3600 * US, 1),
        ],
    )
    portable = tmp_path / "portable.sqlite"
    await export_from(source_home, portable)

    target_home = tmp_path / "target"
    target_home.mkdir()
    report = await run_import(database_path=target_home / "db.sqlite", source=portable)

    assert report.visits_inserted == 2
    engine = await open_database(target_home / "db.sqlite")
    stored = await load_local_visits(engine)
    assert [item.url for item in stored] == ["https://a.example/", "https://b.example/"]
    assert stored[0].machine == "test-laptop"


def test_blocking_wrappers_are_usable_from_sync_code(tmp_path: Path) -> None:
    """CLI 调的是这两个同步外壳 —— 它们自己开事件循环，不能是协程。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])

    exported = export_blocking(
        database_path=home / "ffinfo.sqlite",
        destination=tmp_path / "portable.sqlite",
        home=home,
        platform="linux",
        env={},
        machine="test-laptop",
    )
    imported = import_blocking(
        database_path=tmp_path / "target.sqlite", source=tmp_path / "portable.sqlite"
    )

    assert exported.visits == 1
    assert imported.visits_inserted == 1
