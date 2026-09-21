"""三个 collection 共用的解密管线：应用层墓碑 ``{"deleted": true}``。

历史与书签都有这种墓碑，且常缺主体字段（历史的墓碑没有 ``histUri``）。两个不变量是
**必须在模型校验之前认出来**、**复用 pydantic 的 bool 语义**（``1`` / ``"true"`` 也算）。
按 collection 参数化在一处，免得每个模型文件各写一份一样的断言。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from ffinfo.bookmarks import parse_bookmarks
from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.history import decrypt_history

KEY = KeyBundle(encryption_key=b"e" * 32, hmac_key=b"h" * 32)


def encrypt(cleartext: str) -> str:
    return EncryptedPayload.from_cleartext(KEY, cleartext).to_json()


PARSERS: dict[str, Callable[..., Any]] = {
    "history": decrypt_history,
    "bookmarks": parse_bookmarks,
}


@pytest.mark.parametrize("what", sorted(PARSERS))
@pytest.mark.parametrize("deleted", [True, 1])
def test_deleted_flag_is_a_tombstone(what: str, deleted: object) -> None:
    """``deleted: true`` 和 ``deleted: 1``（pydantic 都当 True）都算墓碑，不是坏记录。"""
    cleartext = json.dumps({"id": "gone", "type": "bookmark", "deleted": deleted})

    report = PARSERS[what]([("gone", encrypt(cleartext))], KEY)

    assert report.tombstones == 1
    assert report.skipped == ()
