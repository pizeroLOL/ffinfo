"""``ffinfo_cli.render`` —— 全部输出知识的 seam。

纯函数：注入 ``machine`` / ``tz`` / ``width`` 让两种模式的输出都确定。
机器模式必须与旧 ``to_json`` 逐字段一致；人读模式钉住排版（时间走注入时区）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from ffinfo.bookmarks import BookmarkNode
from ffinfo.tabs import ClientTabs, TabEntry
from ffinfo_cli.list.bookmarks import BookmarksReport
from ffinfo_cli.list.history import HistoryItem, HistoryReport
from ffinfo_cli.list.tabs import TabsReport
from ffinfo_cli.profiles import CollectionProgress, FileInfo, ProfileInfo, ProfilesReport
from ffinfo_cli.render import render
from ffinfo_cli.sync import CollectedSync, SyncReport
from ffinfo_cli.transfer import ExportReport, ImportReport

SHANGHAI = timezone(timedelta(hours=8), "CST")
"""固定东八区 —— 人读时间必须跟着注入的时区走，不能跟着跑测试的机器。"""


def history_report(*, items: list[HistoryItem] | None = None, **overrides: Any) -> HistoryReport:
    rows = items if items is not None else []
    base: dict[str, Any] = {
        "data_type": "history",
        "generated_at": "2026-09-13T17:30:12+00:00",
        "filters": {},
        "records": len(rows),
        "skipped": 0,
        "matched": len(rows),
        "returned": len(rows),
        "items": rows,
    }
    base.update(overrides)
    return HistoryReport(**base)


def history_item(url: str, title: str, when: str) -> HistoryItem:
    return HistoryItem(
        url=url,
        title=title,
        visited_at=when,
        visit_type=1,
        visit_type_name="link",
        record_id="rec",
        source="sync",
        source_machine=None,
    )


def tabs_report() -> TabsReport:
    return TabsReport(
        data_type="tabs",
        generated_at="2026-09-13T17:30:12+00:00",
        filters={},
        records=1,
        skipped=0,
        matched=2,
        returned=2,
        clients=[
            ClientTabs(
                client_id="dev-1",
                client_name="alpha",
                tabs=(
                    TabEntry(
                        client_id="dev-1",
                        client_name="alpha",
                        title="一",
                        url="https://a.test/",
                        last_used_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
                        icon=None,
                        window_id=None,
                    ),
                    TabEntry(
                        client_id="dev-1",
                        client_name="alpha",
                        title="二",
                        url="https://b.test/",
                        last_used_at=None,
                        icon=None,
                        window_id=None,
                    ),
                ),
            )
        ],
    )


def bookmarks_report() -> BookmarksReport:
    return BookmarksReport(
        data_type="bookmarks",
        generated_at="2026-09-13T17:30:12+00:00",
        filters={},
        records=2,
        skipped=0,
        matched=1,
        returned=1,
        tree=[
            BookmarkNode(
                id="folder",
                type="folder",
                title="工具",
                children=[
                    BookmarkNode(
                        id="bmk",
                        type="bookmark",
                        title="示例",
                        url="https://example.com/",
                    )
                ],
            )
        ],
    )


def test_machine_history_json_is_the_flat_v5_shape() -> None:
    report = history_report(
        items=[history_item("https://a.test/", "A", "2026-09-13T12:00:00+00:00")]
    )

    payload = json.loads(render(report, machine=True))

    assert payload["format_version"] == 5
    assert payload["data_type"] == "history"
    assert payload["items"][0]["visited_at"] == "2026-09-13T12:00:00+00:00"


def test_machine_json_serializes_datetimes_as_isoformat() -> None:
    """``BookmarkNode.added_at`` 与标签页时间是 ``datetime`` —— 统一 ``isoformat()``。"""
    report = bookmarks_report()
    report.tree[0].children[0] = (
        report.tree[0]
        .children[0]
        .model_copy(update={"added_at": datetime(2026, 9, 13, 12, 0, tzinfo=UTC)})
    )

    payload = json.loads(render(report, machine=True))

    assert payload["tree"][0]["children"][0]["added_at"] == "2026-09-13T12:00:00+00:00"

    tabs = json.loads(render(tabs_report(), machine=True))
    assert tabs["clients"][0]["tabs"][0]["last_used_at"] == "2023-11-14T22:13:20+00:00"


def test_human_history_table_uses_the_injected_timezone() -> None:
    report = history_report(
        items=[history_item("https://example.com/page", "Example", "2026-09-13T12:00:00+00:00")],
        sources=["sync"],
        synced_at="2026-09-13T12:00:00+00:00",
        age_seconds=0.0,
    )

    output = render(report, machine=False, tz=SHANGHAI, width=120)

    lines = output.splitlines()
    assert lines[0] == "共 1 次访问（匹配 1）· 源：sync · 同步于 2026-09-13 20:00:00"
    assert "时间" in lines[1]
    assert lines[-1].startswith("2026-09-13 20:00:00")
    assert "Example" in lines[-1]
    assert "example.com" in lines[-1]


def test_human_history_never_synced_says_so() -> None:
    report = history_report()

    output = render(report, machine=False, tz=UTC, width=120)

    assert "从未同步" in output


def test_human_bookmarks_are_an_indented_tree() -> None:
    output = render(bookmarks_report(), machine=False, tz=UTC, width=120)

    assert output.splitlines() == ["▸ 工具", "  • 示例  https://example.com/"]


def test_human_tabs_group_by_device() -> None:
    output = render(tabs_report(), machine=False, tz=UTC, width=120)

    assert output.splitlines() == ["alpha", "  • 一  https://a.test/", "  • 二  https://b.test/"]


def sync_report() -> SyncReport:
    return SyncReport(
        collections=[
            CollectedSync(
                collection="history",
                mode="full",
                records=3,
                inserted=2,
                updated=1,
                deleted=0,
                pages=1,
                tombstones=0,
                server_count=3,
                cursor_before=None,
                cursor_after=1.0,
            )
        ],
        elapsed_seconds=0.5,
        database="/tmp/ffinfo.sqlite",
        protocol={"crypto": 1},
    )


def test_human_sync_is_a_key_value_summary() -> None:
    output = render(sync_report(), machine=False, tz=UTC, width=120)

    assert output.splitlines() == [
        "collections: history",
        "records: 3",
        "elapsed_seconds: 0.5",
        "database: /tmp/ffinfo.sqlite",
        "protocol: crypto=1",
    ]


def test_human_export_is_a_key_value_summary() -> None:
    report = ExportReport(
        destination="/tmp/portable.sqlite",
        machine="laptop",
        profile="default-release",
        profile_path="/home/u/.mozilla/default-release",
        schema_version=1,
        visits=5,
        records=2,
        cursors=1,
        wal_bytes=0,
        elapsed_seconds=0.25,
    )

    output = render(report, machine=False, tz=UTC, width=120)

    assert output.splitlines() == [
        "destination: /tmp/portable.sqlite",
        "machine: laptop",
        "profile: default-release",
        "schema_version: 1",
        "visits: 5",
        "records: 2",
        "cursors: 1",
        "wal_bytes: 0",
        "elapsed_seconds: 0.25",
    ]


def test_human_import_is_a_key_value_summary() -> None:
    report = ImportReport(
        input="firefox",
        machine="laptop",
        profile="default-release",
        visits_inserted=5,
        visits_skipped=1,
        records_inserted=0,
        records_updated=0,
        records_kept=0,
        cursors_advanced=0,
        elapsed_seconds=0.25,
    )

    output = render(report, machine=False, tz=UTC, width=120)

    assert output.splitlines() == [
        "input: firefox",
        "portable_path: —",
        "machine: laptop",
        "profile: default-release",
        "exported_at: —",
        "visits_inserted: 5",
        "visits_skipped: 1",
        "records_inserted: 0",
        "records_updated: 0",
        "records_kept: 0",
        "cursors_advanced: 0",
        "elapsed_seconds: 0.25",
    ]


def profiles_report() -> ProfilesReport:
    return ProfilesReport(
        generated_at="2026-09-13T17:30:12+00:00",
        platform="linux",
        home="/home/u",
        profiles=[ProfileInfo(name="default-release", path="/home/u/.mozilla/dr", is_default=True)],
        searched_roots=[],
        config_dir=FileInfo(path="/home/u/.config/ffinfo", exists=True),
        data_dir=FileInfo(path="/home/u/.local/share/ffinfo", exists=True),
        identity=FileInfo(path="/home/u/.config/ffinfo/age-key.txt", exists=True),
        credentials=FileInfo(path="/home/u/.config/ffinfo/credentials.age", exists=True),
        database=FileInfo(path="/home/u/.local/share/ffinfo/ffinfo.sqlite", exists=True),
        collections=[
            CollectionProgress(
                collection="history",
                records=3,
                last_modified=1.0,
                synced_at="2026-09-13T17:00:00+00:00",
            )
        ],
    )


def test_human_profiles_is_a_key_value_summary() -> None:
    output = render(profiles_report(), machine=False, tz=UTC, width=120)

    assert output.splitlines() == [
        "platform: linux",
        "home: /home/u",
        "profiles: 1",
        "collections: history",
        "database: /home/u/.local/share/ffinfo/ffinfo.sqlite（存在）",
        "credentials: /home/u/.config/ffinfo/credentials.age（存在）",
        "identity: /home/u/.config/ffinfo/age-key.txt（存在）",
    ]
