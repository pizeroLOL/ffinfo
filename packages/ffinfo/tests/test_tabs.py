"""标签页记录的解析与分组。"""

from __future__ import annotations

import json

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.tabs import parse_tabs

KEY = KeyBundle(encryption_key=b"e" * 32, hmac_key=b"h" * 32)
OTHER_KEY = KeyBundle(encryption_key=b"x" * 32, hmac_key=b"y" * 32)

EPOCH_SECONDS = 1_700_000_000


def encrypt(cleartext: str, *, key: KeyBundle = KEY) -> str:
    return EncryptedPayload.from_cleartext(key, cleartext).to_json()


def client_json(
    client_id: str = "client1",
    *,
    name: str | None = "user 的 Firefox",
    tabs: list[dict[str, object]] | None = None,
) -> str:
    payload: dict[str, object] = {
        "id": client_id,
        "tabs": tabs if tabs is not None else [{"title": "T", "urlHistory": ["https://x.test/"]}],
    }
    if name is not None:
        payload["clientName"] = name
    return json.dumps(payload)


# ── 解析 ──────────────────────────────────────────────────────────────────


def test_one_bso_is_one_client() -> None:
    """一个 BSO = 一台设备 —— 别把标签页拍成一长条。"""
    report = parse_tabs([("client1", encrypt(client_json()))], KEY)

    assert len(report.clients) == 1
    assert report.clients[0].client_name == "user 的 Firefox"
    assert report.clients[0].count == 1


def test_last_used_is_seconds() -> None:
    """``lastUsed`` 是**秒** —— 按微秒算会飘到五万年后。"""
    payload = client_json(
        tabs=[{"title": "T", "urlHistory": ["https://x.test/"], "lastUsed": EPOCH_SECONDS}]
    )

    report = parse_tabs([("client1", encrypt(payload))], KEY)

    assert report.entries[0].last_used_at == "2023-11-14T22:13:20+00:00"


def test_url_comes_from_the_first_url_history_entry() -> None:
    """``urlHistory`` 是一个数组（回退过的历史），取第一条。"""
    payload = client_json(
        tabs=[
            {
                "title": "T",
                "urlHistory": ["https://current.test/", "https://earlier.test/"],
            }
        ]
    )

    report = parse_tabs([("client1", encrypt(payload))], KEY)

    assert report.entries[0].url == "https://current.test/"


def test_empty_url_history_is_fine() -> None:
    payload = client_json(tabs=[{"title": "新标签页", "urlHistory": []}])

    report = parse_tabs([("client1", encrypt(payload))], KEY)

    assert report.entries[0].url is None


def test_null_icon_and_missing_window_id_are_fine() -> None:
    """真实数据里 ``icon`` 可以是 null，``windowId`` 可以整个不存在。"""
    payload = client_json(tabs=[{"title": "T", "urlHistory": ["https://x.test/"], "icon": None}])

    report = parse_tabs([("client1", encrypt(payload))], KEY)

    entry = report.entries[0]
    assert entry.icon is None
    assert entry.window_id is None


def test_window_id_is_kept_when_present() -> None:
    payload = client_json(
        tabs=[
            {
                "title": "T",
                "urlHistory": ["https://x.test/"],
                "windowId": "window-0",
            }
        ]
    )

    report = parse_tabs([("client1", encrypt(payload))], KEY)

    assert report.entries[0].window_id == "window-0"


def test_missing_client_name_falls_back_to_the_id() -> None:
    report = parse_tabs([("client1", encrypt(client_json(name=None)))], KEY)

    assert report.clients[0].client_name == "client1"


def test_clients_are_sorted_by_name() -> None:
    """输出顺序稳定 —— 不然 diff 全是噪音。"""
    report = parse_tabs(
        [
            ("b", encrypt(client_json("b", name="BBB"))),
            ("a", encrypt(client_json("a", name="AAA"))),
        ],
        KEY,
    )

    assert [client.client_name for client in report.clients] == ["AAA", "BBB"]


def test_entries_carry_the_client() -> None:
    """拍平之后仍然知道这条属于哪台设备。"""
    report = parse_tabs([("client1", encrypt(client_json()))], KEY)

    entry = report.entries[0]
    assert entry.client_id == "client1"
    assert entry.client_name == "user 的 Firefox"


def test_many_tabs_on_one_client() -> None:
    payload = client_json(
        tabs=[{"title": f"T{i}", "urlHistory": [f"https://x{i}.test/"]} for i in range(5)]
    )

    report = parse_tabs([("client1", encrypt(payload))], KEY)

    assert report.clients[0].count == 5
    assert len(report.entries) == 5


# ── 单条坏掉不连坐 ────────────────────────────────────────────────────────


def test_one_bad_record_does_not_kill_the_batch() -> None:
    broken = json.dumps({"IV": "AAAA", "hmac": "00" * 32, "ciphertext": "AAAA"})

    report = parse_tabs([("good", encrypt(client_json("good"))), ("broken", broken)], KEY)

    assert len(report.clients) == 1
    assert [record_id for record_id, _ in report.skipped] == ["broken"]


def test_wrong_key_skips_everything() -> None:
    report = parse_tabs([("c", encrypt(client_json(), key=OTHER_KEY))], KEY)

    assert report.clients == ()
    assert len(report.skipped) == 1


def test_bsos_without_payload_are_tombstones() -> None:
    """设备注销了，它的标签页记录就成了墓碑。"""
    report = parse_tabs([("gone", None)], KEY)

    assert report.tombstones == 1
    assert report.skipped == ()


def test_records_seen_counts_everything() -> None:
    report = parse_tabs([("gone", None), ("broken", "{}"), ("c", encrypt(client_json("c")))], KEY)

    assert report.records == 3


def test_extra_fields_are_ignored() -> None:
    cleartext = json.dumps({"id": "c", "clientName": "X", "tabs": [], "future": 42})

    report = parse_tabs([("c", encrypt(cleartext))], KEY)

    assert report.clients[0].count == 0
