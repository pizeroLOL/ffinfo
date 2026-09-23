"""KeyBundle 与 EncryptedPayload —— Sync 记录加解密的原语。

测试全部打在这两个公开类的边界上，用官方测试向量验证，**不 mock 任何加密原语**。
"""

from __future__ import annotations

import base64
import json

import pytest

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import DecryptionError, KeyDerivationError
from vectors import AES, SCOPED_KEY


def _official_bundle() -> KeyBundle:
    """向量 1 的那对官方密钥。"""
    return KeyBundle.from_base64(AES.enc_key_b64, AES.hmac_key_b64)


def test_ksync_bytes_split_into_enc_and_mac() -> None:
    bundle = KeyBundle.from_ksync_bytes(bytes.fromhex(SCOPED_KEY.ksync_hex))

    assert bundle.encryption_key.hex() == SCOPED_KEY.enc_key_hex
    assert bundle.hmac_key.hex() == SCOPED_KEY.hmac_key_hex


def test_ksync_base64_split_matches_bytes() -> None:
    scoped = json.loads(SCOPED_KEY.payload_json)
    ksync_b64 = scoped[SCOPED_KEY.scope]["k"]

    bundle = KeyBundle.from_ksync_base64(ksync_b64)

    assert bundle.encryption_key.hex() == SCOPED_KEY.enc_key_hex
    assert bundle.hmac_key.hex() == SCOPED_KEY.hmac_key_hex


@pytest.mark.parametrize("size", [0, 32, 63, 65, 128])
def test_ksync_wrong_length_is_rejected(size: int) -> None:
    with pytest.raises(KeyDerivationError):
        KeyBundle.from_ksync_bytes(b"\x00" * size)


@pytest.mark.parametrize("size", [0, 16, 31, 33])
def test_key_bundle_rejects_wrong_key_sizes(size: int) -> None:
    with pytest.raises(KeyDerivationError):
        KeyBundle(b"\x00" * size, b"\x00" * 32)
    with pytest.raises(KeyDerivationError):
        KeyBundle(b"\x00" * 32, b"\x00" * size)


def test_key_bundle_repr_does_not_leak_keys() -> None:
    bundle = _official_bundle()

    assert AES.enc_key_b64 not in repr(bundle)
    assert AES.hmac_key_b64 not in repr(bundle)


def test_key_bundle_compares_by_key_material() -> None:
    assert _official_bundle() == _official_bundle()
    assert _official_bundle() != KeyBundle(b"\x11" * 32, b"\x22" * 32)


def test_decrypts_official_record() -> None:
    cleartext = _official_bundle().decrypt(AES.ciphertext_b64, AES.iv_b64, AES.hmac_hex)

    assert cleartext == AES.cleartext


def test_encrypts_to_official_ciphertext() -> None:
    ciphertext_b64, iv_b64, hmac_hex = _official_bundle().encrypt(
        AES.cleartext, iv=base64.b64decode(AES.iv_b64)
    )

    assert ciphertext_b64 == AES.ciphertext_b64
    assert iv_b64 == AES.iv_b64
    assert hmac_hex == AES.hmac_hex


def test_encrypt_uses_fresh_iv_when_not_given() -> None:
    bundle = _official_bundle()

    first = bundle.encrypt(AES.cleartext)
    second = bundle.encrypt(AES.cleartext)

    assert first[1] != second[1]
    assert bundle.decrypt(first[0], first[1], first[2]) == AES.cleartext
    assert bundle.decrypt(second[0], second[1], second[2]) == AES.cleartext


def test_tampered_ciphertext_fails_hmac() -> None:
    tampered = AES.ciphertext_b64[:-4] + ("AAAA" if AES.ciphertext_b64[-4:] != "AAAA" else "BBBB")

    with pytest.raises(DecryptionError) as excinfo:
        _official_bundle().decrypt(tampered, AES.iv_b64, AES.hmac_hex)

    assert str(excinfo.value) == "HMAC 校验失败：记录被篡改，或密钥不对"


def test_tampered_hmac_is_rejected() -> None:
    flipped = AES.hmac_hex[:-1] + ("0" if AES.hmac_hex[-1] != "0" else "1")

    with pytest.raises(DecryptionError) as excinfo:
        _official_bundle().decrypt(AES.ciphertext_b64, AES.iv_b64, flipped)

    assert str(excinfo.value) == "HMAC 校验失败：记录被篡改，或密钥不对"


def test_current_tampered_iv_still_passes_hmac() -> None:
    """表征：HMAC 只签密文串、**不覆盖 IV** —— 改 IV 照样过防篡改检查。

    CBC 首块明文 = P0 ⊕ IV ⊕ IV'，攻击者改服务器 payload 里的 IV 即可改写首 16 字节。
    MAC 通过后才在解密/UTF-8 处失败，且失败消息现在点名「IV 可能被篡改」。
    """
    original = base64.b64decode(AES.iv_b64)
    tampered_iv = bytes([original[0] ^ 0xFF]) + original[1:]
    tampered_iv_b64 = base64.b64encode(tampered_iv).decode("ascii")

    with pytest.raises(DecryptionError) as excinfo:
        _official_bundle().decrypt(AES.ciphertext_b64, tampered_iv_b64, AES.hmac_hex)

    message = str(excinfo.value)
    # 不是 MAC 失败的那条文案 —— MAC 没拦下 IV 篡改（协议如此）
    assert "HMAC 校验失败" not in message
    # 失败点名 IV：IV 不在 MAC 覆盖内
    assert "IV" in message


@pytest.mark.parametrize("bad_hmac", ["", "zz", "00", "not-hex", "b1 e6"])
def test_malformed_hmac_is_rejected(bad_hmac: str) -> None:
    with pytest.raises(DecryptionError):
        _official_bundle().decrypt(AES.ciphertext_b64, AES.iv_b64, bad_hmac)


def test_wrong_key_fails_loudly() -> None:
    other = KeyBundle(b"\x11" * 32, b"\x22" * 32)

    with pytest.raises(DecryptionError):
        other.decrypt(AES.ciphertext_b64, AES.iv_b64, AES.hmac_hex)


def test_ciphertext_not_multiple_of_block_is_rejected() -> None:
    truncated = base64.b64encode(base64.b64decode(AES.ciphertext_b64)[:-1]).decode()

    with pytest.raises(DecryptionError):
        _official_bundle().decrypt(truncated, AES.iv_b64, AES.hmac_hex)


def test_encrypted_payload_decrypts_official_vector() -> None:
    payload_json = json.dumps(
        {"IV": AES.iv_b64, "hmac": AES.hmac_hex, "ciphertext": AES.ciphertext_b64}
    )

    payload = EncryptedPayload.from_json(payload_json)

    assert payload.decrypt(_official_bundle()) == AES.cleartext


def test_encrypted_payload_from_cleartext_round_trip() -> None:
    bundle = _official_bundle()

    payload = EncryptedPayload.from_cleartext(bundle, AES.cleartext)

    assert payload.decrypt(bundle) == AES.cleartext
    assert json.loads(payload.to_json())["IV"] == payload.iv


@pytest.mark.parametrize(
    "payload_json",
    [
        '{"hmac":"aa","ciphertext":"bb"}',
        '{"IV":"aa","ciphertext":"bb"}',
        '{"IV":"aa","hmac":"bb"}',
        "not json at all",
    ],
)
def test_encrypted_payload_rejects_malformed_json(payload_json: str) -> None:
    with pytest.raises(DecryptionError):
        EncryptedPayload.from_json(payload_json)
