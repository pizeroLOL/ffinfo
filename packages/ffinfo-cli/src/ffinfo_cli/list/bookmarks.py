"""``ffinfo-cli list bookmarks`` —— 建树 / 剪枝，保留父子层级。

书签的父子层级就是主要信息，拍平就没了。所以输出是一棵**树**，
``--path`` 按 ``/`` 分隔的文件夹标题命中后从命中文件夹**重新生根**（祖先不带），
``--limit`` 数的是书签条数（文件夹是挂书签用的结构，不占名额）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from ffinfo.bookmarks import BookmarkNode, parse_bookmarks
from ffinfo.crypto import KeyBundle
from ffinfo_cli.list.common import (
    SourceName,
    cloud_key,
    details,
    guard_all_failed,
    load_shell,
    truncate,
)


class BookmarksReport(BaseModel):
    """``list bookmarks`` 的输出 —— 保留父子层级的树。

    与 history / tabs 的报告**没有共同基类**：共享的是形状，用 ``report.ListReport``
    这个 union 表达。序列化在 ``ffinfo_cli/render.py``。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    format_version: int = 5
    """**5**：报告拆成三份互不继承的类型（形状不变，仍是 v4 的扁平字段）。"""
    data_type: str
    generated_at: str
    filters: dict[str, str | int | None]
    records: int
    """库里读出来的记录条数。"""
    synced_at: str | None = None
    """这个 collection **上次成功 sync** 的时间（UTC ISO）。从未同步过就是 ``null``。"""
    age_seconds: float | None = None
    """``synced_at`` 距现在多少秒：**数据有多陈**。"""
    firefox_records: int = 0
    """v4 外壳留下的字段 —— 书签没有 firefox 源，恒为 0；保留是为了形状逐字段不变。"""
    sources: list[SourceName] = []
    """v4 外壳留下的字段 —— 书签没有多源合并，恒为空；保留是为了形状逐字段不变。"""
    skipped: int
    """没能进结果的记录条数（解密失败、或建树时丢弃的病态记录）—— 单条坏掉不连坐。"""
    skipped_details: list[dict[str, str]] = []
    notes: list[str] = []
    """结果为空但**不是失败**时的提示 —— 比如 ``--path`` 什么都没命中。退出码照旧 0。"""
    matched: int
    """过滤之后剩多少条 —— 对 bookmarks 是书签条数（文件夹是结构，不计）。"""
    returned: int
    """实际返回多少条（``--limit`` 之后，口径与 ``matched`` 相同）。"""
    tree: list[BookmarkNode] = []
    """**保留层级**，不是拍平的表。"""
    counts: dict[str, int] = {}
    """**返回的这棵树**里各类节点各有多少（含作为结构的文件夹）。"""


async def run_bookmarks(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    path: str | None = None,
    limit: int | None = None,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> BookmarksReport:
    """读库 → 解密 → 建树 → 按路径选根 → 出报告。全程不联网。"""
    shell = await load_shell(
        database_path=database_path,
        collection="bookmarks",
        filters={"path": path, "limit": limit},
        warn=warn,
        clock=clock,
    )
    key = await cloud_key(
        store=shell.store,
        identity_path=identity_path,
        credentials_path=credentials_path,
        collection="bookmarks",
    )
    return _bookmark_report(shell.records, key, shell.common, path=path, limit=limit)


def _bookmark_report(
    records: Sequence[tuple[str, str | None]],
    key: KeyBundle,
    common: dict[str, Any],
    *,
    path: str | None,
    limit: int | None,
) -> BookmarksReport:
    """书签：**保留树**。``--path`` 命中后从命中文件夹重新生根，祖先剪掉。

    ``--limit`` 数的是**书签条数** —— 文件夹是挂书签用的结构，不占名额
    （与 tabs 那边"设备不占名额"一个道理）。最终树、``returned`` 与
    ``counts`` 都从同一棵树上数出来，互相自洽。
    """
    decrypted = parse_bookmarks(records, key)
    guard_all_failed(decrypted.records, len(decrypted.roots), len(decrypted.skipped))

    notes: list[str] = []
    if path is not None:
        roots = _select_by_path(decrypted.roots, path)
        if not roots:
            notes.append(f"没有匹配路径「{path}」的文件夹")
    else:
        roots = list(decrypted.roots)

    bookmarks = [node for node in _flatten(roots) if node.type != "folder"]
    returned = truncate(bookmarks, limit)
    kept_ids = {node.id for node in returned}
    tree = _prune(roots, lambda node: node.id in kept_ids)
    not_in_tree = decrypted.skipped + decrypted.dropped

    return BookmarksReport(
        **common,
        skipped=len(not_in_tree),
        skipped_details=details(not_in_tree),
        notes=notes,
        matched=len(bookmarks),
        returned=len(returned),
        tree=tree,
        counts=_counts(tree),
    )


def _select_by_path(nodes: Sequence[BookmarkNode], path: str) -> list[BookmarkNode]:
    """按 ``/`` 分隔的**文件夹标题**路径找节点 —— 精确匹配、区分大小写、同名全收。

    路径从任意 root 起算（root 标题也是第一段 —— Firefox 的 root 名是本地化的，
    不能写死「Bookmarks Toolbar」），首尾斜杠忽略。只有文件夹能作为路径段；
    命中后整棵子树归它，不再往下找同路径的孙文件夹。

    **迭代版**（显式栈）—— 与 ``bookmarks.build_tree`` / ``_prune`` 同一条防线。
    """
    target = tuple(segment for segment in path.split("/") if segment)
    if not target:
        return []
    found: list[BookmarkNode] = []
    stack: list[tuple[BookmarkNode, tuple[str, ...]]] = [
        (node, (node.title,)) for node in reversed(list(nodes))
    ]
    while stack:
        node, trail = stack.pop()
        if len(trail) > len(target) or trail != target[: len(trail)]:
            continue
        if trail == target:
            if node.type == "folder":
                found.append(node)
            continue
        if node.type != "folder":
            continue
        stack.extend((child, (*trail, child.title)) for child in reversed(node.children))
    return found


def _prune(
    nodes: Sequence[BookmarkNode], keep: Callable[[BookmarkNode], bool]
) -> list[BookmarkNode]:
    """剪枝：文件夹只要有**任意一个后代**留下就保留。

    **迭代版**（显式栈走后序）—— 病态深树不炸栈，与 ``bookmarks._walk`` 是同一条防线。
    """
    pruned: dict[int, list[BookmarkNode]] = {}
    stack: list[tuple[BookmarkNode, bool]] = [(node, False) for node in reversed(list(nodes))]
    while stack:
        node, expanded = stack.pop()
        if not expanded:
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.children))
            continue
        children: list[BookmarkNode] = []
        for child in node.children:
            children.extend(pruned.pop(id(child), ()))
        if children or (keep(node) and node.type != "folder"):
            pruned[id(node)] = [node.model_copy(update={"children": children})]
        else:
            pruned[id(node)] = []

    kept: list[BookmarkNode] = []
    for node in nodes:
        kept.extend(pruned.pop(id(node), ()))
    return kept


def _flatten(nodes: Sequence[BookmarkNode]) -> list[BookmarkNode]:
    """深度优先拍平 —— 只用来数数和截断，输出仍然是树。**迭代版**，不炸栈。"""
    out: list[BookmarkNode] = []
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(node.children))
    return out


def _counts(nodes: Sequence[BookmarkNode]) -> dict[str, int]:
    """树上各类**节点**各有多少（含文件夹 —— 它们也是输出的一部分）。"""
    tally: dict[str, int] = {}
    for node in _flatten(nodes):
        tally[node.type] = tally.get(node.type, 0) + 1
    return tally


def list_bookmarks_blocking(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    path: str | None = None,
    limit: int | None = None,
    warn: Callable[[str], None] | None = None,
) -> BookmarksReport:
    """:func:`run_bookmarks` 的同步外壳。纯本地，所以没有 HTTP 客户端要开。"""
    return asyncio.run(
        run_bookmarks(
            identity_path=identity_path,
            credentials_path=credentials_path,
            database_path=database_path,
            path=path,
            warn=warn,
            limit=limit,
        )
    )
