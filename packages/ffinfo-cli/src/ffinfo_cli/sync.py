"""``ffinfo-cli sync`` —— 从 Firefox Sync 拉一个 collection 并落盘（04 号 ticket）。

**拉全了才写库。** 中途被要求退避、集合被改、条数对不上 —— 库里一个字节都不会动。
半截数据比没有数据更坏：agent 分不出"这个账号就这么多"还是"上次没拉完"。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.errors import ConfigurationError
from ffinfo.keys import OLD_SYNC_SCOPE
from ffinfo.oauth import Credentials
from ffinfo.storage import MOZILLA_TOKEN_SERVER, SyncStorageClient
from ffinfo_cli.store import open_database, replace_collection

_HTTP_TIMEOUT_SECONDS: Final = 60.0

SYNCABLE_COLLECTIONS: Final = frozenset({"history", "bookmarks", "tabs"})
"""允许拉取的 collection —— **白名单，不是黑名单**。

设计文档决策 4 说数据范围是"历史 + 书签 + 标签页"，这里就是那句话的代码形态。
别的不是拉不动，是**不该拉**：

* ``forms`` —— 服务器上还躺着 42768 条遗留记录（2026-09-14 实测），引擎早就死了。
  内容无从考证，老 Firefox 的表单历史里可能混着当年填过的敏感内容（**包括密码**）
* ``passwords`` / ``creditcards`` / ``addresses`` —— 登录凭据与支付信息，不在本项目范围内

用白名单而不是黑名单：将来 Mozilla 再冒出什么新 collection，默认是"不碰"。"""


class SyncReport(BaseModel):
    """一次 sync 的结果 —— 直接就是 ``--json`` 的输出。"""

    model_config = ConfigDict(frozen=True)

    format_version: int = 1
    collection: str
    records: int
    pages: int
    tombstones: int
    server_count: int | None
    database: str
    elapsed_seconds: float

    def to_json(self) -> str:
        """给 agent 消费的 JSON。"""
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


def load_credentials(*, identity_path: Path, credentials_path: Path) -> Credentials:
    """把 02 存下来的凭据解出来。"""
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
    clock: Callable[[], float] = time.time,
) -> SyncReport:
    """拉一个 collection，校验通过后整体落盘。

    HTTP 客户端与时钟由调用者注入 —— 测试才能塞 mock、不真的 ``sleep``。
    """
    if collection not in SYNCABLE_COLLECTIONS:
        allowed = "、".join(sorted(SYNCABLE_COLLECTIONS))
        msg = (
            f"不拉 collection「{collection}」—— 本项目只拉这几个：{allowed}。"
            "理由见 docs/design.md §3.7：forms 里是来历不明的遗留记录，"
            "passwords / creditcards / addresses 不在本项目范围内"
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
    fetch = await client.fetch_collection(collection, page_size=page_size)
    engine = await open_database(database_path)
    stored = await replace_collection(engine, collection, fetch.records)

    return SyncReport(
        collection=collection,
        records=stored,
        pages=fetch.pages,
        tombstones=sum(1 for record in fetch.records if record.is_tombstone),
        server_count=fetch.server_count,
        database=str(database_path),
        elapsed_seconds=round(clock() - started, 2),
    )


def sync_blocking(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    collection: str,
    page_size: int = 100,
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
            )

    return asyncio.run(_main())
