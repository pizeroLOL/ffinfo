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

**历史是双源的**：云端 Sync 拉下来的（``sync_records``，要解密）和本地 ``places.sqlite``
导入进来的（``local_visits``，本来就是明文）在查询时合并。合并按
``(url, 访问时刻)`` —— **逐微秒相等才算同一次访问**，这样"两个源都有"的那条只出一行、
标成 ``both``，而不是重复两行。只有一边有就照常出，标 ``sync`` 或 ``local``。
本地源是空的（目标机器没导入过）就自然降级成单源，不用特判。

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Literal
from urllib.parse import urlsplit

from piccolo.engine.sqlite import SQLiteEngine
from pydantic import BaseModel, ConfigDict

from ffinfo.bookmarks import BookmarkNode, parse_bookmarks
from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import ConfigurationError, DecryptionError
from ffinfo.history import HistoryEntry, decrypt_history, visit_type_name
from ffinfo.keys import CollectionKeys
from ffinfo.tabs import ClientTabs, TabEntry, parse_tabs
from ffinfo_cli._time import to_microseconds
from ffinfo_cli.store import StoredVisit, load_local_visits, load_records, open_database
from ffinfo_cli.sync import load_credentials

_KEYS_RECORD_ID: Final = "keys"
"""``crypto/keys`` 那条记录的 id —— 密钥层次里最关键的一条。"""

_MAX_SKIPPED_DETAILS: Final = 10
"""JSON 里最多列几条解密失败的明细。够定位就行，别把整页灌进去。"""

type VisitSource = Literal["sync", "local", "both"]
"""一条访问打哪儿来 —— **三个值就是全部**，写错了 pyright 当场红。

这个字段是给 agent 消费的契约（见 ``format_version``），不是内部枚举：
``sync`` 云端 · ``local`` 本地 places · ``both`` 两边都有（合并后只出一行）。"""

type SourceName = Literal["sync", "local"]
"""``sources`` 里出现的源名 —— ``both`` 不属于这里，它是**合并之后**才有的结论。"""

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
    record_id: str | None
    """云端那条记录的 GUID。**本地源来的是 ``null``** —— 它压根没有这个 id。"""
    source: VisitSource
    """这条打哪儿来：``sync``（云端）· ``local``（本地 places）· ``both``（两边都有）。"""
    source_machine: str | None
    """本地源那边导出它的机器名。``source`` 是 ``sync`` 时是 ``null``。"""


class ListReport(BaseModel):
    """``list --json`` 的输出。三种数据类型共用一个外壳。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 2
    """**2**：历史条目加了 ``source`` / ``source_machine``，``record_id`` 可为 null。"""
    data_type: str
    generated_at: str
    filters: dict[str, str | int | None]
    records: int
    """库里读出来的记录条数。"""
    visits: int = 0
    """``history`` 用：**云端**拍平后、过滤前的访问次数（一条记录可以有多次访问）。"""
    local_records: int = 0
    """``history`` 用：本地源那边读出来多少条访问。"""
    sources: list[SourceName] = []
    """实际出了数据的源。只有一个时就是**降级到单源**了。"""
    skipped: int
    """没能进结果的记录条数（解密失败、或建树时丢弃的病态记录）—— 单条坏掉不连坐。"""
    skipped_details: list[dict[str, str]] = []
    matched: int
    """过滤之后剩多少条 —— history 是访问次数、bookmarks 是书签条数、tabs 是标签页数
    （文件夹与设备是结构，不计）。"""
    returned: int
    """实际返回多少条（``--limit`` 之后，口径与 ``matched`` 相同）。"""
    items: list[HistoryItem] = []
    """``history`` 用。"""
    tree: list[BookmarkNode] = []
    """``bookmarks`` 用 —— **保留层级**，不是拍平的表。"""
    counts: dict[str, int] = {}
    """``bookmarks`` 用：**返回的这棵树**里各类节点各有多少（含作为结构的文件夹）。"""
    clients: list[ClientTabs] = []
    """``tabs`` 用 —— 按设备分组。"""

    def to_json(self) -> str:
        """给 agent 消费的 JSON。不相关的字段直接不输出。"""
        payload = self.model_dump(exclude=_EXCLUDED_FIELDS[self.data_type])
        return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


def _json_default(value: object) -> str:
    """``json.dumps`` 遇到富类型时的兜底 —— 目前只有时间字段的 ``datetime``。

    **统一走 ``isoformat()``**（``+00:00``），与 history 的字符串格式逐字节一致；
    pydantic 自己的 json 模式会写成 ``Z``，两种风格混在一份输出里不好。
    """
    if isinstance(value, datetime):
        return value.isoformat()
    msg = f"JSON 不认识这个类型：{type(value).__name__}"
    raise TypeError(msg)


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
    # 本地源只有历史这一种 —— 书签与标签页是云端独有
    local = await load_local_visits(engine) if data_type == "history" else ()

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
            records,
            key,
            local,
            common,
            since=since,
            domain=domain,
            search=search,
            limit=limit,
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
    local: Sequence[StoredVisit],
    common: dict[str, Any],
    *,
    since: datetime | None,
    domain: str | None,
    search: str | None,
    limit: int | None,
) -> ListReport:
    """历史：**两个源合并**，拍平成一次访问一行，最新的在前。"""
    decrypted = decrypt_history(records, key)
    _guard_all_failed(
        decrypted.records, len(decrypted.entries), len(decrypted.skipped), fallback=len(local)
    )

    selected = _merge_history(decrypted.entries, local)
    if since is not None:
        selected = [item for item in selected if item.visited_at >= since]
    if domain is not None:
        selected = [item for item in selected if matches_domain(item.url, domain)]
    if search is not None:
        selected = [
            item for item in selected if matches_search(item.url, item.title, needle=search)
        ]
    selected.sort(key=lambda item: item.visited_at, reverse=True)

    returned = _truncate(selected, limit)
    return ListReport(
        **common,
        visits=len(decrypted.entries),
        local_records=len(local),
        sources=_sources(len(decrypted.entries), len(local)),
        skipped=len(decrypted.skipped),
        skipped_details=_details(decrypted.skipped),
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
    entries: Sequence[HistoryEntry], local: Sequence[StoredVisit]
) -> list[_MergedVisit]:
    """把云端与本地两个源并成一个列表。

    **认"同一次访问"靠 ``(url, 微秒)``。** 云端那条的时刻来自记录里的 ``date``，
    本地那条来自 ``moz_historyvisits.visit_date`` —— 两边都是 PRTime 微秒，
    所以只要换算不引入误差，它们就能精确对上。这也是 ``_time`` 里坚持走整数运算的原因：
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

    for item in local:
        key = (item.url, to_microseconds(item.visited_at))
        existing = merged.get(key)
        if existing is None:
            merged[key] = _MergedVisit(
                url=item.url,
                title=item.title,
                visited_at=item.visited_at,
                visit_type=item.visit_type,
                record_id=None,
                source="local",
                machine=item.machine,
            )
            continue
        merged[key] = replace(
            existing,
            # 本地那条没标题时，别把云端已有的标题丢了
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


def _sources(sync_visits: int, local_visits: int) -> list[SourceName]:
    """哪些源真的出了数据 —— 只剩一个就说明这次是**单源降级**。"""
    pairs: tuple[tuple[SourceName, int], ...] = (
        ("sync", sync_visits),
        ("local", local_visits),
    )
    return [name for name, count in pairs if count]


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
    """书签：**保留树**。过滤只作用在书签上，筛空的文件夹跟着剪掉。

    ``--limit`` 数的是**书签条数** —— 文件夹是挂书签用的结构，不占名额
    （与 tabs 那边"设备不占名额"一个道理）。最终树、``returned`` 与
    ``counts`` 都从同一棵树上数出来，互相自洽。
    """
    decrypted = parse_bookmarks(records, key)
    _guard_all_failed(decrypted.records, len(decrypted.roots), len(decrypted.skipped))

    def keep(node: BookmarkNode) -> bool:
        if since is not None and (node.added_at is None or node.added_at < since):
            return False
        if domain is not None and not matches_domain(node.url, domain):
            return False
        return not (search is not None and not matches_search(node.title, node.url, needle=search))

    filtered = _prune(decrypted.roots, keep)
    bookmarks = [node for node in _flatten(filtered) if node.type != "folder"]
    returned = _truncate(bookmarks, limit)
    kept_ids = {node.id for node in returned}
    tree = _prune(filtered, lambda node: node.id in kept_ids)
    not_in_tree = decrypted.skipped + decrypted.dropped

    return ListReport(
        **common,
        skipped=len(not_in_tree),
        skipped_details=_details(not_in_tree),
        matched=len(bookmarks),
        returned=len(returned),
        tree=tree,
        counts=_counts(tree),
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
        if since is not None and (entry.last_used_at is None or entry.last_used_at < since):
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


def _guard_all_failed(records: int, produced: int, skipped: int, *, fallback: int = 0) -> None:
    """一条都解不开时别装没事 —— 多半是换了账号。

    ``fallback`` 是有本地源兜底时的条数：那种情况下查询仍有结果，
    拦下来反而把用户自己的本地数据也一起藏了（报告里的 ``skipped`` 照样会写）。
    """
    if records and not produced and skipped == records and not fallback:
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


def _counts(nodes: Sequence[BookmarkNode]) -> dict[str, int]:
    """树上各类**节点**各有多少（含文件夹 —— 它们也是输出的一部分）。"""
    tally: dict[str, int] = {}
    for node in _flatten(nodes):
        tally[node.type] = tally.get(node.type, 0) + 1
    return tally


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
