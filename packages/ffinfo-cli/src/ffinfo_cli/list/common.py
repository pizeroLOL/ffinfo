"""``list`` 三个子命令共用的外壳与工具 —— 取值、过滤口径、契约类型、密钥加载。

三种数据类型曾经共用一条 ``run_list(data_type=…)`` 管线；现在各自是
``run_history`` / ``run_bookmarks`` / ``run_tabs`` 一个入口，**这里只放三边都要的东西**：

* ``SourceName`` / ``VisitSource`` 这两个给 agent 的契约类型；
* ``parse_since`` 与 ``matches_domain`` / ``matches_search`` 这两个过滤口径；
* ``keeper`` / ``truncate`` / ``guard_all_failed`` / ``details`` 这些共享谓词；
* ``load_shell`` —— 读库、读 firefox 源、拼报告里的公共字段；
* ``cloud_key`` —— 只在确实要解密云端记录时才加载凭据。

两个约定，别混：

* **输出**的时间一律是 **UTC**（带偏移量，无歧义，跟在哪儿跑无关）
* ``--since`` **输入**如果没写时区，按**本机时区**理解 —— 人打 ``--since 2026-09-13``
  说的是自己那天，不是 UTC 那天
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import ConfigurationError, DecryptionError
from ffinfo.keys import CollectionKeys
from ffinfo_cli.store import Store, StoredVisit, open_database
from ffinfo_cli.sync import load_credentials

_KEYS_RECORD_ID: Final = "keys"
"""``crypto/keys`` 那条记录的 id —— 密钥层次里最关键的一条。"""

_MAX_SKIPPED_DETAILS: Final = 10
"""JSON 里最多列几条解密失败的明细。够定位就行，别把整页灌进去。"""

type VisitSource = Literal["sync", "firefox", "both"]
"""一条访问打哪儿来 —— **三个值就是全部**，写错了 pyright 当场红。

这个字段是给 agent 消费的契约（见 ``format_version``），不是内部枚举：
``sync`` 云端 · ``firefox`` firefox 源 · ``both`` 两边都有（合并后只出一行）。"""

type SourceName = Literal["sync", "firefox"]
"""``sources`` 里出现的源名 —— ``both`` 不属于这里，它是**合并之后**才有的结论。"""


@dataclass(frozen=True, slots=True)
class Shell:
    """一次 ``list`` 查询的公共外壳：库句柄、记录、firefox 源、报告公共字段。"""

    store: Store
    records: Sequence[tuple[str, str | None]]
    firefox: Sequence[StoredVisit]
    common: dict[str, Any]


async def load_shell(
    *,
    database_path: Path,
    collection: str,
    filters: dict[str, str | int | None],
    warn: Callable[[str], None] | None,
    clock: Callable[[], float],
) -> Shell:
    """读库、读 firefox 源、拼出报告公共字段 —— 三个入口唯一会重复的一步。

    ``filters`` 是**这个类型自己的键**（history 是 ``since`` / ``domain`` / ``search`` / ``limit``，
    bookmarks 是 ``path`` / ``limit``，tabs 是 ``device`` / ``limit``）—— 口径由各自的入口拼。
    """
    store = await open_database(database_path, warn=warn)
    records = await store.load_records(collection)
    # firefox 源只有历史这一种 —— 书签与标签页是云端独有
    firefox = await store.load_firefox_visits() if collection == "history" else ()
    # 数据新鲜度：sync 挂了的时候 list 照样输出，但"陈"这件事要有字段说出来
    cursor = next(
        (item for item in await store.load_cursors() if item.collection == collection), None
    )
    now = clock()
    common: dict[str, Any] = {
        "generated_at": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        "data_type": collection,
        "synced_at": (
            datetime.fromtimestamp(cursor.synced_at, tz=UTC).isoformat()
            if cursor is not None
            else None
        ),
        "age_seconds": round(now - cursor.synced_at, 1) if cursor is not None else None,
        "filters": filters,
        "records": len(records),
    }
    return Shell(store=store, records=records, firefox=firefox, common=common)


async def cloud_key(
    *, store: Store, identity_path: Path, credentials_path: Path, collection: str
) -> KeyBundle:
    """加载凭据 → 派生这个 collection 的密钥。**只在确实要解密云端记录时调用。**

    解密链（age 私钥 → 凭据 → scoped key → ``crypto/keys``）只为云端密文而存在。
    纯 firefox 源的查询一步都不走这里 —— 从没 login 过的目标机器不该被挡在外面。
    """
    credentials = load_credentials(identity_path=identity_path, credentials_path=credentials_path)
    return await _collection_key(store, credentials.sync_key_bundle(), collection)


async def _collection_key(store: Store, root_key: KeyBundle, collection: str) -> KeyBundle:
    """从库里的 ``crypto/keys`` 解出目标 collection 用的那一对密钥。"""
    crypto = await store.load_records("crypto")
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


def guard_all_failed(records: int, produced: int, skipped: int, *, fallback: int = 0) -> None:
    """一条都解不开时别装没事 —— 多半是换了账号。

    ``fallback`` 是有 firefox 源兜底时的条数：那种情况下查询仍有结果，
    拦下来反而把用户自己的 firefox 数据也一起藏了（报告里的 ``skipped`` 照样会写）。
    """
    if records and not produced and skipped == records and not fallback:
        msg = (
            "库里的记录一条都解不开 —— 多半是这份凭据和库里的数据不是同一个账号"
            "（换了账号就重新 login 再 sync）"
        )
        raise ConfigurationError(msg)


def details(skipped: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    """把解密失败的明细截断后转成 JSON 形态 —— 够定位就行。"""
    return [
        {"record_id": record_id, "reason": reason}
        for record_id, reason in skipped[:_MAX_SKIPPED_DETAILS]
    ]


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


def keeper(
    *, since: datetime | None, domain: str | None, search: str | None
) -> Callable[..., bool]:
    """把 ``--since`` / ``--domain`` / ``--search`` 绑成一个谓词 —— **口径只写这一处**。

    只有 ``history`` 用语义过滤（bookmarks / tabs 改成了结构性的 ``--path`` / ``--device``）；
    它提供自己的字段（``when`` / ``url`` / ``title``）；``when`` 缺失算不匹配
    （没有时间的记录进不了"某时间之后"）。三个过滤器一起作用，不是逐个筛。
    """

    def matches(*, when: datetime | None, url: str | None, title: str) -> bool:
        if since is not None and (when is None or when < since):
            return False
        if domain is not None and not matches_domain(url, domain):
            return False
        return not (search is not None and not matches_search(title, url, needle=search))

    return matches


def truncate(entries: list[Any], limit: int | None) -> list[Any]:
    """按结果上限截断 —— ``None`` 不截断，负数当 0。"""
    return entries[: max(limit, 0)] if limit is not None else entries
