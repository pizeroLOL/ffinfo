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
from typing import Any

from ffinfo.bookmarks import BookmarkNode, parse_bookmarks
from ffinfo.crypto import KeyBundle
from ffinfo_cli.list.common import (
    ListReport,
    cloud_key,
    details,
    guard_all_failed,
    load_shell,
    truncate,
)


async def run_bookmarks(
    *,
    identity_path: Path,
    credentials_path: Path,
    database_path: Path,
    path: str | None = None,
    limit: int | None = None,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> ListReport:
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
) -> ListReport:
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

    return ListReport(
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
) -> ListReport:
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
