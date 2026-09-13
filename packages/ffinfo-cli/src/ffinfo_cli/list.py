"""``ffinfo-cli list`` —— 把库里的加密记录解密成浏览历史，输出 JSON。

**纯本地、瞬时**：一条网络请求都不发。所以它要求库里已经有两样东西 ——
``history`` 的记录，以及 ``crypto`` 里那条 ``keys``（解密密钥）。两者都由 ``sync`` 负责。

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
from typing import Final, Self
from urllib.parse import urlsplit

from piccolo.engine.sqlite import SQLiteEngine
from pydantic import BaseModel, ConfigDict

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import ConfigurationError, DecryptionError
from ffinfo.history import HistoryEntry, decrypt_history
from ffinfo.keys import CollectionKeys
from ffinfo_cli.store import load_records, open_database
from ffinfo_cli.sync import load_credentials

_KEYS_RECORD_ID: Final = "keys"
"""``crypto/keys`` 那条记录的 id —— 密钥层次里最关键的一条。"""

_MAX_SKIPPED_DETAILS: Final = 10
"""JSON 里最多列几条解密失败的明细。够定位就行，别把整页灌进去。"""


class ListEntry(BaseModel):
    """输出里的一条浏览记录。"""

    model_config = ConfigDict(frozen=True)

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
    """``list --json`` 的输出。"""

    model_config = ConfigDict(frozen=True)

    format_version: int = 1
    generated_at: str
    filters: dict[str, str | int | None]
    records: int
    """库里读出来的记录条数。"""
    visits: int
    """拍平后、过滤前的访问次数（一条记录可以有多次访问）。"""
    skipped: int
    """解密失败的记录条数 —— 单条坏掉不连坐。"""
    skipped_details: list[dict[str, str]] = []
    matched: int
    """过滤之后剩多少条。"""
    returned: int
    """实际返回多少条（``--limit`` 之后）。"""
    items: list[ListEntry]

    def to_json(self) -> str:
        """给 agent 消费的 JSON。"""
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


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


def matches_domain(url: str, domain: str) -> bool:
    """域名匹配 —— ``example.com`` 也要能匹配 ``www.example.com``。"""
    host = (urlsplit(url).hostname or "").lower()
    wanted = domain.strip().lower().lstrip(".")
    return host == wanted or host.endswith(f".{wanted}")


def apply_filters(
    entries: Sequence[HistoryEntry],
    *,
    since: datetime | None = None,
    domain: str | None = None,
    search: str | None = None,
) -> list[HistoryEntry]:
    """过滤 + 排序（最新的在前）。

    **不截断** —— ``--limit`` 是调用方的事。这样"命中多少"和"返回多少"是两个数，
    agent 才看得出后面还有没有。
    """
    selected = list(entries)
    if since is not None:
        selected = [entry for entry in selected if entry.visited_at >= since]
    if domain is not None:
        selected = [entry for entry in selected if matches_domain(entry.url, domain)]
    if search is not None:
        needle = search.strip().lower()
        selected = [
            entry
            for entry in selected
            if needle in entry.url.lower() or needle in entry.title.lower()
        ]
    selected.sort(key=lambda entry: entry.visited_at, reverse=True)
    return selected


async def run_list(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    since: datetime | None = None,
    domain: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    clock: Callable[[], float] = time.time,
) -> ListReport:
    """读库 → 解密 → 过滤 → 出报告。全程不联网。"""
    credentials = load_credentials(identity_path=identity_path, credentials_path=credentials_path)
    engine = await open_database(database_path)

    history_key = await _history_key(engine, credentials.sync_key_bundle())
    records = await load_records(engine, "history")
    decrypted = decrypt_history(records, history_key)

    if decrypted.records and not decrypted.entries and len(decrypted.skipped) == decrypted.records:
        msg = (
            "库里的历史记录一条都解不开 —— 多半是这份凭据和库里的数据不是同一个账号"
            "（换了账号就重新 login 再 sync）"
        )
        raise ConfigurationError(msg)

    matched = apply_filters(decrypted.entries, since=since, domain=domain, search=search)
    returned = matched[: max(limit, 0)] if limit is not None else matched

    return ListReport(
        generated_at=datetime.fromtimestamp(clock(), tz=UTC).isoformat(),
        filters={
            "since": since.isoformat() if since is not None else None,
            "domain": domain,
            "search": search,
            "limit": limit,
        },
        records=decrypted.records,
        visits=len(decrypted.entries),
        skipped=len(decrypted.skipped),
        skipped_details=[
            {"record_id": record_id, "reason": reason}
            for record_id, reason in decrypted.skipped[:_MAX_SKIPPED_DETAILS]
        ],
        matched=len(matched),
        returned=len(returned),
        items=[ListEntry.from_history(entry) for entry in returned],
    )


def list_blocking(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
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
            since=since,
            domain=domain,
            search=search,
            limit=limit,
        )
    )


async def _history_key(engine: SQLiteEngine, root_key: KeyBundle) -> KeyBundle:
    """从库里的 ``crypto/keys`` 解出 history 用的那一对密钥。"""
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
    return keys.key_for_collection("history")
