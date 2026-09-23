"""``ffinfo-cli sync`` —— 从 Firefox Sync 拉数据并落盘。

**拉全了才写库。** 中途被要求退避、集合被改、条数对不上 —— 库里一个字节都不会动，
**游标也不推进**：全部 collection 拉完后一次 ``store.commit`` 落盘，records 与游标
同一事务（"拉全了才动、中途失败全不动"的实现与推理在那里的 docstring）。
半截数据比没有数据更坏：agent 分不出"这个账号就这么多"还是"上次没拉完"。

第一次是全量；之后每次只拉**上次同步之后的变更**（``newer=<游标>``）。
``--full`` 可以强制回到全量 —— 增量拉久了偶尔需要一次全量来"对账"
（服务器会把很老的墓碑清掉，只有全量才能发现那部分删除）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar, Final

import httpx
from pydantic import BaseModel, ConfigDict

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.errors import ConfigurationError
from ffinfo.keys import OLD_SYNC_SCOPE
from ffinfo.oauth import Credentials
from ffinfo.storage import (
    MOZILLA_TOKEN_SERVER,
    CollectionFetch,
    FetchProgress,
    SyncStorageClient,
)
from ffinfo_cli.login import refresh_credentials
from ffinfo_cli.store import ApplyResult, CollectionBatch, TargetCursor, open_database

_HTTP_TIMEOUT_SECONDS: Final = 60.0

SYNCABLE_COLLECTIONS: Final = ("history", "bookmarks", "tabs")
"""允许拉取的 collection —— **白名单，不是黑名单**（顺序就是报告与请求的顺序）。

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


class CollectedSync(BaseModel):
    """一个 collection 这一次同步的账。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    collection: str
    mode: str
    """``full``（全量）或 ``incremental``（只拉变更）。"""
    records: int
    """这次从这个 collection 拉下来多少条（含墓碑）。"""
    inserted: int
    """这次新落库的条数。"""
    updated: int
    """这次覆盖的条数（同一个 id 已有，内容更新了）。"""
    deleted: int
    """这次删掉的条数 —— **全量时就是对账的那个数**：服务器上没了的记录。"""
    pages: int
    tombstones: int
    """这次服务器报了几条删除。"""
    server_count: int | None
    """服务器报告的条数。**增量时为 ``None``** —— 变更集的条数跟全量对不上，比了没意义。"""
    cursor_before: float | None
    cursor_after: float


class SyncReport(BaseModel):
    """一次 sync 的结果 —— 直接就是 ``--json`` 的输出。

    顶层是"总账 + 明细"：``collections`` 是用户要的那几样，``protocol`` 是协议需要的那几样。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 2
    collections: list[CollectedSync]
    elapsed_seconds: float
    database: str
    protocol: dict[str, int] = {}
    """顺带拉下来的协议数据，形如 ``{"crypto": 1}``。"""


def _report_entry(
    name: str,
    fetch: CollectionFetch,
    applied: ApplyResult,
    cursor_before: float | None,
) -> CollectedSync:
    """把一个 collection 这次的账拼成报告明细。

    ``mode`` / ``server_count`` / ``tombstones`` 的口径只写这一处。
    """
    return CollectedSync(
        collection=name,
        mode="full" if cursor_before is None else "incremental",
        records=fetch.count,
        inserted=applied.inserted,
        updated=applied.updated,
        deleted=applied.deleted,
        pages=fetch.pages,
        tombstones=sum(1 for record in fetch.records if record.is_tombstone),
        server_count=fetch.server_count,
        cursor_before=cursor_before,
        cursor_after=fetch.last_modified,
    )


def load_credentials(*, identity_path: Path, credentials_path: Path) -> Credentials:
    """把存下来的凭据解出来。"""
    identity = AgeIdentity.from_file(identity_path)
    return Credentials.from_json(CredentialStore(identity=identity, path=credentials_path).load())


async def run_sync(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    collections: Sequence[str],
    http: httpx.AsyncClient,
    on_progress: Callable[[FetchProgress], None] | None = None,
    warn: Callable[[str], None] | None = None,
    page_size: int = 100,
    full: bool = False,
    clock: Callable[[], float] = time.time,
) -> SyncReport:
    """拉一组 collection（外加协议数据），**全部拉完后一次** ``store.commit`` 落盘。

    records 与各自目标游标同一事务 —— 本函数只做 fetch-then-commit，
    不编排"先写哪、后动哪"（那是 commit 的内部知识）。
    HTTP 客户端与时钟由调用者注入 —— 测试才能塞 mock、不真的 ``sleep``。
    """
    for name in collections:
        if name not in SYNCABLE_COLLECTIONS:
            allowed = "、".join(sorted(SYNCABLE_COLLECTIONS))
            msg = (
                f"不拉 collection「{name}」—— 本项目只拉这几个：{allowed}。"
                "forms 里是来历不明的遗留记录，passwords / creditcards / addresses "
                "不在本项目范围内"
            )
            raise ConfigurationError(msg)

    credentials = load_credentials(identity_path=identity_path, credentials_path=credentials_path)
    if credentials.is_expired(now=clock()):
        # 过期不急着让用户去点浏览器：refresh token 就是干这个的（RFC 6749 §6）。
        # 它也失效了才会抛出来 —— 那时候的 AuthError 已经写明"重新 login"。
        credentials = await refresh_credentials(
            credentials,
            identity_path=identity_path,
            credentials_path=credentials_path,
            http=http,
            now=clock(),
        )

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
    store = await open_database(database_path, warn=warn)
    targets = (*collections, *_PROTOCOL_COLLECTIONS)

    cursors: dict[str, float | None] = {}
    fetches: dict[str, CollectionFetch] = {}
    batches: list[CollectionBatch] = []
    for name in targets:
        cursor = None if full else await store.load_cursor(name)
        cursors[name] = cursor
        fetch = await client.fetch_collection(
            name, page_size=page_size, newer=cursor, on_progress=on_progress
        )
        fetches[name] = fetch
        batches.append(CollectionBatch(collection=name, records=fetch.records, full=cursor is None))

    now = clock()
    results = await store.commit(
        batches,
        [
            TargetCursor(
                collection=name,
                last_modified=fetches[name].last_modified,
                synced_at=now,
            )
            for name in targets
        ],
    )

    return SyncReport(
        collections=[
            _report_entry(name, fetches[name], results[name], cursors[name]) for name in collections
        ],
        elapsed_seconds=round(clock() - started, 2),
        database=str(database_path),
        protocol={name: fetches[name].count for name in _PROTOCOL_COLLECTIONS},
    )


def sync_blocking(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    collections: Sequence[str],
    page_size: int = 100,
    full: bool = False,
    on_progress: Callable[[FetchProgress], None] | None = None,
    warn: Callable[[str], None] | None = None,
) -> SyncReport:
    """:func:`run_sync` 的同步外壳：自己开 HTTP 客户端。

    ``on_progress`` 一路透传到 HTTP 客户端 —— 命令行那层靠它显示"拉到第几页了"。
    """

    async def _main() -> SyncReport:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as http:
            return await run_sync(
                identity_path=identity_path,
                credentials_path=credentials_path,
                database_path=database_path,
                collections=collections,
                http=http,
                page_size=page_size,
                full=full,
                on_progress=on_progress,
                warn=warn,
            )

    return asyncio.run(_main())
