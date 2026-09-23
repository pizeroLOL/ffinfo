"""``ffinfo-cli list tabs`` —— 按设备分组，一个 BSO 就是一台设备。

``--device`` 按 ``clientName``（不区分大小写）或 ``clientId`` **精确**匹配（不做子串）。
``--limit`` 数的是标签页条数 —— 设备是分组的结构，不占名额
（与 bookmarks 那边"文件夹不占名额"一个道理）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ffinfo.crypto import KeyBundle
from ffinfo.tabs import ClientTabs, parse_tabs
from ffinfo_cli.list.common import (
    ShellCommon,
    SourceName,
    cloud_key,
    details,
    guard_all_failed,
    load_shell,
)


class TabsReport(BaseModel):
    """``list tabs`` 的输出 —— 按设备分组，一个 BSO 就是一台设备。

    与 history / bookmarks 的报告**没有共同基类**：共享的是形状，用 ``report.ListReport``
    这个 union 表达。序列化在 ``ffinfo_cli/render.py``。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 5
    """**5**：报告拆成三份互不继承的类型（形状不变，仍是 v4 的扁平字段）。"""
    data_type: str
    generated_at: str
    filters: dict[str, str | int | None]
    records: int
    """库里读出来的记录条数。"""
    synced_at: str | None = None
    """这个 collection **上次成功 sync** 的时间（UTC ISO）。从未同步过就是 ``null``。"""
    age_seconds: float | None = None
    """``synced_at`` 距现在多少秒：**数据有多陈**。"""
    firefox_records: int = 0
    """v4 外壳留下的字段 —— 标签页没有 firefox 源，恒为 0；保留是为了形状逐字段不变。"""
    sources: list[SourceName] = []
    """v4 外壳留下的字段 —— 标签页没有多源合并，恒为空；保留是为了形状逐字段不变。"""
    skipped: int
    """没能进结果的记录条数（解密失败、或建树时丢弃的病态记录）—— 单条坏掉不连坐。"""
    skipped_details: list[dict[str, str]] = []
    notes: list[str] = []
    """结果为空但**不是失败**时的提示 —— 比如 ``--device`` 什么都没命中。退出码照旧 0。"""
    matched: int
    """过滤之后剩多少条 —— 对 tabs 是标签页数（设备是分组，不计）。"""
    returned: int
    """实际返回多少条（``--limit`` 之后，口径与 ``matched`` 相同）。"""
    clients: list[ClientTabs] = []
    """按设备分组。"""


async def run_tabs(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    device: str | None = None,
    limit: int | None = None,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> TabsReport:
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
    common: ShellCommon,
    *,
    device: str | None,
    limit: int | None,
) -> TabsReport:
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

    return TabsReport(
        generated_at=common.generated_at,
        data_type=common.data_type,
        synced_at=common.synced_at,
        age_seconds=common.age_seconds,
        filters=common.filters,
        records=common.records,
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
