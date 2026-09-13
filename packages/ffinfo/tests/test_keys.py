"""密钥层次：scoped key → 同步 KeyBundle → 各 collection 的 KeyBundle（01 的另一半）。

测试打在 :mod:`ffinfo.keys` 的公开函数/类上，向量来自官方（见 ``vectors.py``）。
"""

from __future__ import annotations

import json

import pytest

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import DecryptionError, KeyDerivationError
from ffinfo.keys import OLD_SYNC_SCOPE, CollectionKeys, parse_scoped_keys
from vectors import CRYPTO_KEYS, SCOPED_KEY


def _crypto_keys_payload() -> EncryptedPayload:
    return EncryptedPayload(
        iv=CRYPTO_KEYS.iv_b64,
        hmac=CRYPTO_KEYS.hmac_hex,
        ciphertext=CRYPTO_KEYS.ciphertext_b64,
    )


def _root_key() -> KeyBundle:
    """crypto/keys 那条记录用的根密钥（官方向量里的那对）。"""
    return KeyBundle.from_base64(CRYPTO_KEYS.enc_key_b64, CRYPTO_KEYS.hmac_key_b64)


def _collection_keys() -> CollectionKeys:
    return CollectionKeys.from_encrypted_payload(_crypto_keys_payload(), _root_key())


# ── scoped key：OAuth 的产物 ──────────────────────────────────────────────


def test_scoped_key_decodes_to_sync_key_bundle() -> None:
    keys = parse_scoped_keys(SCOPED_KEY.payload_json)

    bundle = keys[OLD_SYNC_SCOPE].to_key_bundle()

    assert bundle.encryption_key.hex() == SCOPED_KEY.enc_key_hex
    assert bundle.hmac_key.hex() == SCOPED_KEY.hmac_key_hex


def test_scoped_key_keeps_its_metadata() -> None:
    key = parse_scoped_keys(SCOPED_KEY.payload_json)[OLD_SYNC_SCOPE]

    assert key.kty == "oct"
    assert key.scope == OLD_SYNC_SCOPE
    assert key.kid == "1526414944666-zgTjf5oXmPmBjxwXWFsDWg"


def test_scoped_key_key_bytes_are_the_64_byte_ksync() -> None:
    key = parse_scoped_keys(SCOPED_KEY.payload_json)[OLD_SYNC_SCOPE]

    assert key.key_bytes().hex() == SCOPED_KEY.ksync_hex
    assert len(key.key_bytes()) == 64


def test_scoped_key_with_wrong_length_is_rejected() -> None:
    payload = json.dumps(
        {OLD_SYNC_SCOPE: {"kty": "oct", "scope": OLD_SYNC_SCOPE, "k": "AAAA", "kid": "1-x"}}
    )
    key = parse_scoped_keys(payload)[OLD_SYNC_SCOPE]

    with pytest.raises(KeyDerivationError):
        key.to_key_bundle()


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"https://identity.mozilla.com/apps/oldsync": {"kty": "oct"}}',
        "[]",
        "null",
    ],
)
def test_malformed_scoped_keys_payload_is_rejected(payload: str) -> None:
    with pytest.raises(KeyDerivationError):
        parse_scoped_keys(payload)


def test_scoped_keys_payload_without_any_scope_is_empty() -> None:
    """空 JSON 不算坏数据 —— 缺哪个 scope 由调用方判断。"""
    assert parse_scoped_keys("{}") == {}


# ── crypto/keys → 各 collection 的密钥 ────────────────────────────────────


def test_collection_keys_come_out_of_the_encrypted_record() -> None:
    keys = _collection_keys()

    assert keys.default.encryption_key.hex() == CRYPTO_KEYS.default_enc_hex
    assert keys.default.hmac_key.hex() == CRYPTO_KEYS.default_mac_hex


def test_collection_overrides_win_over_default() -> None:
    keys = _collection_keys()

    history = keys.key_for_collection("history")
    bookmarks = keys.key_for_collection("bookmarks")

    assert history.encryption_key.hex() == CRYPTO_KEYS.history_enc_hex
    assert history.hmac_key.hex() == CRYPTO_KEYS.history_mac_hex
    assert bookmarks.encryption_key.hex() == CRYPTO_KEYS.bookmarks_enc_hex
    assert bookmarks.hmac_key.hex() == CRYPTO_KEYS.bookmarks_mac_hex
    assert history != keys.default
    assert bookmarks != history


def test_collection_without_override_falls_back_to_default() -> None:
    keys = _collection_keys()

    assert keys.key_for_collection("passwords") == keys.default
    assert keys.key_for_collection("tabs") == keys.default


def test_crypto_keys_with_wrong_root_key_fails_loudly() -> None:
    wrong_root = KeyBundle(b"\x00" * 32, b"\x00" * 32)

    with pytest.raises(DecryptionError):
        CollectionKeys.from_encrypted_payload(_crypto_keys_payload(), wrong_root)


def test_crypto_keys_with_garbage_cleartext_is_rejected() -> None:
    root = _root_key()
    payload = EncryptedPayload.from_cleartext(root, '{"id":"keys"}')

    with pytest.raises(DecryptionError):
        CollectionKeys.from_encrypted_payload(payload, root)


# ── 端到端：一条记录用拿到的 collection 密钥解开 ──────────────────────────


def test_collection_key_actually_decrypts_a_record() -> None:
    keys = _collection_keys()
    history_key = keys.key_for_collection("history")

    payload = EncryptedPayload.from_cleartext(history_key, SCOPED_KEY.payload_json)

    assert payload.decrypt(history_key) == SCOPED_KEY.payload_json
    with pytest.raises(DecryptionError):
        payload.decrypt(keys.default)
