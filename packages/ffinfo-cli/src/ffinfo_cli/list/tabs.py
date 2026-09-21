"""``ffinfo-cli list tabs`` —— 按设备分组，一个 BSO 就是一台设备。

``--device`` 按 ``clientName``（不区分大小写）或 ``clientId`` **精确**匹配（不做子串）。
``--limit`` 数的是标签页条数 —— 设备是分组的结构，不占名额
（与 bookmarks 那边"文件夹不占名额"一个道理）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ffinfo.crypto import KeyBundle
from ffinfo.tabs import ClientTabs, parse_tabs
from ffinfo_cli.list.common import (
    ListReport,
    cloud_key,
    details,
    guard_all_failed,
    load_shell,
)


async def run_tabs(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    device: str | None = None,
    limit: int | None = None,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> ListReport:
    """读库 → 解密 → 按设备选 → 分组 → 出报告。全程不联网。"""
    shell = await load_shell(
        database_path=database_path,
        collection="tabs",
        filters={"device": device, "limit": limit},
        warn=warn,
        clock=clock,
    )
    key = await cloud_key(
        store=shell.store,
        identity_path=identity_path,
        credentials_path=credentials_path,
        collection="tabs",
    )
    return _tabs_report(shell.records, key, shell.common, device=device, limit=limit)


def _tabs_report(
    records: Sequence[tuple[str, str | None]],
    key: KeyBundle,
    common: dict[str, Any],
    *,
    device: str | None,
    limit: int | None,
) -> ListReport:
    """标签页：按设备分组。``--device`` 只留下命中的那台；没命中就空结果 + ``notes``。"""
    decrypted = parse_tabs(records, key)
    guard_all_failed(decrypted.records, len(decrypted.clients), len(decrypted.skipped))

    notes: list[str] = []
    clients = list(decrypted.clients)
    if device is not None:
        clients = [client for client in clients if _device_matches(client, device)]
        if not clients:
            notes.append(f"没有匹配设备「{device}」的设备")
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
        notes=notes,
        matched=matched,
        returned=sum(client.count for client in clients),
        clients=clients,
    )


def _device_matches(client: ClientTabs, device: str) -> bool:
    """``clientName``（不区分大小写）**或** ``clientId`` 精确命中。不做子串。"""
    wanted = device.strip()
    return client.client_name.casefold() == wanted.casefold() or client.client_id == wanted


def list_tabs_blocking(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    device: str | None = None,
    limit: int | None = None,
    warn: Callable[[str], None] | None = None,
) -> ListReport:
    """:func:`run_tabs` 的同步外壳。纯本地，所以没有 HTTP 客户端要开。"""
    return asyncio.run(
        run_tabs(
            identity_path=identity_path,
            credentials_path=credentials_path,
            database_path=database_path,
            device=device,
            warn=warn,
            limit=limit,
        )
    )
