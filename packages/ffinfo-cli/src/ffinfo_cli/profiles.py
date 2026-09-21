"""``ffinfo-cli profiles``：看一眼本地状态。

两个常见的卡壳都在这里出口：

* **"profile 在哪？"** —— ``export`` 找不到 profile 时会让你用 ``--profile`` 指定，
  这里把候选（以及**找过哪些目录**）列出来
* **"同步到哪了？"** —— 库里各 collection 的游标与条数

**只读**：库不存在就是"还没同步过"，不建库。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ffinfo_cli.paths import config_dir, credentials_path, data_dir, database_path, identity_path
from ffinfo_cli.places import HostContext, discover_profiles, firefox_roots
from ffinfo_cli.store import open_database

__all__ = ["LocalPaths", "ProfilesReport", "build_report", "default_paths", "profiles_blocking"]


@dataclass(frozen=True, slots=True)
class LocalPaths:
    """这台机器上的默认落点 —— 由 CLI 层决定（库不认识任何默认路径）。"""

    config_dir: Path
    data_dir: Path
    identity: Path
    credentials: Path
    database: Path


def default_paths() -> LocalPaths:
    """按平台算出默认落点（与其它命令用的是同一套 ``paths``）。"""
    return LocalPaths(
        config_dir=config_dir(),
        data_dir=data_dir(),
        identity=identity_path(),
        credentials=credentials_path(),
        database=database_path(),
    )


class FileInfo(BaseModel):
    """一个文件或目录的落点与存在性。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    path: str
    exists: bool


class ProfileInfo(BaseModel):
    """一个**能用的** Firefox profile。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    path: str
    is_default: bool


class CollectionProgress(BaseModel):
    """一个 collection 的同步进度。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    collection: str
    records: int
    last_modified: float
    synced_at: str
    """上次同步完成的时间（UTC ISO）。"""


class ProfilesReport(BaseModel):
    """``profiles`` 的输出。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 1
    generated_at: str
    platform: str
    home: str
    profiles: list[ProfileInfo] = []
    """探测到的 profile —— 列出来的都**真的有** ``places.sqlite``（与 export 挑的是同一批）。"""
    searched_roots: list[FileInfo] = []
    """找过哪些目录 —— 一个 profile 都没找到时，照着这个核对。"""
    config_dir: FileInfo
    data_dir: FileInfo
    identity: FileInfo
    credentials: FileInfo
    database: FileInfo
    collections: list[CollectionProgress] = []
    notes: list[str] = []
    """下一步能做什么 —— 缺什么说什么。"""

    def to_json(self) -> str:
        """给 agent 消费的 JSON。"""
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


async def build_report(
    *,
    host: HostContext,
    paths: LocalPaths,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> ProfilesReport:
    """把本地状态凑成一份报告。

    ``host`` 由调用者注入 —— 测试才塞得进假环境。
    """
    found = discover_profiles(host=host)
    roots = [FileInfo(path=str(root), exists=root.is_dir()) for root in firefox_roots(host=host)]

    collections: list[CollectionProgress] = []
    db_note: str | None = None
    if paths.database.is_file():
        try:
            store = await open_database(paths.database, warn=warn)
            collections = [
                CollectionProgress(
                    collection=cursor.collection,
                    records=cursor.records,
                    last_modified=cursor.last_modified,
                    synced_at=datetime.fromtimestamp(cursor.synced_at, tz=UTC).isoformat(),
                )
                for cursor in await store.load_cursors()
            ]
        except (OSError, sqlite3.Error) as exc:
            db_note = f"本地库打不开：{exc}"

    return ProfilesReport(
        generated_at=datetime.fromtimestamp(clock(), tz=UTC).isoformat(),
        platform=host.platform,
        home=str(host.home),
        profiles=[
            ProfileInfo(name=profile.name, path=str(profile.path), is_default=profile.is_default)
            for profile in found
        ],
        searched_roots=roots,
        config_dir=FileInfo(path=str(paths.config_dir), exists=paths.config_dir.is_dir()),
        data_dir=FileInfo(path=str(paths.data_dir), exists=paths.data_dir.is_dir()),
        identity=FileInfo(path=str(paths.identity), exists=paths.identity.is_file()),
        credentials=FileInfo(path=str(paths.credentials), exists=paths.credentials.is_file()),
        database=FileInfo(path=str(paths.database), exists=paths.database.is_file()),
        collections=collections,
        notes=_notes(
            has_profiles=bool(found),
            credentials_exist=paths.credentials.is_file(),
            collections=collections,
            db_note=db_note,
        ),
    )


def _notes(
    *,
    has_profiles: bool,
    credentials_exist: bool,
    collections: list[CollectionProgress],
    db_note: str | None,
) -> list[str]:
    """缺什么说什么 —— 每条都得能照着做。"""
    notes: list[str] = []
    if db_note is not None:
        notes.append(db_note)
    if not has_profiles:
        notes.append(
            "这台机器上没找到 Firefox profile（找过的目录见 searched_roots）—— "
            "firefox 那半历史要在**有 Firefox 的机器**上 `ffinfo-cli export`，再 import 过来。"
        )
    if not credentials_exist:
        notes.append("还没登录过：先跑 `ffinfo-cli login`。")
    if not collections:
        notes.append("还没有同步过：先跑 `ffinfo-cli sync`。")
    return notes


def profiles_blocking(
    *,
    host: HostContext,
    paths: LocalPaths | None = None,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> ProfilesReport:
    """:func:`build_report` 的同步外壳。"""
    return asyncio.run(
        build_report(
            host=host,
            paths=paths if paths is not None else default_paths(),
            warn=warn,
            clock=clock,
        )
    )
