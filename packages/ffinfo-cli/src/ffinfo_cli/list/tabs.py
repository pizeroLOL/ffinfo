"""``ffinfo-cli list tabs`` —— 按设备分组，一个 BSO 就是一台设备。

``--limit`` 数的是标签页条数 —— 设备是分组的结构，不占名额
（与 bookmarks 那边"文件夹不占名额"一个道理）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ffinfo.crypto import KeyBundle
from ffinfo.tabs import ClientTabs, TabEntry, parse_tabs
from ffinfo_cli.list.common import (
    ListReport,
    cloud_key,
    details,
    guard_all_failed,
    keeper,
    load_shell,
)


async def run_tabs(
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
    """读库 → 解密 → 分组 → 过滤 → 出报告。全程不联网。"""
    shell = await load_shell(
        database_path=database_path,
        collection="tabs",
        since=since,
        domain=domain,
        search=search,
        limit=limit,
        warn=warn,
        clock=clock,
    )
    key = await cloud_key(
        store=shell.store,
        identity_path=identity_path,
        credentials_path=credentials_path,
        collection="tabs",
    )
    return _tabs_report(
        shell.records, key, shell.common, since=since, domain=domain, search=search, limit=limit
    )


def _tabs_report(
    records: Sequence[tuple[str, str | None]],
    key: KeyBundle,
    common: dict[str, Any],
    *,
    since: datetime | None,
    domain: str | None,
    search: str | None,
    limit: int | None,
) -> ListReport:
    """标签页：按设备分组。过滤只作用在标签页上，筛空的设备跟着去掉。"""
    decrypted = parse_tabs(records, key)
    guard_all_failed(decrypted.records, len(decrypted.clients), len(decrypted.skipped))

    matches = keeper(since=since, domain=domain, search=search)

    def keep(entry: TabEntry) -> bool:
        return matches(when=entry.last_used_at, url=entry.url, title=entry.title)

    clients = [
        ClientTabs(
            client_id=client.client_id,
            client_name=client.client_name,
            tabs=tuple(tab for tab in client.tabs if keep(tab)),
        )
        for client in decrypted.clients
    ]
    clients = [client for client in clients if client.tabs]
    matched = sum(client.count for client in clients)

    if limit is not None:
        budget = max(limit, 0)
        trimmed: list[ClientTabs] = []
        for client in clients:
            if budget <= 0:
                break
            kept = client.tabs[:budget]
            trimmed.append(
                ClientTabs(client_id=client.client_id, client_name=client.client_name, tabs=kept)
            )
            budget -= len(kept)
        clients = trimmed

    return ListReport(
        **common,
        skipped=len(decrypted.skipped),
        skipped_details=details(decrypted.skipped),
        matched=matched,
        returned=sum(client.count for client in clients),
        clients=clients,
    )


def list_tabs_blocking(
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
    """:func:`run_tabs` 的同步外壳。纯本地，所以没有 HTTP 客户端要开。"""
    return asyncio.run(
        run_tabs(
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
