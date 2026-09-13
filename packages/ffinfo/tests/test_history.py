"""历史记录的解析与解密。

密文是**用本库自己的加密方向现造的** —— 加密方向已经对着官方向量
逐字节验过了，所以拿它造测试数据是可信的。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.history import (
    HistoryRecord,
    decrypt_history,
    visit_type_name,
)

KEY = KeyBundle(encryption_key=b"e" * 32, hmac_key=b"h" * 32)
OTHER_KEY = KeyBundle(encryption_key=b"x" * 32, hmac_key=b"y" * 32)

# 2023-11-14T22:13:20+00:00 —— 整十亿秒，好记
EPOCH_MICROS = 1_700_000_000_000_000


def encrypt(cleartext: str, *, key: KeyBundle = KEY) -> str:
    """造一条 payload。"""
    return EncryptedPayload.from_cleartext(key, cleartext).to_json()


OMIT = object()
"""``title=OMIT`` 表示**压根不写这个字段**，与 ``title=None``（写成 ``null``）区分开。"""


def record_json(
    *,
    record_id: str = "rec1",
    url: str = "https://example.com/",
    title: object = "Example",
    visits: list[dict[str, int]] | None = None,
) -> str:
    """造一条历史记录的明文 JSON。"""
    payload: dict[str, object] = {
        "id": record_id,
        "histUri": url,
        "visits": visits if visits is not None else [{"date": EPOCH_MICROS, "type": 1}],
    }
    if title is not OMIT:
        payload["title"] = title
    return json.dumps(payload)


# ── 解析 ──────────────────────────────────────────────────────────────────


def test_parses_a_record_into_one_entry_per_visit() -> None:
    """一次访问一行 —— 这才叫"浏览历史"。"""
    cleartext = record_json(
        visits=[
            {"date": EPOCH_MICROS, "type": 1},
            {"date": EPOCH_MICROS + 60_000_000, "type": 2},
        ]
    )

    report = decrypt_history([("rec1", encrypt(cleartext))], KEY)

    assert len(report.entries) == 2
    assert [entry.visit_type for entry in report.entries] == [1, 2]
    assert report.entries[0].url == "https://example.com/"
    assert report.entries[0].title == "Example"
    assert report.entries[0].record_id == "rec1"


def test_microseconds_become_utc_datetime() -> None:
    """``date`` 是**微秒** —— 差三个数量级，这里错了时间会飘到 1970 年。"""
    report = decrypt_history([("rec1", encrypt(record_json()))], KEY)

    assert report.entries[0].visited_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert report.entries[0].visited_at.isoformat() == "2023-11-14T22:13:20+00:00"


def test_null_title_becomes_empty_string() -> None:
    """上游明确处理过 ``title: null``（那边还有专门的测试），我们照做。"""
    report = decrypt_history([("rec1", encrypt(record_json(title=None)))], KEY)

    assert report.entries[0].title == ""


def test_missing_title_becomes_empty_string() -> None:
    """字段干脆不在 —— 上游说这种情况也见过。"""
    report = decrypt_history([("rec1", encrypt(record_json(title=OMIT)))], KEY)

    assert report.entries[0].title == ""


def test_record_without_visits_contributes_nothing() -> None:
    """没有访问记录的历史记录 —— 合法，但拍不出任何东西。"""
    report = decrypt_history([("rec1", encrypt(record_json(visits=[])))], KEY)

    assert report.entries == ()
    assert report.skipped == ()


def test_extra_fields_are_ignored() -> None:
    """服务器多给字段不该把我们打挂。"""
    cleartext = json.dumps(
        {
            "id": "rec1",
            "histUri": "https://example.com/",
            "title": "t",
            "visits": [{"date": EPOCH_MICROS, "type": 1, "future": True}],
            "future": 42,
        }
    )

    report = decrypt_history([("rec1", encrypt(cleartext))], KEY)

    assert len(report.entries) == 1


def test_visit_type_names() -> None:
    """数字对名字 —— 表按 ``places/src/types.rs`` 抄的。"""
    assert visit_type_name(1) == "link"
    assert visit_type_name(2) == "typed"
    assert visit_type_name(3) == "bookmark"
    assert visit_type_name(7) == "download"
    assert visit_type_name(9) == "reload"
    assert visit_type_name(10) == "update_place"


def test_unknown_visit_type_does_not_blow_up() -> None:
    """Mozilla 将来加新类型不该把我们打挂。"""
    assert visit_type_name(99) == "unknown"

    report = decrypt_history(
        [("rec1", encrypt(record_json(visits=[{"date": EPOCH_MICROS, "type": 99}])))], KEY
    )

    assert report.entries[0].visit_type_name == "unknown"


def test_history_record_model_directly() -> None:
    """模型本身也认 camelCase 的字段名。"""
    record = HistoryRecord.model_validate(
        {"id": "a", "histUri": "https://x.test/", "visits": [{"date": 1, "type": 1}]}
    )

    assert record.hist_uri == "https://x.test/"
    assert record.title == ""
    assert record.visits[0].date == 1


# ── 批量解密：单条坏掉不连坐 ──────────────────────────────────────────────


def test_one_bad_record_does_not_kill_the_batch() -> None:
    """硬要求：一条被篡改的记录，不该让你看不到另外两条。"""
    good = encrypt(record_json(record_id="good"))
    broken = json.dumps({"IV": "AAAA", "hmac": "00" * 32, "ciphertext": "AAAA"})

    report = decrypt_history(
        [("good", good), ("broken", broken), ("good2", encrypt(record_json(record_id="good2")))],
        KEY,
    )

    assert len(report.entries) == 2
    assert [record_id for record_id, _ in report.skipped] == ["broken"]
    assert "HMAC" in report.skipped[0][1]


def test_wrong_key_skips_everything_but_does_not_raise() -> None:
    """密钥不对时全部跳过 —— 由调用方决定这是不是"换账号了"。"""
    report = decrypt_history([("rec1", encrypt(record_json(), key=OTHER_KEY))], KEY)

    assert report.entries == ()
    assert len(report.skipped) == 1


def test_malformed_cleartext_is_skipped() -> None:
    """HMAC 过了但明文不是历史记录 —— 也算这一条坏掉。"""
    report = decrypt_history([("rec1", encrypt("not json at all"))], KEY)

    assert report.entries == ()
    assert report.skipped == (("rec1", "明文不是合法的历史记录"),)


def test_tombstones_are_counted_not_skipped() -> None:
    """墓碑是"这条被删了"，不是"这条坏了" —— 两回事。"""
    report = decrypt_history([("gone", None), ("rec1", encrypt(record_json()))], KEY)

    assert report.tombstones == 1
    assert report.skipped == ()
    assert len(report.entries) == 1


def test_total_counts_everything_seen() -> None:
    report = decrypt_history(
        [("gone", None), ("broken", "{}"), ("rec1", encrypt(record_json()))], KEY
    )

    assert report.records == 3


def test_empty_input_is_fine() -> None:
    report = decrypt_history([], KEY)

    assert report.entries == ()
    assert report.records == 0
