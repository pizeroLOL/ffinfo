"""合并策略核心 —— 三条 sync 写路径共用的「入站 → 对已有行的判定」。

直接打 :func:`plan_sync_writes`：入站含旧 / 新 / 重复 id / 墓碑，断言判定集合。
三条路径的端到端行为回归在 ``test_store`` / ``test_sync`` / ``test_transfer``。
"""

from __future__ import annotations

from ffinfo.storage import EncryptedBso
from ffinfo_cli.merge_policy import plan_sync_writes
from ffinfo_cli.portable import PortableRecord


def bso(record_id: str, *, modified: float, payload: str | None = "encrypted") -> EncryptedBso:
    """造一条 sync 路径形态的入站记录。``payload=None`` 是墓碑。"""
    return EncryptedBso(id=record_id, modified=modified, payload=payload)


def portable(
    record_id: str,
    *,
    modified: float,
    payload: str | None = "encrypted",
    collection: str = "history",
) -> PortableRecord:
    """造一条 import 路径形态的入站记录。"""
    return PortableRecord(
        collection=collection,
        record_id=record_id,
        modified=modified,
        payload=payload,
        sortindex=None,
        ttl=None,
    )


def test_batch_folds_to_newest_per_key() -> None:
    """同键多次出现只认 ``modified`` 最大（平手留后面的）—— 一份算法。"""
    older = bso("a", modified=1.0, payload="old")
    newer = bso("a", modified=5.0, payload="new")
    tied_first = bso("b", modified=2.0, payload="b-first")
    tied_second = bso("b", modified=2.0, payload="b-second")

    plan = plan_sync_writes(
        [older, newer, tied_first, tied_second, bso("c", modified=3.0)],
        {},
        key_of=lambda record: record.id,
        disposition="delete",
    )

    assert [record.id for record in plan.live] == ["a", "b", "c"]
    payloads = {record.id: record.payload for record in plan.live}
    assert payloads["a"] == "new"
    assert payloads["b"] == "b-second"


def test_stale_never_wins_under_delete_disposition() -> None:
    """增量：已有行更新时，入站旧的既不覆盖也不删 —— 陈数据永不赢。"""
    plan = plan_sync_writes(
        [bso("a", modified=1.0, payload="old")],
        {"a": 5.0},
        key_of=lambda record: record.id,
        disposition="delete",
    )

    assert plan.inserts == []
    assert plan.updates == []
    assert plan.deletes == []
    assert [record.payload for record in plan.stale] == ["old"]


def test_newer_record_updates_and_absent_key_inserts() -> None:
    """增量：更新的覆盖、库里没有的插入 —— 判定按键分派。"""
    plan = plan_sync_writes(
        [bso("old", modified=1.0), bso("new", modified=1.0), bso("fresher", modified=9.0)],
        {"old": 5.0, "fresher": 5.0},
        key_of=lambda record: record.id,
        disposition="delete",
    )

    assert [record.id for record in plan.inserts] == ["new"]
    assert [record.id for record in plan.updates] == ["fresher"]
    assert [record.id for record in plan.live] == ["new", "fresher"]


def test_tombstone_delete_disposition_only_removes_existing_rows() -> None:
    """增量墓碑：命中已有行才删；没行当没发生（不进 inserts / kept）。"""
    plan = plan_sync_writes(
        [bso("gone", modified=2.0, payload=None), bso("never", modified=2.0, payload=None)],
        {"gone": 1.0},
        key_of=lambda record: record.id,
        disposition="delete",
    )

    assert plan.deletes == ["gone"]
    assert plan.inserts == []
    assert plan.updates == []
    assert plan.kept == []
    assert plan.live == []


def test_tombstone_keep_disposition_counts_all_tombstones_and_stale() -> None:
    """import：墓碑（不管有没有行）与陈数据都计 ``kept``，一行不动。"""
    plan = plan_sync_writes(
        [
            bso("tomb", modified=9.0, payload=None),
            bso("fresh-tomb", modified=9.0, payload=None),
            bso("stale", modified=1.0),
        ],
        {"tomb": 1.0, "stale": 5.0},
        key_of=lambda record: record.id,
        disposition="keep",
    )

    assert plan.deletes == []
    assert [record.id for record in plan.kept] == ["tomb", "fresh-tomb", "stale"]
    assert [record.id for record in plan.live] == []


def test_tombstone_filter_disposition_drops_tombstones_and_reconciles_absent_rows() -> None:
    """全量：墓碑筛出存活集；存活集之外的已有行进 ``deletes``（对账）。"""
    plan = plan_sync_writes(
        [bso("keep", modified=1.0), bso("tomb", modified=9.0, payload=None)],
        {"keep": 5.0, "tomb": 1.0, "absent": 1.0},
        key_of=lambda record: record.id,
        disposition="filter",
    )

    assert [record.id for record in plan.live] == ["keep"]
    assert sorted(plan.deletes) == ["absent", "tomb"]
    assert plan.kept == []
    assert plan.stale == []


def test_filter_disposition_writes_older_live_records() -> None:
    """全量以这一批为准 —— 入站 ``modified`` 旧也照写（不走 modified 比较）。"""
    plan = plan_sync_writes(
        [bso("a", modified=1.0, payload="from-batch")],
        {"a": 50.0},
        key_of=lambda record: record.id,
        disposition="filter",
    )

    assert [record.payload for record in plan.updates] == ["from-batch"]
    assert plan.stale == []
    assert plan.deletes == []


def test_portable_records_walk_the_same_algorithm() -> None:
    """import 形态：同键折叠 + 陈数据不赢，与 sync 形态一份实现。"""
    plan = plan_sync_writes(
        [
            portable("a", modified=1.0, payload="old"),
            portable("a", modified=7.0, payload="new"),
            portable("b", modified=1.0),
        ],
        {("history", "b"): 5.0},
        key_of=lambda item: (item.collection, item.record_id),
        disposition="keep",
    )

    assert [item.payload for item in plan.live if item.record_id == "a"] == ["new"]
    assert plan.stale[0].record_id == "b"
    assert [item.record_id for item in plan.kept] == ["b"]


def test_empty_incoming_yields_empty_plan_except_filter_reconciliation() -> None:
    """空批：增量 / import 什么都不判；全量把已有行全部对账删除。"""
    shared = {"a": 1.0}
    empty: list[EncryptedBso] = []
    incremental = plan_sync_writes(
        empty, shared, key_of=lambda record: record.id, disposition="delete"
    )
    assert incremental.deletes == []
    assert incremental.live == []

    reconciled = plan_sync_writes(
        empty, shared, key_of=lambda record: record.id, disposition="filter"
    )
    assert reconciled.deletes == ["a"]
    assert reconciled.live == []
