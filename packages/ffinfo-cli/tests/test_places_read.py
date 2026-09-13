"""本地 places.sqlite：快照 + 读取。

**WAL 是这个文件最大的坑**：Firefox 跑着的时候最近的访问还躺在 ``places.sqlite-wal``
里，只拷主文件会**静默**丢掉它们。所以这里把"带上 -wal / -shm"做成硬行为，并用测试锁住。

测试用的 ``places.sqlite`` 是**现造**的 —— 只建我们真正会读的两张表，字段按 Firefox
真实的 schema 来（``moz_places`` / ``moz_historyvisits``）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ffinfo.errors import ConfigurationError
from ffinfo_cli.places import LocalVisit, read_visits, snapshot_places
from support import US, add_visits, build_places


@pytest.fixture
def live_places(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    """一个"Firefox 正在运行"的 places.sqlite。

    **老记录已经落进主文件，新记录还停在 ``-wal`` 里** —— 这才是真实的形态，
    也是"只拷主文件"会静默丢数据的原因。连接不关，WAL 就不会被 checkpoint。
    """
    path = tmp_path / "profile" / "places.sqlite"
    build_places(path, [("https://old.example/", "Old", 1_699_000_000 * US, 1)]).close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    add_visits(
        connection,
        [
            ("https://example.com/", "Example", 1_700_000_000 * US, 1),
            ("https://example.com/deep", "Deep", 1_700_000_060 * US, 1),
        ],
    )
    connection.commit()
    try:
        yield path, 3
    finally:
        connection.close()


# ── 快照 ──────────────────────────────────────────────────────────────────


def test_snapshot_carries_the_wal_and_shm(tmp_path: Path, live_places: tuple[Path, int]) -> None:
    """Firefox 运行中：附属文件必须一起走，否则最近的数据留在原地。"""
    source, count = live_places
    assert (source.parent / "places.sqlite-wal").stat().st_size > 0

    snapshot = snapshot_places(source, into=tmp_path / "snap")

    assert set(snapshot.sidecars) == {"-wal", "-shm"}
    assert snapshot.wal_bytes > 0
    assert snapshot.directory == tmp_path / "snap"
    # WAL 折进去了，快照自包含 —— 目录里只剩一个文件，读得到全部记录
    assert [path.name for path in snapshot.directory.iterdir()] == ["places.sqlite"]
    assert len(read_visits(snapshot.database)) == count


def test_snapshot_works_without_sidecars(tmp_path: Path) -> None:
    """Firefox 关着的时候没有 -wal —— 这不是错误，照常快照。"""
    source = tmp_path / "profile" / "places.sqlite"
    build_places(source, [("https://example.com/", "Example", 1_700_000_000 * US, 1)]).close()

    snapshot = snapshot_places(source, into=tmp_path / "snap")

    assert snapshot.sidecars == ()
    assert snapshot.wal_bytes == 0


def test_snapshot_leaves_the_original_alone(tmp_path: Path) -> None:
    """**严格只读** —— 快照不能碰源文件（那是用户正在用的 Firefox）。"""
    source = tmp_path / "profile" / "places.sqlite"
    build_places(source, [("https://example.com/", "Example", 1_700_000_000 * US, 1)]).close()
    before = {path.name: path.read_bytes() for path in source.parent.iterdir()}

    snapshot = snapshot_places(source, into=tmp_path / "snap")

    after = {path.name: path.read_bytes() for path in source.parent.iterdir()}
    assert after == before
    assert snapshot.database != source


def test_snapshot_missing_database_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        snapshot_places(tmp_path / "profile" / "places.sqlite", into=tmp_path / "snap")

    assert "places.sqlite" in str(caught.value)


# ── 读取 ──────────────────────────────────────────────────────────────────


def test_read_visits_one_row_per_visit(tmp_path: Path) -> None:
    source = tmp_path / "places.sqlite"
    build_places(
        source,
        [
            ("https://a.example/", "A", 1_700_000_000 * US, 1),
            ("https://a.example/", "A", 1_700_000_060 * US, 2),
            ("https://b.example/", None, 1_700_000_120 * US, 1),
        ],
    ).close()

    visits = read_visits(source)

    assert [visit.url for visit in visits] == [
        "https://a.example/",
        "https://a.example/",
        "https://b.example/",
    ]
    assert visits[1].visit_type == 2
    assert visits[2].title == ""
    assert visits[0].visited_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_read_visits_since_filters(tmp_path: Path) -> None:
    source = tmp_path / "places.sqlite"
    build_places(
        source,
        [
            ("https://old.example/", "Old", 1_600_000_000 * US, 1),
            ("https://new.example/", "New", 1_700_000_000 * US, 1),
        ],
    ).close()

    visits = read_visits(source, since=datetime(2023, 1, 1, tzinfo=UTC))

    assert [visit.url for visit in visits] == ["https://new.example/"]


def test_read_visits_skips_hidden_places(tmp_path: Path) -> None:
    """``hidden=1`` 是 Firefox 自己标"别在历史里显示"的（跳转落点之类），跟着它走。"""
    source = tmp_path / "places.sqlite"
    build_places(
        source,
        [
            ("https://shown.example/", "Shown", 1_700_000_000 * US, 1),
            ("https://hidden.example/", "Hidden", 1_700_000_060 * US, 1),
        ],
        hidden=["https://hidden.example/"],
    ).close()

    visits = read_visits(source)

    assert [visit.url for visit in visits] == ["https://shown.example/"]


def test_read_visits_keeps_microsecond_precision(tmp_path: Path) -> None:
    """微秒不能丢 —— 合并时就是靠它认"这两条是不是同一次访问"。"""
    source = tmp_path / "places.sqlite"
    stamp = 1_700_000_000_123_456
    build_places(source, [("https://a.example/", "A", stamp, 1)]).close()

    visits = read_visits(source)

    assert visits[0].visited_at.microsecond == 123_456


def test_read_visits_from_a_snapshot_keeps_what_lives_in_the_wal(
    tmp_path: Path, live_places: tuple[Path, int]
) -> None:
    """**这张票的核心**：快照带着 -wal 走，WAL 里的那几条就读得到。"""
    source, count = live_places

    snapshot = snapshot_places(source, into=tmp_path / "snap")

    assert len(read_visits(snapshot.database)) == count


def test_main_file_alone_loses_what_is_still_in_the_wal(
    tmp_path: Path, live_places: tuple[Path, int]
) -> None:
    """反面教材：只拷主文件 → **静默**丢数据。这就是必须带 -wal 的理由。"""
    source, _ = live_places
    main_only = tmp_path / "hand-copied.sqlite"
    main_only.write_bytes(source.read_bytes())

    rows = read_visits(main_only)

    # **不报错**，就是少了 —— 这就是它危险的地方
    assert len(rows) == 1
    assert [visit.url for visit in rows] == ["https://old.example/"]


def test_read_visits_does_not_modify_the_snapshot(
    tmp_path: Path, live_places: tuple[Path, int]
) -> None:
    """读的时候**只读打开**，不给快照留下任何改动（也就不需要写权限）。"""
    source, _ = live_places
    snapshot = snapshot_places(source, into=tmp_path / "snap")
    before = {path.name: path.read_bytes() for path in snapshot.directory.iterdir()}

    read_visits(snapshot.database)

    after = {path.name: path.read_bytes() for path in snapshot.directory.iterdir()}
    assert after == before


def test_read_visits_missing_table_is_actionable(tmp_path: Path) -> None:
    """传进来一个不是 places.sqlite 的库时，别把 sqlite3 的原始错误甩给用户。"""
    other = tmp_path / "other.sqlite"
    with closing(sqlite3.connect(other)) as connection:
        connection.execute("CREATE TABLE nope (id INTEGER)")

    with pytest.raises(ConfigurationError) as caught:
        read_visits(other)

    assert "places.sqlite" in str(caught.value)


def test_local_visit_is_frozen() -> None:
    """值对象 —— 拿到手就不该能改。"""
    visit = LocalVisit(
        url="https://a.example/",
        title="A",
        visited_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
        visit_type=1,
    )

    with pytest.raises(Exception):  # noqa: B017 —— 冻结的 dataclass 会抛 FrozenInstanceError
        visit.url = "https://b.example/"  # type: ignore[misc]
