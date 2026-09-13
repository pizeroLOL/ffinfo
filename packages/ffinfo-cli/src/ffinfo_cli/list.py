"""``ffinfo-cli list`` —— 把库里的加密记录解密成人能看的浏览数据，输出 JSON。

**纯本地、瞬时**：一条网络请求都不发。所以它要求库里已经有两样东西 ——
目标 collection 的记录，以及 ``crypto`` 里那条 ``keys``（解密密钥）。两者都由 ``sync`` 负责。

三种数据类型共用同一条管线（读库 → 解密钥 → 批量解密 → 过滤 → 出 JSON），
只有**解析**和**呈现形状**不同：

| ``--data-type`` | 形状 | 为什么 |
| --- | --- | --- |
| ``history`` | 一次访问一行，最新的在前 | 这才是"浏览历史" |
| ``bookmarks`` | **树** | 父子层级是书签的主要信息，拍平就没了 |
| ``tabs`` | 按设备分组 | 一个 BSO 就是一台设备 |

两个约定，别混：

* **输出**的时间一律是 **UTC**（带偏移量，无歧义，跟在哪儿跑无关）
* ``--since`` **输入**如果没写时区，按**本机时区**理解 —— 人打 ``--since 2026-09-13``
  说的是自己那天，不是 UTC 那天
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Self
from urllib.parse import urlsplit

from piccolo.engine.sqlite import SQLiteEngine
from pydantic import BaseModel, ConfigDict

from ffinfo.bookmarks import BookmarkNode, parse_bookmarks
from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import ConfigurationError, DecryptionError
from ffinfo.history import HistoryEntry, decrypt_history
from ffinfo.keys import CollectionKeys
from ffinfo.tabs import ClientTabs, TabEntry, parse_tabs
from ffinfo_cli.store import load_records, open_database
from ffinfo_cli.sync import load_credentials

_KEYS_RECORD_ID: Final = "keys"
"""``crypto/keys`` 那条记录的 id —— 密钥层次里最关键的一条。"""

_MAX_SKIPPED_DETAILS: Final = 10
"""JSON 里最多列几条解密失败的明细。够定位就行，别把整页灌进去。"""

DATA_TYPES: Final = ("history", "bookmarks", "tabs")
"""``--data-type`` 的取值。"""

_EXCLUDED_FIELDS: Final[dict[str, set[str]]] = {
    "history": {"tree", "counts", "clients"},
    "bookmarks": {"items", "visits", "clients"},
    "tabs": {"items", "visits", "tree", "counts"},
}
"""每种类型不输出的字段 —— 免得 JSON 里躺着一堆空数组。"""


class HistoryItem(BaseModel):
    """一条浏览记录。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    url: str
    title: str
    visited_at: str
    """ISO 8601，UTC。"""
    visit_type: int
    visit_type_name: str
    record_id: str

    @classmethod
    def from_history(cls, entry: HistoryEntry) -> Self:
        """从库层的记录转成输出形态。"""
        return cls(
            url=entry.url,
            title=entry.title,
            visited_at=entry.visited_at.isoformat(),
            visit_type=entry.visit_type,
            visit_type_name=entry.visit_type_name,
            record_id=entry.record_id,
        )


class ListReport(BaseModel):
    """``list --json`` 的输出。三种数据类型共用一个外壳。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 1
    data_type: str
    generated_at: str
    filters: dict[str, str | int | None]
    records: int
    """库里读出来的记录条数。"""
    visits: int = 0
    """``history`` 用：拍平后、过滤前的访问次数（一条记录可以有多次访问）。"""
    skipped: int
    """解密失败的记录条数 —— 单条坏掉不连坐。"""
    skipped_details: list[dict[str, str]] = []
    matched: int
    """过滤之后剩多少条。"""
    returned: int
    """实际返回多少条（``--limit`` 之后）。"""
    items: list[HistoryItem] = []
    """``history`` 用。"""
    tree: list[BookmarkNode] = []
    """``bookmarks`` 用 —— **保留层级**，不是拍平的表。"""
    counts: dict[str, int] = {}
    """``bookmarks`` 用：各类节点各有多少。"""
    clients: list[ClientTabs] = []
    """``tabs`` 用 —— 按设备分组。"""

    def to_json(self) -> str:
        """给 agent 消费的 JSON。不相关的字段直接不输出。"""
        payload = self.model_dump(exclude=_EXCLUDED_FIELDS[self.data_type])
        return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_since(raw: str) -> datetime:
    """解析 ``--since``。裸日期/时间按**本机时区**理解，返回 UTC。"""
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        msg = f"--since 看不懂：{raw!r} —— 要 YYYY-MM-DD 或 ISO 8601 时间"
        raise ConfigurationError(msg) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC)


def matches_domain(url: str | None, domain: str) -> bool:
    """域名匹配 —— ``example.com`` 也要能匹配 ``www.example.com``。"""
    if url is None:
        return False
    host = (urlsplit(url).hostname or "").lower()
    wanted = domain.strip().lower().lstrip(".")
    return host == wanted or host.endswith(f".{wanted}")


def matches_search(*fields: str | None, needle: str) -> bool:
    """在若干字段里找子串（不区分大小写）。"""
    lowered = needle.strip().lower()
    return any(field is not None and lowered in field.lower() for field in fields)


async def run_list(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    data_type: str = "history",
    since: datetime | None = None,
    domain: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    clock: Callable[[], float] = time.time,
) -> ListReport:
    """读库 → 解密 → 过滤 → 出报告。全程不联网。"""
    if data_type not in DATA_TYPES:
        allowed = "、".join(DATA_TYPES)
        msg = f"不认识的 --data-type「{data_type}」—— 只能是：{allowed}"
        raise ConfigurationError(msg)

    credentials = load_credentials(identity_path=identity_path, credentials_path=credentials_path)
    engine = await open_database(database_path)
    key = await _collection_key(engine, credentials.sync_key_bundle(), data_type)
    records = await load_records(engine, data_type)

    common = {
        "generated_at": datetime.fromtimestamp(clock(), tz=UTC).isoformat(),
        "data_type": data_type,
        "filters": {
            "since": since.isoformat() if since is not None else None,
            "domain": domain,
            "search": search,
            "limit": limit,
        },
        "records": len(records),
    }

    if data_type == "history":
        return _history_report(
            records, key, common, since=since, domain=domain, search=search, limit=limit
        )
    if data_type == "bookmarks":
        return _bookmark_report(
            records, key, common, since=since, domain=domain, search=search, limit=limit
        )
    return _tabs_report(
        records, key, common, since=since, domain=domain, search=search, limit=limit
    )


def _history_report(
    records: Sequence[tuple[str, str | None]],
    key: KeyBundle,
    common: dict[str, Any],
    *,
    since: datetime | None,
    domain: str | None,
    search: str | None,
    limit: int | None,
) -> ListReport:
    """历史：拍平成一次访问一行，最新的在前。"""
    decrypted = decrypt_history(records, key)
    _guard_all_failed(decrypted.records, len(decrypted.entries), len(decrypted.skipped))

    selected = list(decrypted.entries)
    if since is not None:
        selected = [entry for entry in selected if entry.visited_at >= since]
    if domain is not None:
        selected = [entry for entry in selected if matches_domain(entry.url, domain)]
    if search is not None:
        selected = [
            entry for entry in selected if matches_search(entry.url, entry.title, needle=search)
        ]
    selected.sort(key=lambda entry: entry.visited_at, reverse=True)

    returned = _truncate(selected, limit)
    return ListReport(
        **common,
        visits=len(decrypted.entries),
        skipped=len(decrypted.skipped),
        skipped_details=_details(decrypted.skipped),
        matched=len(selected),
        returned=len(returned),
        items=[HistoryItem.from_history(entry) for entry in returned],
    )


def _bookmark_report(
    records: Sequence[tuple[str, str | None]],
    key: KeyBundle,
    common: dict[str, Any],
    *,
    since: datetime | None,
    domain: str | None,
    search: str | None,
    limit: int | None,
) -> ListReport:
    """书签：**保留树**。过滤只作用在书签上，筛空的文件夹跟着剪掉。"""
    decrypted = parse_bookmarks(records, key)
    _guard_all_failed(decrypted.records, len(decrypted.roots), len(decrypted.skipped))

    def keep(node: BookmarkNode) -> bool:
        if since is not None and (node.added_at is None or node.added_at < since.isoformat()):
            return False
        if domain is not None and not matches_domain(node.url, domain):
            return False
        return not (search is not None and not matches_search(node.title, node.url, needle=search))

    tree = _prune(decrypted.roots, keep)
    matched = sum(1 for _ in _walk(tree))
    returned = _truncate(_flatten(tree), limit)
    kept_ids = {node.id for node in returned}

    return ListReport(
        **common,
        skipped=len(decrypted.skipped),
        skipped_details=_details(decrypted.skipped),
        matched=matched,
        returned=len(returned),
        tree=_prune(decrypted.roots, lambda node: node.id in kept_ids),
        counts=decrypted.counts(),
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
    _guard_all_failed(decrypted.records, len(decrypted.clients), len(decrypted.skipped))

    def keep(entry: TabEntry) -> bool:
        if since is not None and (
            entry.last_used_at is None or entry.last_used_at < since.isoformat()
        ):
            return False
        if domain is not None and not matches_domain(entry.url, domain):
            return False
        return not (
            search is not None and not matches_search(entry.title, entry.url, needle=search)
        )

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
        skipped_details=_details(decrypted.skipped),
        matched=matched,
        returned=sum(client.count for client in clients),
        clients=clients,
    )


def _guard_all_failed(records: int, produced: int, skipped: int) -> None:
    """一条都解不开时别装没事 —— 多半是换了账号。"""
    if records and not produced and skipped == records:
        msg = (
            "库里的记录一条都解不开 —— 多半是这份凭据和库里的数据不是同一个账号"
            "（换了账号就重新 login 再 sync）"
        )
        raise ConfigurationError(msg)


def _details(skipped: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {"record_id": record_id, "reason": reason}
        for record_id, reason in skipped[:_MAX_SKIPPED_DETAILS]
    ]


def _truncate(entries: list[Any], limit: int | None) -> list[Any]:
    return entries[: max(limit, 0)] if limit is not None else entries


def _prune(
    nodes: Sequence[BookmarkNode], keep: Callable[[BookmarkNode], bool]
) -> list[BookmarkNode]:
    """递归剪枝：文件夹只要有**任意一个后代**留下就保留。"""
    kept: list[BookmarkNode] = []
    for node in nodes:
        children = _prune(node.children, keep)
        if children or (keep(node) and node.type != "folder"):
            kept.append(node.model_copy(update={"children": children}))
    return kept


def _flatten(nodes: Sequence[BookmarkNode]) -> list[BookmarkNode]:
    """深度优先拍平 —— 只用来数数和截断，输出仍然是树。"""
    out: list[BookmarkNode] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node.children))
    return out


def _walk(nodes: Sequence[BookmarkNode]) -> list[BookmarkNode]:
    return _flatten(nodes)


async def _collection_key(engine: SQLiteEngine, root_key: KeyBundle, collection: str) -> KeyBundle:
    """从库里的 ``crypto/keys`` 解出目标 collection 用的那一对密钥。"""
    crypto = await load_records(engine, "crypto")
    payload = next((record for record_id, record in crypto if record_id == _KEYS_RECORD_ID), None)
    if payload is None:
        msg = (
            "库里没有 crypto/keys —— 解密密钥还没同步下来。"
            "先跑一次 `ffinfo-cli sync`（它会顺带拉这条记录）"
        )
        raise ConfigurationError(msg)
    try:
        keys = CollectionKeys.from_encrypted_payload(EncryptedPayload.from_json(payload), root_key)
    except DecryptionError as exc:
        msg = "crypto/keys 解不开 —— 这份凭据和库里的数据不是同一个账号？"
        raise ConfigurationError(msg) from exc
    return keys.key_for_collection(collection)


def list_blocking(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    data_type: str = "history",
    since: datetime | None = None,
    domain: str | None = None,
    search: str | None = None,
    limit: int | None = None,
) -> ListReport:
    """:func:`run_list` 的同步外壳。纯本地，所以没有 HTTP 客户端要开。"""
    return asyncio.run(
        run_list(
            identity_path=identity_path,
            credentials_path=credentials_path,
            database_path=database_path,
            data_type=data_type,
            since=since,
            domain=domain,
            search=search,
            limit=limit,
        )
    )
