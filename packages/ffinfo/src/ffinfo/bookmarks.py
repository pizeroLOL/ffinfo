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
from datetime import datetime
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from ffinfo._decrypt import decrypt_records, single
from ffinfo.crypto import KeyBundle
from ffinfo.timestamps import from_milliseconds

__all__ = [
    "BookmarkNode",
    "BookmarkRecord",
    "BookmarkReport",
    "TreeResult",
    "build_tree",
    "parse_bookmarks",
]


_DUPLICATE_REASON: Final = "id 重复 —— 同一 id 出现多次，保留最后一条"
_CYCLE_REASON: Final = "父链成环 —— 环里的节点到不了任何根，整环丢弃"


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
        return from_milliseconds(self.date_added)


class BookmarkNode(BaseModel):
    """树上的一个节点 —— 文件夹有 ``children``，书签没有。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: str
    type: str
    title: str
    url: str | None = None
    added_at: datetime | None = None
    """添加时间（UTC ``datetime``）—— **与 history / tabs 同一个口径**，JSON 里仍是 ISO。"""
    parent_id: str | None = None
    parent_name: str | None = None
    children: list[BookmarkNode] = []


@dataclass(frozen=True, slots=True)
class TreeResult:
    """建树的结果：树本身 + 建树时丢掉的病态记录。

    ``dropped`` 与解密失败的 ``skipped`` 汇进同一个报告口径 —— 都是
    "这条没能出现在树里，原因在此"。
    """

    roots: tuple[BookmarkNode, ...]
    dropped: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class BookmarkReport:
    """批量解密 + 建树的结果。"""

    roots: tuple[BookmarkNode, ...]
    skipped: tuple[tuple[str, str], ...]
    """**解密失败**的记录：(id, 原因)。"""
    dropped: tuple[tuple[str, str], ...]
    """**解密成功但没进树**的病态记录：(id, 原因) —— 重复 id / 父链成环。"""
    tombstones: int
    """``{"deleted": true}`` 的记录条数 —— 不是失败，是"这条被删了"。"""

    records: int
    """看过的记录条数。"""

    def counts(self) -> dict[str, int]:
        """每种类型各有多少个节点（不含被删的、也不含建树时丢掉的）。"""
        tally: dict[str, int] = {}
        for node in _walk(self.roots):
            tally[node.type] = tally.get(node.type, 0) + 1
        return tally


def parse_bookmarks(records: Iterable[tuple[str, str | None]], key: KeyBundle) -> BookmarkReport:
    """批量解密并建树。单条坏掉只跳过并记下来，不连坐。"""
    batch = decrypt_records(records, key, model=BookmarkRecord, expand=single, what="书签")
    tree = build_tree(batch.items)
    return BookmarkReport(
        roots=tree.roots,
        skipped=batch.skipped,
        dropped=tree.dropped,
        tombstones=batch.tombstones,
        records=batch.records,
    )


def build_tree(records: Iterable[BookmarkRecord]) -> TreeResult:
    """按 ``parentid`` 把一堆平铺的记录拼成树。

    找不到父亲的（根目录本身、父记录没同步过来、**或者父亲是自己**）当根节点 ——
    不能因为一个缺失的父亲就把整棵子树丢掉。

    两种病态**不静默**，都进 :attr:`TreeResult.dropped`：

    * **重复 id** —— 同一 id 出现多次时保留最后一条，先前的记一笔
    * **环**（A→B→A）—— 环里每个节点都"有父亲"，却谁也到不了根。整个环
      没有天然的根可认，**整环丢弃并记一笔**（不硬造结构）

    环外的节点不受连坐：父在环里的，按"父记录没同步过来"处理，当根。
    """
    materialized = list(records)

    nodes: dict[str, BookmarkNode] = {}
    dropped: list[tuple[str, str]] = []
    for record in materialized:
        if record.id in nodes:
            dropped.append((record.id, _DUPLICATE_REASON))
        nodes[record.id] = _node(record)

    parent_of: dict[str, str] = {}
    for node in nodes.values():
        parent = nodes.get(node.parent_id) if node.parent_id else None
        if parent is not None and parent.id != node.id:
            parent_of[node.id] = parent.id

    # 环检测：沿父链上溯，能走到"没有父亲"的就是好节点；本次上溯里撞见的
    # 节点就是环 —— 环里到不了根，整环丢弃。
    cyclic: set[str] = set()
    settled: set[str] = set()
    for start in nodes:
        if start in settled:
            continue
        path: list[str] = []
        seen_at: dict[str, int] = {}
        current: str | None = start
        while current is not None and current not in settled and current not in seen_at:
            seen_at[current] = len(path)
            path.append(current)
            current = parent_of.get(current)
        if current is not None and current in seen_at:
            cyclic.update(path[seen_at[current] :])
        settled.update(path)

    roots: list[BookmarkNode] = []
    for node in nodes.values():
        if node.id in cyclic:
            dropped.append((node.id, _CYCLE_REASON))
            continue
        parent = nodes.get(node.parent_id) if node.parent_id else None
        if parent is None or parent.id == node.id or parent.id in cyclic:
            roots.append(node)
        else:
            parent.children.append(node)
    return TreeResult(roots=tuple(sorted(roots, key=_order)), dropped=tuple(dropped))


def _node(record: BookmarkRecord) -> BookmarkNode:
    return BookmarkNode(
        id=record.id,
        type=record.type,
        title=record.title,
        url=record.url,
        added_at=record.added_at,
        parent_id=record.parent_id,
        parent_name=record.parent_name,
    )


def _order(node: BookmarkNode) -> tuple[int, str]:
    """文件夹排在书签前面，同类按标题 —— 输出稳定，diff 起来才看得出变化。"""
    return (0 if node.type == "folder" else 1, node.title)


def _walk(nodes: Iterable[BookmarkNode]) -> Iterable[BookmarkNode]:
    """迭代版深度优先 —— 病态深树也不会炸栈。"""
    stack = list(reversed(list(nodes)))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))
