"""本地访问的落库（08 号 ticket）—— ``local_visits`` 这张表。

决策 12 要的是**双源分表**：云端来的在 ``sync_records``（加密原文），本地
``places.sqlite`` 来的在这张表（本来就是明文，没有解密这回事）。查询时才合并。

**认"同一次访问"靠 ``(machine, url, visited_at)``**，不靠自增主键 —— 自增主键在
"删了重插"之后会重排（04 号 ticket 实测过），拿它当身份会静默串行。
带上 ``machine`` 是因为同一个库将来可能收下好几台机器的导出。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ffinfo_cli.store import (
    LocalVisitRow,
    StoredVisit,
    load_local_visits,
    open_database,
    store_local_visits,
)

US = 1_000_000


def visit(
    url: str = "https://a.example/",
    *,
    machine: str = "test-laptop",
    seconds: int = 1_700_000_000,
    title: str = "A",
    visit_type: int = 1,
) -> StoredVisit:
    return StoredVisit(
        machine=machine,
        url=url,
        title=title,
        visited_at=datetime.fromtimestamp(seconds, tz=UTC),
        visit_type=visit_type,
    )


async def test_store_and_load_round_trip(tmp_path: Path) -> None:
    engine = await open_database(tmp_path / "db.sqlite")

    result = await store_local_visits(engine, [visit(), visit("https://b.example/")])

    assert result.inserted == 2
    stored = await load_local_visits(engine)
    assert [item.url for item in stored] == ["https://a.example/", "https://b.example/"]
    assert stored[0].machine == "test-laptop"
    assert stored[0].title == "A"
    assert stored[0].visited_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)


async def test_microsecond_precision_survives_the_round_trip(tmp_path: Path) -> None:
    """合并两个源靠的是逐微秒相等 —— 落库这一段不能把它磨掉。"""
    engine = await open_database(tmp_path / "db.sqlite")
    exact = datetime(2023, 11, 14, 22, 13, 20, 123_456, tzinfo=UTC)

    await store_local_visits(
        engine,
        [
            StoredVisit(
                machine="m", url="https://a.example/", title="A", visited_at=exact, visit_type=1
            )
        ],
    )

    stored = await load_local_visits(engine)
    assert stored[0].visited_at == exact


async def test_reimport_is_idempotent(tmp_path: Path) -> None:
    """**增量导入**：同一份导出再导一次，不能翻倍。"""
    engine = await open_database(tmp_path / "db.sqlite")
    batch = [visit(), visit("https://b.example/")]

    await store_local_visits(engine, batch)
    second = await store_local_visits(engine, batch)

    assert second.inserted == 0
    assert len(await load_local_visits(engine)) == 2


async def test_second_import_only_adds_the_new_part(tmp_path: Path) -> None:
    """第一次导 1 条，第二次导 3 条（含原来那条）—— 库里是 3 条，不是 4 条。"""
    engine = await open_database(tmp_path / "db.sqlite")
    first = visit()

    await store_local_visits(engine, [first])
    result = await store_local_visits(
        engine, [first, visit("https://b.example/"), visit("https://c.example/")]
    )

    assert result.inserted == 2
    assert len(await load_local_visits(engine)) == 3


async def test_same_visit_from_two_machines_coexist(tmp_path: Path) -> None:
    """两台机器看了同一个 URL、时间戳还撞上 —— 那是两次访问，各留各的。"""
    engine = await open_database(tmp_path / "db.sqlite")

    await store_local_visits(engine, [visit(machine="laptop"), visit(machine="desktop")])

    stored = await load_local_visits(engine)
    assert sorted(item.machine for item in stored) == ["desktop", "laptop"]


async def test_title_change_updates_instead_of_duplicating(tmp_path: Path) -> None:
    """同一次访问、标题后来变了（Firefox 会改）—— 更新，不新增。"""
    engine = await open_database(tmp_path / "db.sqlite")
    await store_local_visits(engine, [visit(title="旧标题")])

    result = await store_local_visits(engine, [visit(title="新标题")])

    assert result.inserted == 0
    assert result.updated == 1
    stored = await load_local_visits(engine)
    assert [item.title for item in stored] == ["新标题"]


async def test_load_returns_empty_on_a_fresh_database(tmp_path: Path) -> None:
    """**本地源缺失要能降级** —— 没导入过就是空的，不是错误。"""
    engine = await open_database(tmp_path / "db.sqlite")

    assert await load_local_visits(engine) == ()


async def test_table_is_created_by_open_database(tmp_path: Path) -> None:
    await open_database(tmp_path / "db.sqlite")

    assert await LocalVisitRow.count() == 0
