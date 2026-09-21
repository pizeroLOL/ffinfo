# pyright: reportCallIssue=false
# 上面这条：pyright 认不出 pydantic 的 populate_by_name（以为参数名是别名），运行时正常。
"""书签记录的解析与建树。

密文用本库自己的加密方向现造（加密方向已由官方向量逐字节验过）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ffinfo.bookmarks import (
    BookmarkNode,
    BookmarkRecord,
    BookmarkReport,
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
    assert node.added_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_missing_date_added_is_fine() -> None:
    report = parse_bookmarks([("rec1", encrypt(bookmark_json("rec1", date_added=None)))], KEY)

    assert report.roots[0].added_at is None


def test_deleted_records_are_tombstones_not_failures() -> None:
    """书签的墓碑是 ``{"deleted": true}``，不是 payload 为 null —— 别当成坏记录。"""
    cleartext = json.dumps({"id": "gone", "deleted": True})

    report = parse_bookmarks([("gone", encrypt(cleartext))], KEY)

    assert report.tombstones == 1
    assert report.skipped == ()
    assert report.roots == ()


def test_deleted_flag_is_a_tombstone_even_when_not_a_json_bool() -> None:
    """``deleted: 1``（pydantic 当 True）也是墓碑 —— 判别别收窄成只认 JSON ``true``。"""
    cleartext = json.dumps({"id": "gone", "type": "bookmark", "deleted": 1})

    report = parse_bookmarks([("gone", encrypt(cleartext))], KEY)

    assert report.tombstones == 1
    assert report.skipped == ()
    assert report.roots == ()


def test_bsos_without_payload_are_tombstones_too() -> None:
    report = parse_bookmarks([("gone", None)], KEY)

    assert report.tombstones == 1
    assert report.skipped == ()


def test_tree_keeps_the_hierarchy() -> None:
    """父子关系要在 —— 这是这一票的硬要求。"""
    records = [
        BookmarkRecord(id="root", type="folder", title="根"),
        BookmarkRecord(id="sub", type="folder", title="子", parent_id="root"),
        BookmarkRecord(
            id="leaf", type="bookmark", title="书签", parent_id="sub", url="https://x.test/"
        ),
    ]

    roots = build_tree(records).roots

    assert len(roots) == 1
    assert roots[0].id == "root"
    assert roots[0].children[0].id == "sub"
    assert roots[0].children[0].children[0].id == "leaf"


def test_orphans_become_roots_instead_of_disappearing() -> None:
    """父记录没同步过来时，子树不能凭空消失。"""
    records = [
        BookmarkRecord(id="lost", type="bookmark", title="孤儿", parent_id="missing"),
    ]

    roots = build_tree(records).roots

    assert [node.id for node in roots] == ["lost"]


def test_folders_sort_before_bookmarks() -> None:
    """输出顺序稳定 —— 不然 diff 起来全是噪音。"""
    records = [
        BookmarkRecord(id="b", type="bookmark", title="zzz"),
        BookmarkRecord(id="a", type="folder", title="aaa"),
    ]

    assert [node.id for node in build_tree(records).roots] == ["a", "b"]


def test_self_parent_becomes_a_root_instead_of_its_own_child() -> None:
    """自环（父亲是自己）：父亲不合法 → 当根。绝不能成为自己的孩子。"""
    records = [BookmarkRecord(id="a", type="folder", title="自环", parent_id="a")]

    result = build_tree(records)

    assert [node.id for node in result.roots] == ["a"]
    assert result.roots[0].children == []
    assert result.dropped == ()


def test_a_cycle_is_dropped_and_reported() -> None:
    """环（A→B→A）：环里谁也到不了根 —— 整环丢弃，但要**报出来**，不静默。"""
    records = [
        BookmarkRecord(id="a", type="folder", title="A", parent_id="b"),
        BookmarkRecord(id="b", type="folder", title="B", parent_id="a"),
    ]

    result = build_tree(records)

    assert result.roots == ()
    assert {record_id for record_id, _ in result.dropped} == {"a", "b"}
    assert all("成环" in reason for _, reason in result.dropped)


def test_nodes_outside_a_cycle_are_not_collateral_damage() -> None:
    """环外的节点不连坐：父在环里的，按"父记录没同步过来"当根。"""
    records = [
        BookmarkRecord(id="a", type="folder", title="A", parent_id="b"),
        BookmarkRecord(id="b", type="folder", title="B", parent_id="a"),
        BookmarkRecord(id="child", type="bookmark", title="环外的孩子", parent_id="a"),
    ]

    result = build_tree(records)

    assert [node.id for node in result.roots] == ["child"]
    assert {record_id for record_id, _ in result.dropped} == {"a", "b"}


def test_duplicate_ids_keep_the_last_and_report_the_rest() -> None:
    """重复 id：后者胜（dict 语义），先前的记一笔 —— 不静默覆盖。"""
    records = [
        BookmarkRecord(id="dup", type="bookmark", title="先来的", url="https://first.test/"),
        BookmarkRecord(id="dup", type="bookmark", title="后来的", url="https://last.test/"),
    ]

    result = build_tree(records)

    assert [node.title for node in result.roots] == ["后来的"]
    assert result.dropped == (("dup", "id 重复 —— 同一 id 出现多次，保留最后一条"),)


def test_cycles_reach_the_report_as_dropped() -> None:
    """病态记录要经 ``parse_bookmarks`` 走到报告层 —— 解密成功但没进树。"""
    report = parse_bookmarks(
        [
            ("a", encrypt(folder_json("a", parent_id="b", title="A"))),
            ("b", encrypt(folder_json("b", parent_id="a", title="B"))),
        ],
        KEY,
    )

    assert report.roots == ()
    assert report.skipped == ()
    assert {record_id for record_id, _ in report.dropped} == {"a", "b"}


def test_deep_trees_do_not_blow_the_stack() -> None:
    """病态深树：``counts()`` 走的是迭代版 ``_walk``，不炸栈。"""
    depth = 2_000
    records = [BookmarkRecord(id="n0", type="folder", title="根")]
    for index in range(1, depth):
        records.append(
            BookmarkRecord(
                id=f"n{index}", type="folder", title=f"n{index}", parent_id=f"n{index - 1}"
            )
        )

    result = build_tree(records)
    report = BookmarkReport(roots=result.roots, skipped=(), dropped=(), tombstones=0, records=depth)

    assert len(result.roots) == 1
    assert sum(report.counts().values()) == depth


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
