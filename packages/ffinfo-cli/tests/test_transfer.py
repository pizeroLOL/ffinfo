"""export / import 的编排。

这里测的是**端到端的那条路**：源机器有 Firefox → ``export`` 出便携文件 →
拷到目标机器 → ``import`` 进本地库 → ``list`` 查得到。中间每个零件都有自己的
测试（``test_places*`` / ``test_portable``），这里只关心它们接起来对不对。
"""

# 上面三行：同上 —— 测试直接查表类验证落库结果。
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownParameterType=false
# pyright: reportUnknownLambdaType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ffinfo.errors import ConfigurationError
from ffinfo.storage import EncryptedBso
from ffinfo_cli.places import FirefoxVisit
from ffinfo_cli.portable import (
    ExportSource,
    PortableCursor,
    PortableRecord,
    read_portable,
    write_portable,
)
from ffinfo_cli.render import render
from ffinfo_cli.store import (
    CollectionBatch,
    SyncCursor,
    SyncRecord,
    open_database,
)
from ffinfo_cli.transfer import (
    ExportReport,
    FirefoxImport,
    PortableImport,
    export_blocking,
    import_blocking,
    run_export,
    run_import,
)
from support import US, add_visits, build_places, host
from support import us_of as micros

DAY = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
MOMENT = "2026-09-14T02:00:00+00:00"


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
) -> ExportReport:
    return await run_export(
        database_path=database if database is not None else home / "ffinfo.sqlite",
        destination=destination,
        host=host(home),
        machine="test-laptop",
        **kwargs,  # type: ignore[arg-type]
    )


async def test_export_reads_the_profile_and_writes_a_portable_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile_with_firefox(
        home,
        [
            ("https://a.example/", "A", micros(DAY), 1),
            ("https://b.example/", "B", micros(DAY) + 60 * US, 2),
        ],
    )

    report = await export_from(home, tmp_path / "out" / "portable.sqlite")

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
    store = await open_database(database)
    await store.store_batches(
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


async def test_export_does_not_touch_the_local_database(tmp_path: Path) -> None:
    """export 是"读出来带走"：老库里的重复行不该被它收敛，索引也不该被它补上。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])
    database = tmp_path / "legacy.sqlite"
    store = await open_database(database)
    await store.store_batches(
        [CollectionBatch(collection="history", records=[record("a")], full=True)],
    )
    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX ux_sync_records_collection_record_id")
    connection.execute(
        "INSERT INTO sync_records (collection, record_id, modified, payload) "
        "VALUES ('history', 'a', 99.0, 'duplicate')"
    )
    connection.commit()
    connection.close()

    await export_from(home, tmp_path / "portable.sqlite", database=database)

    connection = sqlite3.connect(database)
    duplicated = connection.execute(
        "SELECT COUNT(*) FROM sync_records WHERE collection = 'history' AND record_id = 'a'"
    ).fetchone()
    indexes = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
        "AND name = 'ux_sync_records_collection_record_id'"
    ).fetchone()
    connection.close()
    assert duplicated == (2,)  # 重复行原样留着 —— 收敛是开库命令的事，不是 export 的
    assert indexes == (0,)


async def test_export_works_without_any_local_database(tmp_path: Path) -> None:
    """只装过 Firefox、还没 login/sync 的机器也要能 export。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])

    report = await export_from(home, tmp_path / "portable.sqlite")

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

    report = await export_from(home, tmp_path / "portable.sqlite")
    payload = json.loads(render(report, machine=True))

    assert payload["destination"].endswith("portable.sqlite")
    assert payload["machine"] == "test-laptop"
    assert payload["profile"] == "default-release"
    assert payload["schema_version"] == 1
    assert payload["visits"] == 1


def portable_file(path: Path, *, visits: list[tuple[str, int]], machine: str = "src") -> None:
    write_portable(
        path,
        source=ExportSource(machine=machine, profile="default-release", generator="test"),
        visits=[
            FirefoxVisit(
                url=url,
                title="T",
                visited_at=datetime.fromtimestamp(us // US, tz=UTC).replace(microsecond=us % US),
                visit_type=1,
            )
            for url, us in visits
        ],
        exported_at=MOMENT,
    )


async def test_import_lands_in_the_firefox_table(tmp_path: Path) -> None:
    portable_file(tmp_path / "portable.sqlite", visits=[("https://a.example/", micros(DAY))])

    report = await run_import(
        database_path=tmp_path / "db.sqlite",
        input=PortableImport(path=tmp_path / "portable.sqlite"),
    )

    assert report.input == "portable"
    assert report.format_version == 2
    assert report.visits_inserted == 1
    assert report.machine == "src"
    assert report.warnings == []


async def test_import_twice_does_not_duplicate(tmp_path: Path) -> None:
    """**增量导入**：同一份导出再导一次，只处理新增的部分。"""
    portable_file(tmp_path / "portable.sqlite", visits=[("https://a.example/", micros(DAY))])
    portable = PortableImport(path=tmp_path / "portable.sqlite")

    await run_import(database_path=tmp_path / "db.sqlite", input=portable)
    second = await run_import(database_path=tmp_path / "db.sqlite", input=portable)

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

    report = await run_import(
        database_path=tmp_path / "db.sqlite", input=PortableImport(path=source)
    )

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
    store = await open_database(database)
    await store.store_batches(
        [CollectionBatch(collection="history", records=[record("rec", modified=99.0)], full=True)],
    )
    await SyncCursor.insert(
        SyncCursor(collection="history", last_modified=500.0, synced_at=501.0, records=1)
    )

    await run_import(database_path=database, input=PortableImport(path=source))

    rows = await SyncRecord.select().where(SyncRecord.record_id == "rec")
    assert float(rows[0]["modified"]) == 99.0
    assert await store.load_cursor("history") == 500.0


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


async def test_import_from_firefox_lands_in_the_table(tmp_path: Path) -> None:
    """`import --from-firefox` 直连本机 profile —— 不用先 export 一次。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])

    report = await run_import(
        database_path=tmp_path / "db.sqlite",
        input=FirefoxImport(host=host(home), machine="test-laptop"),
    )

    assert report.input == "firefox"
    assert report.visits_inserted == 1
    assert report.machine == "test-laptop"
    assert report.profile == "default-release"
    assert report.portable_path is None
    assert report.exported_at is None
    assert report.records_inserted == 0
    assert report.cursors_advanced == 0
    payload = json.loads(render(report, machine=True))
    assert payload["input"] == "firefox"
    assert payload["portable_path"] is None
    assert payload["exported_at"] is None
    store = await open_database(tmp_path / "db.sqlite")
    stored = await store.load_firefox_visits()
    assert [item.url for item in stored] == ["https://a.example/"]
    assert stored[0].machine == "test-laptop"


async def test_import_from_firefox_is_idempotent(tmp_path: Path) -> None:
    """同机再跑一次 —— 靠 ``(machine, url, 访问时刻)`` 去重，不翻倍。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])
    database = tmp_path / "db.sqlite"
    source = FirefoxImport(host=host(home), machine="test-laptop")

    first = await run_import(database_path=database, input=source)
    second = await run_import(database_path=database, input=source)

    assert first.visits_inserted == 1
    assert second.visits_inserted == 0
    assert second.visits_skipped == 1
    store = await open_database(database)
    assert len(await store.load_firefox_visits()) == 1


async def test_import_from_firefox_takes_an_explicit_profile(tmp_path: Path) -> None:
    """``--profile <目录>`` 与 export 走同一套发现逻辑。"""
    profile = tmp_path / "elsewhere" / "my.profile"
    build_places(profile / "places.sqlite", [("https://a.example/", "A", micros(DAY), 1)]).close()

    report = await run_import(
        database_path=tmp_path / "db.sqlite",
        input=FirefoxImport(
            host=host(tmp_path / "home"),
            machine="test-laptop",
            profile_path=profile,
        ),
    )

    assert report.profile == "my.profile"
    assert report.visits_inserted == 1


async def test_import_from_firefox_keeps_visits_that_live_in_the_wal(tmp_path: Path) -> None:
    """直连导入走 02 那条 WAL 安全的公共路径 —— 停在 ``-wal`` 里的最近访问也要进来。"""
    home = tmp_path / "home"
    root = home / ".mozilla" / "firefox"
    connection = build_places(
        root / "abc123.default-release" / "places.sqlite",
        [("https://old.example/", "Old", micros(DAY), 1)],
        wal=True,
    )
    add_visits(connection, [("https://new.example/", "New", micros(DAY) + 60 * US, 1)])
    connection.commit()
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=default-release\nIsRelative=1\nPath=abc123.default-release\nDefault=1\n",
        encoding="utf-8",
    )
    try:
        report = await run_import(
            database_path=tmp_path / "db.sqlite",
            input=FirefoxImport(host=host(home), machine="test-laptop"),
        )
    finally:
        connection.close()

    assert report.visits_inserted == 2


async def test_import_from_firefox_leaves_cloud_records_and_cursors_alone(tmp_path: Path) -> None:
    """firefox 那边没有云端记录/游标 —— 直连导入不许碰库里已有的那部分。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])
    database = tmp_path / "db.sqlite"
    store = await open_database(database)
    await store.store_batches(
        [CollectionBatch(collection="history", records=[record("rec")], full=True)],
    )
    await SyncCursor.insert(
        SyncCursor(collection="history", last_modified=500.0, synced_at=501.0, records=1)
    )

    report = await run_import(
        database_path=database,
        input=FirefoxImport(host=host(home), machine="test-laptop"),
    )

    assert report.records_inserted == 0
    assert report.cursors_advanced == 0
    assert [row["record_id"] for row in await SyncRecord.select()] == ["rec"]
    assert await store.load_cursor("history") == 500.0


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
    report = await run_import(
        database_path=target_home / "db.sqlite", input=PortableImport(path=portable)
    )

    assert report.visits_inserted == 2
    store = await open_database(target_home / "db.sqlite")
    stored = await store.load_firefox_visits()
    assert [item.url for item in stored] == ["https://a.example/", "https://b.example/"]
    assert stored[0].machine == "test-laptop"


def test_blocking_wrappers_are_usable_from_sync_code(tmp_path: Path) -> None:
    """CLI 调的是这两个同步外壳 —— 它们自己开事件循环，不能是协程。"""
    home = tmp_path / "home"
    profile_with_firefox(home, [("https://a.example/", "A", micros(DAY), 1)])

    exported = export_blocking(
        database_path=home / "ffinfo.sqlite",
        destination=tmp_path / "portable.sqlite",
        host=host(home),
        machine="test-laptop",
    )
    imported = import_blocking(
        database_path=tmp_path / "target.sqlite",
        input=PortableImport(path=tmp_path / "portable.sqlite"),
    )

    assert exported.visits == 1
    assert imported.visits_inserted == 1
