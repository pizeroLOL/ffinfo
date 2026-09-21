"""``ffinfo-cli list history`` —— 双源合并 → 一次访问一行，最新的在前。

**历史是双源的**：云端 Sync 拉下来的（``sync_records``，要解密）和 firefox 的
``places.sqlite`` 导入进来的（``firefox_visits``，本来就是明文）在查询时合并。合并按
``(url, 访问时刻)`` —— **逐微秒相等才算同一次访问**，这样"两个源都有"的那条只出一行、
标成 ``both``，而不是重复两行。只有一边有就照常出，标 ``sync`` 或 ``firefox``。
firefox 源是空的（目标机器没导入过）就自然降级成单源，不用特判。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ffinfo.crypto import KeyBundle
from ffinfo.history import DecryptionReport, HistoryEntry, decrypt_history, visit_type_name
from ffinfo.timestamps import to_microseconds
from ffinfo_cli.list.common import (
    HistoryItem,
    ListReport,
    SourceName,
    VisitSource,
    cloud_key,
    details,
    guard_all_failed,
    keeper,
    load_shell,
    truncate,
)
from ffinfo_cli.store import StoredVisit


async def run_history(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    since: datetime | None = None,
    domain: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> ListReport:
    """读库 → 解密 → 过滤 → 出报告。全程不联网。"""
    shell = await load_shell(
        database_path=database_path,
        collection="history",
        since=since,
        domain=domain,
        search=search,
        limit=limit,
        warn=warn,
        clock=clock,
    )
    # 双源：库里一条云端记录都没有时，明文的 firefox 数据就够 —— 别拿凭据挡路。
    # 从没 login 过的机器也能看自己导入的东西（凭据只为解密云端记录而存在）。
    key = (
        await cloud_key(
            store=shell.store,
            identity_path=identity_path,
            credentials_path=credentials_path,
            collection="history",
        )
        if shell.records
        else None
    )
    return _history_report(
        shell.records,
        key,
        shell.firefox,
        shell.common,
        since=since,
        domain=domain,
        search=search,
        limit=limit,
    )


def _history_report(
    records: Sequence[tuple[str, str | None]],
    key: KeyBundle | None,
    firefox: Sequence[StoredVisit],
    common: dict[str, Any],
    *,
    since: datetime | None,
    domain: str | None,
    search: str | None,
    limit: int | None,
) -> ListReport:
    """历史：**两个源合并**，拍平成一次访问一行，最新的在前。"""
    # ``key is None`` 只在“库里没有任何云端记录”时发生（见 run_history）—— 此时没有密文要解，
    # 空结果就是全部真相；纯 firefox 源的人不需要 age 私钥。
    decrypted = (
        decrypt_history(records, key)
        if key is not None
        else DecryptionReport(entries=(), skipped=(), tombstones=0, records=0)
    )
    guard_all_failed(
        decrypted.records, len(decrypted.entries), len(decrypted.skipped), fallback=len(firefox)
    )

    matches = keeper(since=since, domain=domain, search=search)
    selected = [
        item
        for item in _merge_history(decrypted.entries, firefox)
        if matches(when=item.visited_at, url=item.url, title=item.title)
    ]
    selected.sort(key=lambda item: item.visited_at, reverse=True)

    returned = truncate(selected, limit)
    return ListReport(
        **common,
        visits=len(decrypted.entries),
        firefox_records=len(firefox),
        sources=_sources(len(decrypted.entries), len(firefox)),
        skipped=len(decrypted.skipped),
        skipped_details=details(decrypted.skipped),
        matched=len(selected),
        returned=len(returned),
        items=[_item(item) for item in returned],
    )


@dataclass(frozen=True, slots=True)
class _MergedVisit:
    """合并之后、还没转成输出形态的一条访问。"""

    url: str
    title: str
    visited_at: datetime
    visit_type: int
    record_id: str | None
    source: VisitSource
    machine: str | None


def _merge_history(
    entries: Sequence[HistoryEntry], firefox: Sequence[StoredVisit]
) -> list[_MergedVisit]:
    """把云端与 firefox 两个源并成一个列表。

    **认"同一次访问"靠 ``(url, 微秒)``。** 云端那条的时刻来自记录里的 ``date``，
    firefox 那条来自 ``moz_historyvisits.visit_date`` —— 两边都是 PRTime 微秒，
    所以只要换算不引入误差，它们就能精确对上。这也是 ``ffinfo.timestamps`` 里坚持走整数运算的原因：
    差 1 微秒，同一次访问就会出两行。
    """
    merged: dict[tuple[str, int], _MergedVisit] = {}
    for entry in entries:
        merged[(entry.url, to_microseconds(entry.visited_at))] = _MergedVisit(
            url=entry.url,
            title=entry.title,
            visited_at=entry.visited_at,
            visit_type=entry.visit_type,
            record_id=entry.record_id,
            source="sync",
            machine=None,
        )

    for item in firefox:
        key = (item.url, to_microseconds(item.visited_at))
        existing = merged.get(key)
        if existing is None:
            merged[key] = _MergedVisit(
                url=item.url,
                title=item.title,
                visited_at=item.visited_at,
                visit_type=item.visit_type,
                record_id=None,
                source="firefox",
                machine=item.machine,
            )
            continue
        merged[key] = replace(
            existing,
            # firefox 那条没标题时，别把云端已有的标题丢了
            title=existing.title or item.title,
            source="both",
            machine=item.machine,
        )
    return list(merged.values())


def _item(visit: _MergedVisit) -> HistoryItem:
    return HistoryItem(
        url=visit.url,
        title=visit.title,
        visited_at=visit.visited_at.isoformat(),
        visit_type=visit.visit_type,
        visit_type_name=visit_type_name(visit.visit_type),
        record_id=visit.record_id,
        source=visit.source,
        source_machine=visit.machine,
    )


def _sources(sync_visits: int, firefox_visits: int) -> list[SourceName]:
    """哪些源真的出了数据 —— 只剩一个就说明这次是**单源降级**。"""
    pairs: tuple[tuple[SourceName, int], ...] = (
        ("sync", sync_visits),
        ("firefox", firefox_visits),
    )
    return [name for name, count in pairs if count]


def list_history_blocking(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    since: datetime | None = None,
    domain: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    warn: Callable[[str], None] | None = None,
) -> ListReport:
    """:func:`run_history` 的同步外壳。纯本地，所以没有 HTTP 客户端要开。"""
    return asyncio.run(
        run_history(
            identity_path=identity_path,
            credentials_path=credentials_path,
            database_path=database_path,
            since=since,
            domain=domain,
            search=search,
            warn=warn,
            limit=limit,
        )
    )
