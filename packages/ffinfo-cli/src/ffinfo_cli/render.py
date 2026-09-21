"""全部输出的**唯一 seam** —— 机器 JSON 与人读排版都在这里。

为什么要把所有序列化收进来：命令各自 ``typer.echo(report.to_json())`` 时，"stdout 只有
JSON / stderr 只有错误"这条契约在每个命令里重复一遍；默认改人读、``-j`` 切 JSON 之后，
每份报告还要再多一条渲染路径。收进一个纯函数后，interface 就是测试面：

* ``machine=True`` —— 给 agent 的 JSON，形状与旧 ``to_json`` 逐字段一致
* ``machine=False`` —— 给人读的排版：history 三列表格、bookmarks 缩进树、tabs 设备小标题、
  其余 ``key: value`` 短摘要

人读时间用**注入的时区**（测试确定；不注入才用本机时区），宽度交给 ``rich``
（``width`` 可注入）。JSON 仍是 UTC ISO —— agent 契约不动。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, tzinfo
from io import StringIO
from typing import assert_never
from urllib.parse import urlsplit

from rich import box
from rich.console import Console
from rich.table import Table

from ffinfo.bookmarks import BookmarkNode
from ffinfo_cli.list.bookmarks import BookmarksReport
from ffinfo_cli.list.history import HistoryReport
from ffinfo_cli.list.report import ListReport
from ffinfo_cli.list.tabs import TabsReport
from ffinfo_cli.profiles import FileInfo, ProfilesReport
from ffinfo_cli.sync import SyncReport
from ffinfo_cli.transfer import ExportReport, ImportReport

__all__ = ["Report", "render"]

type Report = ListReport | SyncReport | ExportReport | ImportReport | ProfilesReport
"""五份能输出的报告 —— **封闭** union，pyright 能查 ``match`` 的穷尽性。"""


def render(
    report: Report,
    *,
    machine: bool,
    tz: tzinfo | None = None,
    width: int | None = None,
) -> str:
    """报告 → 一行/一屏文本。**纯函数**：不碰全局状态，返回值由命令层写出。"""
    if machine:
        return _to_json(report)
    match report:
        case HistoryReport():
            return _render_history(report, tz=tz, width=width)
        case BookmarksReport():
            return _render_bookmarks(report)
        case TabsReport():
            return _render_tabs(report)
        case SyncReport():
            return _render_sync(report)
        case ExportReport():
            return _render_export(report)
        case ImportReport():
            return _render_import(report)
        case ProfilesReport():
            return _render_profiles(report)
        case _ as unreachable:
            assert_never(unreachable)


def _to_json(report: Report) -> str:
    """给 agent 消费的 JSON —— 扁平的 ``model_dump()``，``datetime`` 统一走 ISO。"""
    return json.dumps(report.model_dump(), ensure_ascii=False, indent=2, default=_json_default)


def _json_default(value: object) -> str:
    """``json.dumps`` 遇到富类型时的兜底 —— 目前只有时间字段的 ``datetime``。

    **统一走 ``isoformat()``**（``+00:00``）—— pydantic 的 json 模式会写成 ``Z``，
    两种风格混在一份输出里不好。
    """
    if isinstance(value, datetime):
        return value.isoformat()
    msg = f"JSON 不认识这个类型：{type(value).__name__}"
    raise TypeError(msg)


def _render_history(report: HistoryReport, *, tz: tzinfo | None, width: int | None) -> str:
    """三列表格（时间 / 标题 / 域名）+ 顶部汇总（条数、源、新鲜度）。"""
    console, stream = _console(width)
    console.print(_history_summary(report, tz))
    if report.items:
        table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
        table.add_column("时间", no_wrap=True)
        table.add_column("标题", no_wrap=True, overflow="ellipsis")
        table.add_column("域名", no_wrap=True, overflow="ellipsis")
        for item in report.items:
            table.add_row(_local_time(item.visited_at, tz), item.title, _domain(item.url))
        console.print(table)
    return _finish(stream)


def _history_summary(report: HistoryReport, tz: tzinfo | None) -> str:
    """汇总那行：条数 / 源 / 新鲜度。"""
    sources = "、".join(report.sources) if report.sources else "无"
    freshness = (
        "从未同步" if report.synced_at is None else f"同步于 {_local_time(report.synced_at, tz)}"
    )
    return f"共 {report.returned} 次访问（匹配 {report.matched}）· 源：{sources} · {freshness}"


def _render_bookmarks(report: BookmarksReport) -> str:
    """缩进树：文件夹 ``▸``、书签 ``•``。"""
    lines: list[str] = []
    for node in report.tree:
        _bookmark_lines(node, 0, lines)
    return "\n".join(lines)


def _bookmark_lines(node: BookmarkNode, depth: int, lines: list[str]) -> None:
    """把一个节点及其子树写进 ``lines`` —— 递归的深度就是缩进。"""
    indent = "  " * depth
    if node.type == "folder":
        lines.append(f"{indent}▸ {node.title or '（未命名文件夹）'}")
    else:
        label = node.title or node.url or "（无标题）"
        suffix = f"  {node.url}" if node.url and node.title else ""
        lines.append(f"{indent}• {label}{suffix}")
    for child in node.children:
        _bookmark_lines(child, depth + 1, lines)


def _render_tabs(report: TabsReport) -> str:
    """设备名小标题 + 标签列表。"""
    lines: list[str] = []
    for client in report.clients:
        lines.append(client.client_name or client.client_id)
        for tab in client.tabs:
            label = tab.title or tab.url or "（无标题）"
            suffix = f"  {tab.url}" if tab.url and tab.title else ""
            lines.append(f"  • {label}{suffix}")
    return "\n".join(lines)


def _render_sync(report: SyncReport) -> str:
    """一次 sync 的 ``key: value`` 短摘要。"""
    records = sum(entry.records for entry in report.collections)
    protocol = "、".join(f"{name}={count}" for name, count in report.protocol.items()) or "无"
    return "\n".join(
        [
            "collections: " + ("、".join(entry.collection for entry in report.collections) or "无"),
            f"records: {records}",
            f"elapsed_seconds: {report.elapsed_seconds}",
            f"database: {report.database}",
            f"protocol: {protocol}",
        ]
    )


def _render_export(report: ExportReport) -> str:
    """一次 export 的 ``key: value`` 短摘要。"""
    return "\n".join(
        [
            f"destination: {report.destination}",
            f"machine: {report.machine}",
            f"profile: {report.profile}",
            f"schema_version: {report.schema_version}",
            f"visits: {report.visits}",
            f"records: {report.records}",
            f"cursors: {report.cursors}",
            f"wal_bytes: {report.wal_bytes}",
            f"elapsed_seconds: {report.elapsed_seconds}",
        ]
    )


def _render_import(report: ImportReport) -> str:
    """一次 import 的 ``key: value`` 短摘要。"""
    return "\n".join(
        [
            f"input: {report.input}",
            f"portable_path: {report.portable_path or '—'}",
            f"machine: {report.machine}",
            f"profile: {report.profile}",
            f"exported_at: {report.exported_at or '—'}",
            f"visits_inserted: {report.visits_inserted}",
            f"visits_skipped: {report.visits_skipped}",
            f"records_inserted: {report.records_inserted}",
            f"records_updated: {report.records_updated}",
            f"records_kept: {report.records_kept}",
            f"cursors_advanced: {report.cursors_advanced}",
            f"elapsed_seconds: {report.elapsed_seconds}",
        ]
    )


def _render_profiles(report: ProfilesReport) -> str:
    """``profiles`` 的 ``key: value`` 短摘要。"""
    collections = "、".join(item.collection for item in report.collections) or "无"
    return "\n".join(
        [
            f"platform: {report.platform}",
            f"home: {report.home}",
            f"profiles: {len(report.profiles)}",
            f"collections: {collections}",
            f"database: {_file_info(report.database)}",
            f"credentials: {_file_info(report.credentials)}",
            f"identity: {_file_info(report.identity)}",
        ]
    )


def _file_info(info: FileInfo) -> str:
    """文件落点 + 存在性 —— 人读时一眼看出缺哪个。"""
    return f"{info.path}（{'存在' if info.exists else '缺失'}）"


def _console(width: int | None) -> tuple[Console, StringIO]:
    """人读输出的载体 —— 非 TTY、无颜色，宽度可注入，测试才稳。"""
    stream = StringIO()
    console = Console(
        file=stream,
        width=width,
        force_terminal=False,
        color_system=None,
        highlight=False,
        legacy_windows=False,
    )
    return console, stream


def _finish(stream: StringIO) -> str:
    """去掉 ``Console`` 收尾多出来的那一个换行 —— 命令层 ``typer.echo`` 会自己补。"""
    text = stream.getvalue()
    return text[:-1] if text.endswith("\n") else text


def _local_time(value: str, tz: tzinfo | None) -> str:
    """UTC ISO → 注入时区（缺省本机）的 ``YYYY-MM-DD HH:MM:SS``。"""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = tz if tz is not None else datetime.now().astimezone().tzinfo
    assert local is not None
    return moment.astimezone(local).strftime("%Y-%m-%d %H:%M:%S")


def _domain(url: str) -> str:
    """URL → 域名。``urlsplit`` 认不出来的就原样返回。"""
    return urlsplit(url).netloc or url
