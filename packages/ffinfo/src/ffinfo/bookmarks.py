"""书签记录的解析与解密。

真实结构（2026-09-14 实测）::

    {
        "id": "aB3dEf6hIj9k",
        "type": "bookmark",
        "parentid": "Ab1Cd2Ef3Gh4",
        "parentName": "learn",
        "dateAdded": 1770311268141,
        "bmkUri": "https://…",
        "title": "…",
    }

三个坑：

1. **`dateAdded` 是毫秒** —— 历史那边是**微秒**，标签页那边是**秒**。
   三个 collection 三个单位，混了时间就飘。
2. **墓碑长成 ``{"deleted": true}``**，不是 BSO 那种 payload 为 ``null`` 的墓碑。
   所以"payload 存在"不等于"这条还活着"，还得看 ``deleted``。
3. **父子关系挂在 ``parentid`` 上**，我们按它建树 —— 不依赖 ``children`` 字段
   （那个字段是可选的，而 ``parentid`` 每条都有）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import DecryptionError

__all__ = [
    "BOOKMARK_TYPES",
    "BookmarkNode",
    "BookmarkRecord",
    "BookmarkReport",
    "build_tree",
    "parse_bookmarks",
]

BOOKMARK_TYPES: Final[dict[str, str]] = {
    "bookmark": "书签",
    "folder": "文件夹",
    "livemark": "实时书签",
    "query": "智能书签",
    "separator": "分隔符",
}
"""``type`` 的取值 —— 表里没有的原样返回，不报错。"""

_MILLISECONDS: Final = 1_000


class BookmarkRecord(BaseModel):
    """一条书签记录的明文。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="ignore", populate_by_name=True
    )

    id: str
    type: str = "bookmark"
    title: str = ""
    parent_id: str | None = Field(default=None, alias="parentid")
    parent_name: str | None = Field(default=None, alias="parentName")
    url: str | None = Field(default=None, alias="bmkUri")
    date_added: int | None = Field(default=None, alias="dateAdded")
    deleted: bool = False

    @property
    def added_at(self) -> datetime | None:
        """添加时间（UTC）。``dateAdded`` 是**毫秒**。"""
        if self.date_added is None:
            return None
        return datetime.fromtimestamp(self.date_added / _MILLISECONDS, tz=UTC)


class BookmarkNode(BaseModel):
    """树上的一个节点 —— 文件夹有 ``children``，书签没有。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: str
    type: str
    title: str
    url: str | None = None
    added_at: str | None = None
    parent_id: str | None = None
    parent_name: str | None = None
    children: list[BookmarkNode] = []


@dataclass(frozen=True, slots=True)
class BookmarkReport:
    """批量解密 + 建树的结果。"""

    roots: tuple[BookmarkNode, ...]
    skipped: tuple[tuple[str, str], ...]
    tombstones: int
    """``{"deleted": true}`` 的记录条数 —— 不是失败，是"这条被删了"。"""

    records: int
    """看过的记录条数。"""

    def counts(self) -> dict[str, int]:
        """每种类型各有多少个节点（不含被删的）。"""
        tally: dict[str, int] = {}
        for node in _walk(self.roots):
            tally[node.type] = tally.get(node.type, 0) + 1
        return tally


def parse_bookmarks(records: Iterable[tuple[str, str | None]], key: KeyBundle) -> BookmarkReport:
    """批量解密并建树。单条坏掉只跳过并记下来，不连坐。"""
    parsed: list[BookmarkRecord] = []
    skipped: list[tuple[str, str]] = []
    tombstones = 0
    seen = 0

    for record_id, payload in records:
        seen += 1
        if payload is None:
            tombstones += 1
            continue
        try:
            cleartext = EncryptedPayload.from_json(payload).decrypt(key)
            record = BookmarkRecord.model_validate_json(cleartext)
        except (DecryptionError, ValidationError) as exc:
            skipped.append((record_id, _reason(exc)))
            continue
        if record.deleted:
            tombstones += 1
            continue
        parsed.append(record)

    return BookmarkReport(
        roots=tuple(build_tree(parsed)),
        skipped=tuple(skipped),
        tombstones=tombstones,
        records=seen,
    )


def build_tree(records: Iterable[BookmarkRecord]) -> list[BookmarkNode]:
    """按 ``parentid`` 把一堆平铺的记录拼成树。

    找不到父亲的（根目录本身、或者父记录没同步过来）当根节点 —— 不能因为
    一个缺失的父亲就把整棵子树丢掉。
    """
    nodes = {record.id: _node(record) for record in records}
    roots: list[BookmarkNode] = []
    for record in records:
        node = nodes[record.id]
        parent = nodes.get(record.parent_id) if record.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
    return sorted(roots, key=_order)


def _node(record: BookmarkRecord) -> BookmarkNode:
    return BookmarkNode(
        id=record.id,
        type=record.type,
        title=record.title,
        url=record.url,
        added_at=record.added_at.isoformat() if record.added_at is not None else None,
        parent_id=record.parent_id,
        parent_name=record.parent_name,
    )


def _order(node: BookmarkNode) -> tuple[int, str]:
    """文件夹排在书签前面，同类按标题 —— 输出稳定，diff 起来才看得出变化。"""
    return (0 if node.type == "folder" else 1, node.title)


def _walk(nodes: Iterable[BookmarkNode]) -> Iterable[BookmarkNode]:
    for node in nodes:
        yield node
        yield from _walk(node.children)


def _reason(exc: Exception) -> str:
    """给跳过的那条记一个短原因。"""
    if isinstance(exc, DecryptionError):
        return str(exc)
    return "明文不是合法的书签记录"
