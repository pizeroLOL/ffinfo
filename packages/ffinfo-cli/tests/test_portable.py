"""便携文件：export 写出去的那份 SQLite。

格式是**契约**：里面有 ``schema_version``、来源信息、以及够用来校验完整性的计数。
读的一方（``import``）负责在数据不完整时**明确说话**，而不是默默收下。

⚠️ 一件必须写下来的事：**"事后"判断不出 ``-wal`` 是不是丢了**。Firefox 正常关闭之后
库照样是 WAL 模式、照样没有 ``-wal`` 文件 —— 所以"WAL 模式 + 没有 -wal"根本不能当证据。
防线只能在**导出端**（必须把附属文件带上，见 ``places.snapshot_places``），
这里能做的是：把导出当时的 WAL 状态**记下来**，并在文件本身对不上账时报警。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ffinfo.errors import ConfigurationError
from ffinfo_cli.places import FirefoxVisit
from ffinfo_cli.portable import (
    SCHEMA_VERSION,
    ExportSource,
    PortableCursor,
    PortableRecord,
    read_portable,
    write_portable,
)

US = 1_000_000
MOMENT = "2026-09-14T02:00:00+00:00"


def visit(url: str, *, seconds: int = 1_700_000_000, title: str = "T") -> FirefoxVisit:
    return FirefoxVisit(
        url=url,
        title=title,
        visited_at=datetime.fromtimestamp(seconds, tz=UTC),
        visit_type=1,
    )


def source(**overrides: object) -> ExportSource:
    base = {
        "machine": "test-laptop",
        "profile": "default-release",
        "generator": "ffinfo-cli 0.1.0",
        "wal_bytes": 40960,
        "wal_carried": True,
    }
    return ExportSource(**{**base, **overrides})  # type: ignore[arg-type]


def record(record_id: str = "abc", *, payload: str | None = "encrypted") -> PortableRecord:
    return PortableRecord(
        collection="history",
        record_id=record_id,
        modified=1.5,
        payload=payload,
        sortindex=None,
        ttl=None,
    )


def cursor() -> PortableCursor:
    return PortableCursor(collection="history", last_modified=1.5, synced_at=2.0, records=1)


def write(path: Path, **kwargs: object) -> None:
    defaults: dict[str, object] = {
        "source": source(),
        "visits": [visit("https://a.example/")],
        "records": [record()],
        "cursors": [cursor()],
        "exported_at": MOMENT,
    }
    write_portable(path, **{**defaults, **kwargs})  # type: ignore[arg-type]


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "portable.sqlite"

    meta = write_portable(
        path,
        source=source(),
        visits=[visit("https://a.example/"), visit("https://b.example/", seconds=1_700_000_060)],
        records=[record("abc"), record("def")],
        cursors=[cursor()],
        exported_at=MOMENT,
    )

    portable = read_portable(path)

    assert portable.warnings == ()
    assert meta.visits == 2
    assert meta.sync_records == 2
    assert [item.url for item in portable.visits] == [
        "https://a.example/",
        "https://b.example/",
    ]
    assert [item.record_id for item in portable.records] == ["abc", "def"]
    assert portable.cursors == (cursor(),)


def test_meta_describes_where_it_came_from(tmp_path: Path) -> None:
    path = tmp_path / "portable.sqlite"

    write(path)

    meta = read_portable(path).meta

    assert meta.schema_version == SCHEMA_VERSION
    assert meta.machine == "test-laptop"
    assert meta.profile == "default-release"
    assert meta.generator == "ffinfo-cli 0.1.0"
    assert meta.wal_bytes == 40960
    assert meta.exported_at == MOMENT
    assert meta.latest_visit_us == 1_700_000_000 * US


def test_empty_export_round_trips(tmp_path: Path) -> None:
    """一台机器可能既没同步过、也没有历史 —— 那也是一份合法的导出。"""
    path = tmp_path / "portable.sqlite"

    write(path, visits=[], records=[], cursors=[])

    portable = read_portable(path)

    assert portable.visits == ()
    assert portable.records == ()
    assert portable.cursors == ()
    assert portable.warnings == ()
    assert portable.meta.latest_visit_us == 0


def test_latest_visit_survives_the_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "portable.sqlite"

    write(
        path,
        visits=[
            visit("https://old.example/"),
            visit("https://new.example/", seconds=1_800_000_000),
        ],
    )

    assert read_portable(path).meta.latest_visit_us == 1_800_000_000 * US


def test_missing_file_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        read_portable(tmp_path / "nope.sqlite")

    assert "nope.sqlite" in str(caught.value)


def test_not_a_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notes.sqlite"
    path.write_text("这不是数据库", encoding="utf-8")

    with pytest.raises(ConfigurationError) as caught:
        read_portable(path)

    assert "ffinfo" in str(caught.value)


def test_raw_places_sqlite_gets_the_wal_lecture(tmp_path: Path) -> None:
    """有人直接把 Firefox 的 places.sqlite 拷过来了 —— 得当场告诉他坑在哪。"""
    path = tmp_path / "places.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT)")

    with pytest.raises(ConfigurationError) as caught:
        read_portable(path)

    message = str(caught.value)
    assert "places.sqlite" in message
    assert "-wal" in message
    assert "export" in message


def test_schema_version_mismatch_is_refused(tmp_path: Path) -> None:
    """将来的格式改了，旧版本不能"凑合读" —— 读歪了比读不了更糟。"""
    path = tmp_path / "portable.sqlite"
    write(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE ffinfo_export SET value = '99' WHERE key = 'schema_version'")
        connection.commit()

    with pytest.raises(ConfigurationError) as caught:
        read_portable(path)

    assert "99" in str(caught.value)


def test_truncated_file_is_flagged(tmp_path: Path) -> None:
    """元数据说 2 条、文件里只剩 1 条 —— **必须说话**，不能当没事。"""
    path = tmp_path / "portable.sqlite"
    write(path, visits=[visit("https://a.example/"), visit("https://b.example/")])
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DELETE FROM ffinfo_visits WHERE url = 'https://b.example/'")
        connection.commit()

    portable = read_portable(path)

    assert len(portable.warnings) == 1
    assert "2" in portable.warnings[0]
    assert "1" in portable.warnings[0]


def test_wal_that_was_not_carried_is_flagged(tmp_path: Path) -> None:
    """导出时源库有没落盘的 WAL，却没带出来 —— 最近的记录缺了，得说。"""
    path = tmp_path / "portable.sqlite"

    write(path, source=source(wal_carried=False))

    portable = read_portable(path)

    assert len(portable.warnings) == 1
    assert "-wal" in portable.warnings[0]


def test_clean_export_has_no_warnings(tmp_path: Path) -> None:
    """没有 WAL（Firefox 关着）也不该报 —— 那不是问题。"""
    path = tmp_path / "portable.sqlite"

    write(path, source=source(wal_bytes=0, wal_carried=False))

    assert read_portable(path).warnings == ()
