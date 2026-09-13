"""标签页记录的解析与解密。

真实结构（2026-09-14 实测）::

    {
        "id": "aB3dEf6hIj9k",
        "clientName": "user 在 laptop 上使用的 Firefox",
        "tabs": [
            {
                "title": "…",
                "urlHistory": ["https://…"],
                "icon": "https://…",
                "lastUsed": 1682745266,
                "windowId": "window-0",
            }
        ],
    }

三个坑：

1. **``lastUsed`` 是秒** —— 历史那边是**微秒**，书签那边是**毫秒**。
   三个 collection 三个单位，混了时间就飘。
2. **``urlHistory`` 是一个数组**（同一个标签页里回退过的历史），
   取第一个当"这个标签页在看什么"。
3. **``icon`` 可以是 ``null``**，``windowId`` 可以整个不存在。

一个 BSO 就是**一台设备**（``clientName`` 是它自己的名字），
所以这里按设备分组，而不是把标签页拍成一长条。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ffinfo._decrypt import decrypt_records
from ffinfo.crypto import KeyBundle

__all__ = [
    "ClientTabs",
    "Tab",
    "TabEntry",
    "TabsRecord",
    "TabsReport",
    "parse_tabs",
]


class Tab(BaseModel):
    """一个标签页。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="ignore", populate_by_name=True
    )

    title: str = ""
    url_history: tuple[str, ...] = Field(default=(), alias="urlHistory")
    icon: str | None = None
    last_used: int | None = Field(default=None, alias="lastUsed")
    window_id: str | None = Field(default=None, alias="windowId")

    @property
    def url(self) -> str | None:
        """这个标签页在看什么 —— ``urlHistory`` 的第一条。"""
        return self.url_history[0] if self.url_history else None

    @property
    def last_used_at(self) -> datetime | None:
        """最后使用时间（UTC）。``lastUsed`` 是**秒**。"""
        if self.last_used is None:
            return None
        return datetime.fromtimestamp(self.last_used, tz=UTC)


class TabsRecord(BaseModel):
    """一台设备报上来的标签页。**一个 BSO = 一台设备。**"""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="ignore", populate_by_name=True
    )

    id: str
    client_name: str = Field(default="", alias="clientName")
    tabs: tuple[Tab, ...] = ()


@dataclass(frozen=True, slots=True)
class TabEntry:
    """拍平之后的一个标签页（仍然带着它属于哪台设备）。"""

    client_id: str
    client_name: str
    title: str
    url: str | None
    last_used_at: datetime | None
    """最后使用时间（UTC ``datetime``）—— 与 history / bookmarks 同一个口径，JSON 里仍是 ISO。"""
    icon: str | None
    window_id: str | None


@dataclass(frozen=True, slots=True)
class ClientTabs:
    """一台设备的标签页。"""

    client_id: str
    client_name: str
    tabs: tuple[TabEntry, ...]

    @property
    def count(self) -> int:
        """这台设备报了几个标签页。"""
        return len(self.tabs)


@dataclass(frozen=True, slots=True)
class TabsReport:
    """批量解密的结果。"""

    clients: tuple[ClientTabs, ...]
    skipped: tuple[tuple[str, str], ...]
    tombstones: int
    records: int
    """看过的记录条数（一个记录 = 一台设备）。"""

    @property
    def entries(self) -> tuple[TabEntry, ...]:
        """所有标签页，拍平 —— 过滤和搜索要在这上面做。"""
        return tuple(tab for client in self.clients for tab in client.tabs)


def parse_tabs(records: Iterable[tuple[str, str | None]], key: KeyBundle) -> TabsReport:
    """批量解密并按设备分组。单条坏掉只跳过并记下来，不连坐。"""
    batch = decrypt_records(
        records, key, model=TabsRecord, expand=lambda record, _id: (_client(record),), what="标签页"
    )
    clients = sorted(batch.items, key=lambda client: client.client_name)
    return TabsReport(
        clients=tuple(clients),
        skipped=batch.skipped,
        tombstones=batch.tombstones,
        records=batch.records,
    )


def _client(record: TabsRecord) -> ClientTabs:
    return ClientTabs(
        client_id=record.id,
        client_name=record.client_name or record.id,
        tabs=tuple(
            TabEntry(
                client_id=record.id,
                client_name=record.client_name or record.id,
                title=tab.title,
                url=tab.url,
                last_used_at=tab.last_used_at,
                icon=tab.icon,
                window_id=tab.window_id,
            )
            for tab in record.tabs
        ),
    )
