"""keys_jwe 解密（OAuth 流程里「拿到 scoped key」那一环）。

官方向量：一条真实 ``keys_jwe`` + 一把固定 P-256 私钥，出自 fxa-client 的
``scoped_keys.rs::test_flow``。算法是 ECDH-ES（直接模式）+ Concat KDF + A256GCM。
"""

from __future__ import annotations

import base64

import pytest

from ffinfo.errors import DecryptionError
from ffinfo.jwe import EphemeralKeyPair
from ffinfo.keys import OLD_SYNC_SCOPE, parse_scoped_keys
from vectors import JWE


def _official_pair() -> EphemeralKeyPair:
    d = base64.urlsafe_b64decode(JWE.private_key_d_b64 + "=" * (-len(JWE.private_key_d_b64) % 4))
    return EphemeralKeyPair.from_private_bytes(d)


def test_decrypts_the_official_keys_jwe() -> None:
    assert _official_pair().decrypt_jwe(JWE.jwe) == JWE.expected_keys_json


def test_result_feeds_straight_into_scoped_key_parsing() -> None:
    """解出来的东西必须能直接喂给 01 的 parse_scoped_keys —— 两条向量的接缝。"""
    keys = parse_scoped_keys(_official_pair().decrypt_jwe(JWE.jwe))

    assert len(keys[OLD_SYNC_SCOPE].key_bytes()) == 64


def test_another_private_key_cannot_open_it() -> None:
    with pytest.raises(DecryptionError):
        EphemeralKeyPair.generate().decrypt_jwe(JWE.jwe)


def test_tampered_ciphertext_is_refused() -> None:
    parts = JWE.jwe.split(".")
    parts[3] = _flip(parts[3])

    with pytest.raises(DecryptionError):
        _official_pair().decrypt_jwe(".".join(parts))


def test_tampered_header_is_refused() -> None:
    parts = JWE.jwe.split(".")
    parts[0] = _flip(parts[0])

    with pytest.raises(DecryptionError):
        _official_pair().decrypt_jwe(".".join(parts))


def test_tampered_tag_is_refused() -> None:
    parts = JWE.jwe.split(".")
    parts[4] = _flip(parts[4])

    with pytest.raises(DecryptionError):
        _official_pair().decrypt_jwe(".".join(parts))


@pytest.mark.parametrize("jwe", ["", "not-a-jwe", "a.b.c", "a.b.c.d.e.f", "....", "....."])
def test_malformed_jwe_is_rejected(jwe: str) -> None:
    with pytest.raises(DecryptionError):
        _official_pair().decrypt_jwe(jwe)


def _flip(segment: str) -> str:
    """把一段 base64url 的最后 4 个字符换掉，保持长度。"""
    tail = "AAAA" if segment[-4:] != "AAAA" else "BBBB"
    return segment[:-4] + tail


def test_public_jwk_is_a_p256_key_without_the_private_part() -> None:
    jwk = EphemeralKeyPair.generate().public_jwk()

    assert jwk["kty"] == "EC"
    assert jwk["crv"] == "P-256"
    assert set(jwk) == {"kty", "crv", "x", "y"}  # 绝不能带 d
    assert len(base64.urlsafe_b64decode(jwk["x"] + "==")) == 32
    assert len(base64.urlsafe_b64decode(jwk["y"] + "==")) == 32


def test_each_generated_pair_is_different() -> None:
    assert EphemeralKeyPair.generate().public_jwk() != EphemeralKeyPair.generate().public_jwk()
