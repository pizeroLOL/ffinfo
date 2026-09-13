"""JWE 解密：ECDH-ES（直接模式）+ Concat KDF + A256GCM（RFC 7516 / RFC 7518）。

OAuth 授权时我们把 ``keys_jwk``（临时 P-256 公钥）交给 Mozilla，
它把 scope 密钥用这个公钥加密后回一条 ``keys_jwe`` —— 这里负责把它解开。

**只实现解密方向** —— 本库严格只读。
"""

from __future__ import annotations

import hashlib
import struct
from typing import ClassVar, Final, Self

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, ValidationError

from ffinfo._encoding import b64url_encode, record_b64url
from ffinfo.errors import DecryptionError, KeyDerivationError

__all__ = ["EphemeralKeyPair"]

_ECDH_ES: Final = "ECDH-ES"
_A256GCM: Final = "A256GCM"
_COORD_SIZE: Final = 32
_CEK_BITS: Final = 256
_JWE_SEGMENTS: Final = 5


class EphemeralKeyPair:
    """临时 P-256 密钥对：公钥交给 Mozilla 加密，私钥留着解 ``keys_jwe``。"""

    __slots__: tuple[str, ...] = ("_private_key",)

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        """包装一个 P-256 私钥；一般走 :meth:`generate` 或 :meth:`from_private_bytes`。"""
        self._private_key = private_key

    def __repr__(self) -> str:
        """刻意不打印私钥。"""
        return "EphemeralKeyPair(<已隐藏>)"

    # ── 构造 ──────────────────────────────────────────────────────────────

    @classmethod
    def generate(cls) -> Self:
        """现场生成一对新密钥。"""
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_private_bytes(cls, scalar: bytes) -> Self:
        """从 32 字节私钥标量恢复（存下来的私钥、测试向量都走这里）。"""
        try:
            return cls(ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1()))
        except ValueError as exc:
            msg = "不是合法的 P-256 私钥"
            raise KeyDerivationError(msg) from exc

    # ── 公钥 ──────────────────────────────────────────────────────────────

    def public_jwk(self) -> dict[str, str]:
        """给 Mozilla 的 ``keys_jwk`` —— 只有公钥，绝不含 ``d``。"""
        numbers = self._private_key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": b64url_encode(numbers.x.to_bytes(_COORD_SIZE, "big")),
            "y": b64url_encode(numbers.y.to_bytes(_COORD_SIZE, "big")),
        }

    # ── 解密 ──────────────────────────────────────────────────────────────

    def decrypt_jwe(self, jwe: str) -> str:
        """解开一条 JWE，返回明文（对 ``keys_jwe`` 来说就是 scoped keys JSON）。"""
        header_b64, encrypted_key, iv_b64, ciphertext_b64, tag_b64 = _split(jwe)
        header = _parse_header(header_b64)

        if encrypted_key:
            msg = "这是 ECDH-ES 直接模式的 JWE，加密密钥段应该是空的"
            raise DecryptionError(msg)

        cek = self._derive_cek(header)
        try:
            plaintext = AESGCM(cek).decrypt(
                record_b64url(iv_b64, "JWE 的 IV"),
                record_b64url(ciphertext_b64, "JWE 的密文")
                + record_b64url(tag_b64, "JWE 的认证标签"),
                header_b64.encode("ascii"),
            )
        except InvalidTag as exc:
            msg = "JWE 解密失败：认证标签不匹配（密文被篡改，或不是给这把私钥的）"
            raise DecryptionError(msg) from exc

        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = "JWE 的明文不是合法 UTF-8"
            raise DecryptionError(msg) from exc

    def _derive_cek(self, header: _JweHeader) -> bytes:
        """ECDH + Concat KDF → CEK。"""
        epk = header.epk
        if epk.kty != "EC" or epk.crv != "P-256":
            msg = f"不支持的临时公钥类型：{epk.kty}/{epk.crv}"
            raise DecryptionError(msg)
        try:
            peer = ec.EllipticCurvePublicNumbers(
                int.from_bytes(record_b64url(epk.x, "epk.x"), "big"),
                int.from_bytes(record_b64url(epk.y, "epk.y"), "big"),
                ec.SECP256R1(),
            ).public_key()
        except ValueError as exc:
            msg = "JWE 里的临时公钥不是合法的 P-256 点"
            raise DecryptionError(msg) from exc

        shared_secret = self._private_key.exchange(ec.ECDH(), peer)
        return _concat_kdf(shared_secret, header.enc)


class _Epk(BaseModel):
    """JWE header 里的临时公钥。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    kty: str
    crv: str
    x: str
    y: str


class _JweHeader(BaseModel):
    """我们认识的 JWE header 子集。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    alg: str
    enc: str
    epk: _Epk


def _split(jwe: str) -> tuple[str, str, str, str, str]:
    """切成 JWE 的五段，段数不对直接拒绝。"""
    parts = jwe.split(".")
    if len(parts) != _JWE_SEGMENTS:
        msg = f"不是合法的 JWE：应该有 {_JWE_SEGMENTS} 段，实际 {len(parts)} 段"
        raise DecryptionError(msg)
    header_b64, encrypted_key, iv_b64, ciphertext_b64, tag_b64 = parts
    if not header_b64:
        msg = "JWE 的 header 段是空的"
        raise DecryptionError(msg)
    return header_b64, encrypted_key, iv_b64, ciphertext_b64, tag_b64


def _parse_header(header_b64: str) -> _JweHeader:
    """解出并校验 header：只认 ECDH-ES + A256GCM。"""
    raw = record_b64url(header_b64, "JWE 的 header")
    try:
        header = _JweHeader.model_validate_json(raw)
    except ValidationError as exc:
        msg = "JWE 的 header 不是合法 JSON，或缺少 alg / enc / epk"
        raise DecryptionError(msg) from exc

    if header.alg != _ECDH_ES:
        msg = f"不支持的 JWE 算法：{header.alg}（只支持 {_ECDH_ES}）"
        raise DecryptionError(msg)
    if header.enc != _A256GCM:
        msg = f"不支持的 JWE 加密方式：{header.enc}（只支持 {_A256GCM}）"
        raise DecryptionError(msg)
    return header


def _concat_kdf(shared_secret: bytes, algorithm_id: str) -> bytes:
    """RFC 7518 §4.6.2 的 Concat KDF；apu / apv 都为空，一轮 SHA-256 就够 256 位。"""
    alg = algorithm_id.encode("ascii")
    other_info = (
        struct.pack(">I", len(alg))
        + alg
        + struct.pack(">I", 0)  # apu
        + struct.pack(">I", 0)  # apv
        + struct.pack(">I", _CEK_BITS)
    )
    return hashlib.sha256(struct.pack(">I", 1) + shared_secret + other_info).digest()
