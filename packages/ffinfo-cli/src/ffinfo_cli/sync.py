"""``ffinfo-cli sync`` —— 从 Firefox Sync 拉数据并落盘。

**拉全了才写库。** 中途被要求退避、集合被改、条数对不上 —— 库里一个字节都不会动，
**游标也不推进**。半截数据比没有数据更坏：agent 分不出"这个账号就这么多"还是"上次没拉完"。

第一次是全量；之后每次只拉**上次同步之后的变更**（``newer=<游标>``）。
``--full`` 可以强制回到全量 —— 增量拉久了偶尔需要一次全量来"对账"
（服务器会把很老的墓碑清掉，只有全量才能发现那部分删除）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Final

import httpx
from pydantic import BaseModel, ConfigDict

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.errors import ConfigurationError
from ffinfo.keys import OLD_SYNC_SCOPE
from ffinfo.oauth import Credentials
from ffinfo.storage import MOZILLA_TOKEN_SERVER, CollectionFetch, SyncStorageClient
from ffinfo_cli.store import (
    CollectionBatch,
    SyncRecord,
    load_cursor,
    open_database,
    save_cursor,
    store_batches,
)

_HTTP_TIMEOUT_SECONDS: Final = 60.0

SYNCABLE_COLLECTIONS: Final = frozenset({"history", "bookmarks", "tabs"})
"""允许拉取的 collection —— **白名单，不是黑名单**。

数据范围就是"历史 + 书签 + 标签页"。别的不是拉不动，是**不该拉**：

* ``forms`` —— 服务器上还躺着四万多条遗留记录（2026-09-14 实测），引擎早就死了。
  内容无从考证，老 Firefox 的表单历史里可能混着当年填过的敏感内容（**包括密码**）
* ``passwords`` / ``creditcards`` / ``addresses`` —— 登录凭据与支付信息，不在本项目范围内

用白名单而不是黑名单：将来 Mozilla 再冒出什么新 collection，默认是"不碰"。"""

_PROTOCOL_COLLECTIONS: Final = ("crypto",)
"""每次 sync 都顺带拉的**协议数据**。

它不在 ``SYNCABLE_COLLECTIONS`` 里：那个白名单管的是"用户要什么"，
这个管的是"协议需要什么"。``crypto`` 里只有一条 ``keys`` 记录，
装着各 collection 的解密密钥 —— 没有它，库里的记录一条也解不开。
"""


class SyncReport(BaseModel):
    """一次 sync 的结果 —— 直接就是 ``--json`` 的输出。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 1
    collection: str
    mode: str
    """``full``（全量）或 ``incremental``（只拉变更）。"""
    records: int
    """库里现在总共有多少条。"""
    inserted: int
    updated: int
    deleted: int
    pages: int
    tombstones: int
    """这次服务器报了几条删除。"""
    server_count: int | None
    """服务器报告的条数。**增量时为 ``None``** —— 变更集的条数跟全量对不上，比了没意义。"""
    cursor_before: float | None
    cursor_after: float
    database: str
    elapsed_seconds: float
    protocol: dict[str, int] = {}
    """顺带拉下来的协议数据，形如 ``{"crypto": 1}``。"""

    def to_json(self) -> str:
        """给 agent 消费的 JSON。"""
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


def load_credentials(*, identity_path: Path, credentials_path: Path) -> Credentials:
    """把存下来的凭据解出来。"""
    identity = AgeIdentity.from_file(identity_path)
    return Credentials.from_json(CredentialStore(identity=identity, path=credentials_path).load())


async def run_sync(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    collection: str,
    http: httpx.AsyncClient,
    page_size: int = 100,
    full: bool = False,
    clock: Callable[[], float] = time.time,
) -> SyncReport:
    """拉一个 collection（外加协议数据），全部校验通过后一起落盘、再推进游标。

    HTTP 客户端与时钟由调用者注入 —— 测试才能塞 mock、不真的 ``sleep``。
    """
    if collection not in SYNCABLE_COLLECTIONS:
        allowed = "、".join(sorted(SYNCABLE_COLLECTIONS))
        msg = (
            f"不拉 collection「{collection}」—— 本项目只拉这几个：{allowed}。"
            "forms 里是来历不明的遗留记录，passwords / creditcards / addresses "
            "不在本项目范围内"
        )
        raise ConfigurationError(msg)

    credentials = load_credentials(identity_path=identity_path, credentials_path=credentials_path)
    if credentials.is_expired(now=clock()):
        msg = "凭据里的 access token 过期了，重新跑一次 `ffinfo-cli login`"
        raise ConfigurationError(msg)

    scoped = credentials.scoped_keys.get(OLD_SYNC_SCOPE)
    if scoped is None:
        msg = "这份凭据里没有 oldsync 的密钥 —— 重新跑一次 `ffinfo-cli login`"
        raise ConfigurationError(msg)

    client = SyncStorageClient(
        http=http,
        access_token=credentials.access_token,
        key_id=scoped.kid,
        token_server_url=MOZILLA_TOKEN_SERVER,
        clock=clock,
    )

    started = clock()
    engine = await open_database(database_path)
    targets = (collection, *_PROTOCOL_COLLECTIONS)

    # ── 先全部拉下来，一条都别写 ──────────────────────────────────────────
    cursors: dict[str, float | None] = {}
    fetches: dict[str, CollectionFetch] = {}
    batches: list[CollectionBatch] = []
    for name in targets:
        cursor = None if full else await load_cursor(engine, name)
        cursors[name] = cursor
        fetch = await client.fetch_collection(name, page_size=page_size, newer=cursor)
        fetches[name] = fetch
        batches.append(CollectionBatch(collection=name, records=fetch.records, full=cursor is None))

    # ── 全成或全不写 ──────────────────────────────────────────────────────
    results = await store_batches(engine, batches)

    # ── 数据安全落地了，才推进游标 ────────────────────────────────────────
    now = clock()
    for name in targets:
        await save_cursor(
            engine,
            name,
            last_modified=fetches[name].last_modified,
            synced_at=now,
            records=await _count(name),
        )

    main = fetches[collection]
    applied = results[collection]
    return SyncReport(
        collection=collection,
        mode="full" if cursors[collection] is None else "incremental",
        records=await _count(collection),
        inserted=applied.inserted,
        updated=applied.updated,
        deleted=applied.deleted,
        pages=main.pages,
        tombstones=sum(1 for record in main.records if record.is_tombstone),
        server_count=main.server_count,
        cursor_before=cursors[collection],
        cursor_after=main.last_modified,
        database=str(database_path),
        elapsed_seconds=round(clock() - started, 2),
        protocol={name: fetches[name].count for name in _PROTOCOL_COLLECTIONS},
    )


async def _count(collection: str) -> int:
    """库里这个 collection 现在有多少条。"""
    return await SyncRecord.count().where(SyncRecord.collection == collection)


def sync_blocking(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    collection: str,
    page_size: int = 100,
    full: bool = False,
) -> SyncReport:
    """:func:`run_sync` 的同步外壳：自己开 HTTP 客户端。"""

    async def _main() -> SyncReport:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as http:
            return await run_sync(
                identity_path=identity_path,
                credentials_path=credentials_path,
                database_path=database_path,
                collection=collection,
                http=http,
                page_size=page_size,
                full=full,
            )

    return asyncio.run(_main())
