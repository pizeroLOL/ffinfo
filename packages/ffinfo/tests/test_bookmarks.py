"""书签记录的解析与建树。

密文用本库自己的加密方向现造（加密方向已由官方向量逐字节验过）。
"""

from __future__ import annotations

import json

from ffinfo.bookmarks import (
    BookmarkNode,
    BookmarkRecord,
    build_tree,
    parse_bookmarks,
)
from ffinfo.crypto import EncryptedPayload, KeyBundle

KEY = KeyBundle(encryption_key=b"e" * 32, hmac_key=b"h" * 32)
OTHER_KEY = KeyBundle(encryption_key=b"x" * 32, hmac_key=b"y" * 32)

# 2023-11-14T22:13:20+00:00 —— 整十亿秒，好记
EPOCH_MILLIS = 1_700_000_000_000


def encrypt(cleartext: str, *, key: KeyBundle = KEY) -> str:
    return EncryptedPayload.from_cleartext(key, cleartext).to_json()


def bookmark_json(
    record_id: str,
    *,
    parent_id: str | None = "folder1",
    title: str = "Example",
    url: str | None = "https://example.com/",
    kind: str = "bookmark",
    date_added: int | None = EPOCH_MILLIS,
    deleted: bool = False,
) -> str:
    payload: dict[str, object] = {"id": record_id, "type": kind, "title": title}
    if parent_id is not None:
        payload["parentid"] = parent_id
    if url is not None:
        payload["bmkUri"] = url
    if date_added is not None:
        payload["dateAdded"] = date_added
    if deleted:
        payload["deleted"] = True
    return json.dumps(payload)


def folder_json(record_id: str, *, parent_id: str | None = None, title: str = "F") -> str:
    return bookmark_json(record_id, parent_id=parent_id, title=title, url=None, kind="folder")


# ── 解析 ──────────────────────────────────────────────────────────────────


def test_parses_a_bookmark() -> None:
    report = parse_bookmarks([("rec1", encrypt(bookmark_json("rec1")))], KEY)

    node = report.roots[0]  # parentid 指向的文件夹没同步过来 → 孤儿根节点
    assert node.id == "rec1"
    assert node.type == "bookmark"
    assert node.title == "Example"
    assert node.url == "https://example.com/"


def test_date_added_is_milliseconds() -> None:
    """``dateAdded`` 是**毫秒** —— 当成秒算，时间会飘到 1970 年代。"""
    report = parse_bookmarks([("rec1", encrypt(bookmark_json("rec1")))], KEY)

    node = report.roots[0]
    assert node.added_at == "2023-11-14T22:13:20+00:00"


def test_missing_date_added_is_fine() -> None:
    report = parse_bookmarks([("rec1", encrypt(bookmark_json("rec1", date_added=None)))], KEY)

    assert report.roots[0].added_at is None


# ── 墓碑：这一种长得不一样 ────────────────────────────────────────────────


def test_deleted_records_are_tombstones_not_failures() -> None:
    """书签的墓碑是 ``{"deleted": true}``，不是 payload 为 null —— 别当成坏记录。"""
    cleartext = json.dumps({"id": "gone", "deleted": True})

    report = parse_bookmarks([("gone", encrypt(cleartext))], KEY)

    assert report.tombstones == 1
    assert report.skipped == ()
    assert report.roots == ()


def test_bsos_without_payload_are_tombstones_too() -> None:
    report = parse_bookmarks([("gone", None)], KEY)

    assert report.tombstones == 1
    assert report.skipped == ()


# ── 建树 ──────────────────────────────────────────────────────────────────


def test_tree_keeps_the_hierarchy() -> None:
    """父子关系要在 —— 这是这一票的硬要求。"""
    records = [
        BookmarkRecord(id="root", type="folder", title="根"),
        BookmarkRecord(id="sub", type="folder", title="子", parent_id="root"),
        BookmarkRecord(
            id="leaf", type="bookmark", title="书签", parent_id="sub", url="https://x.test/"
        ),
    ]

    roots = build_tree(records)

    assert len(roots) == 1
    assert roots[0].id == "root"
    assert roots[0].children[0].id == "sub"
    assert roots[0].children[0].children[0].id == "leaf"


def test_orphans_become_roots_instead_of_disappearing() -> None:
    """父记录没同步过来时，子树不能凭空消失。"""
    records = [
        BookmarkRecord(id="lost", type="bookmark", title="孤儿", parent_id="missing"),
    ]

    roots = build_tree(records)

    assert [node.id for node in roots] == ["lost"]


def test_folders_sort_before_bookmarks() -> None:
    """输出顺序稳定 —— 不然 diff 起来全是噪音。"""
    records = [
        BookmarkRecord(id="b", type="bookmark", title="zzz"),
        BookmarkRecord(id="a", type="folder", title="aaa"),
    ]

    assert [node.id for node in build_tree(records)] == ["a", "b"]


def test_nested_parse_builds_a_real_tree() -> None:
    """端到端：三条加密记录 → 一棵树。"""
    report = parse_bookmarks(
        [
            ("root", encrypt(folder_json("root", title="书签菜单"))),
            ("sub", encrypt(folder_json("sub", parent_id="root", title="工具"))),
            ("leaf", encrypt(bookmark_json("leaf", parent_id="sub"))),
        ],
        KEY,
    )

    root = report.roots[0]
    assert root.title == "书签菜单"
    assert root.children[0].title == "工具"
    assert root.children[0].children[0].url == "https://example.com/"
    assert report.counts() == {"folder": 2, "bookmark": 1}


def test_counts_walks_the_whole_tree() -> None:
    report = parse_bookmarks(
        [
            ("root", encrypt(folder_json("root"))),
            ("a", encrypt(bookmark_json("a", parent_id="root"))),
            ("b", encrypt(bookmark_json("b", parent_id="root"))),
        ],
        KEY,
    )

    assert report.counts() == {"folder": 1, "bookmark": 2}


# ── 单条坏掉不连坐 ────────────────────────────────────────────────────────


def test_one_bad_record_does_not_kill_the_batch() -> None:
    broken = json.dumps({"IV": "AAAA", "hmac": "00" * 32, "ciphertext": "AAAA"})

    report = parse_bookmarks(
        [
            ("good", encrypt(bookmark_json("good"))),
            ("broken", broken),
        ],
        KEY,
    )

    assert len(report.roots) == 1
    assert [record_id for record_id, _ in report.skipped] == ["broken"]


def test_wrong_key_skips_everything() -> None:
    report = parse_bookmarks([("rec1", encrypt(bookmark_json("rec1"), key=OTHER_KEY))], KEY)

    assert report.roots == ()
    assert len(report.skipped) == 1


def test_malformed_cleartext_is_skipped() -> None:
    report = parse_bookmarks([("rec1", encrypt("not json"))], KEY)

    assert report.skipped == (("rec1", "明文不是合法的书签记录"),)


def test_records_seen_counts_everything() -> None:
    report = parse_bookmarks(
        [("gone", None), ("broken", "{}"), ("rec1", encrypt(bookmark_json("rec1")))], KEY
    )

    assert report.records == 3


def test_extra_fields_are_ignored() -> None:
    """真实记录里有 ``hasDupe`` / ``parentName`` 这些字段，别被它们绊倒。"""
    cleartext = json.dumps(
        {
            "id": "rec1",
            "type": "bookmark",
            "parentid": "p",
            "parentName": "learn",
            "hasDupe": True,
            "bmkUri": "https://x.test/",
            "title": "t",
        }
    )

    report = parse_bookmarks([("rec1", encrypt(cleartext))], KEY)

    node = report.roots[0]
    assert node.parent_name == "learn"
    assert node.url == "https://x.test/"


def test_node_model_is_constructible_directly() -> None:
    node = BookmarkNode(id="a", type="folder", title="t")
    assert node.children == []
