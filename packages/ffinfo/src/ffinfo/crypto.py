"""Sync 记录的加解密原语：:class:`KeyBundle` 与 :class:`EncryptedPayload`。

协议出处：Sync storage format v5（``syncstorage-rs/docs/src/sync-client/global-storage-v5.md``）
与 ``application-services/components/sync15/src/key_bundle.rs``。

三条容易写错的规则，这里都按官方实现来：

1. 密钥对是 **enc_key(32B) + hmac_key(32B)**，由 64 字节的 kSync 直接切分（前 32 / 后 32）。
2. 记录用 **AES-256-CBC + PKCS#7** 加密，IV 每条随机 16 字节。
3. HMAC-SHA256 **算在 base64 之后的密文字符串上**，不是算在原始密文字节上。

本模块只向 ``cryptography`` 借 AES-CBC 与 PKCS#7 padding；HMAC 走标准库，
以便用 :func:`hmac.compare_digest` 做常数时间比较。
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_lib
import os
from dataclasses import dataclass
from typing import ClassVar, Final, Self

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ffinfo._encoding import key_b64, key_b64url, record_b64, record_hex
from ffinfo.errors import ConfigurationError, DecryptionError, KeyDerivationError

__all__ = ["EncryptedPayload", "KeyBundle"]

_KEY_SIZE: Final = 32
"""单个密钥的字节数（AES-256 / HMAC-SHA256 都是 256 位）。"""

_KSYNC_SIZE: Final = _KEY_SIZE * 2
"""kSync 的字节数：enc_key 与 hmac_key 拼在一起。"""

_IV_SIZE: Final = 16
"""AES 块大小，也是 IV 的长度。"""

_BLOCK_BITS: Final = 128


@dataclass(frozen=True, slots=True, repr=False)
class KeyBundle:
    """一对 256 位对称密钥：AES-256 加密密钥 + HMAC-SHA256 签名密钥。

    实例不可变，比较按密钥material 逐字节进行。``repr`` 刻意不打印密钥。
    """

    encryption_key: bytes
    hmac_key: bytes

    def __post_init__(self) -> None:
        """校验两个密钥都是 32 字节 —— 长度不对就地失败，不留"半坏"的对象。"""
        if len(self.encryption_key) != _KEY_SIZE:
            msg = f"加密密钥必须是 {_KEY_SIZE} 字节，收到 {len(self.encryption_key)} 字节"
            raise KeyDerivationError(msg)
        if len(self.hmac_key) != _KEY_SIZE:
            msg = f"HMAC 密钥必须是 {_KEY_SIZE} 字节，收到 {len(self.hmac_key)} 字节"
            raise KeyDerivationError(msg)

    def __repr__(self) -> str:
        """刻意不打印密钥材料。"""
        return f"KeyBundle(encryption_key=<{_KEY_SIZE} 字节>, hmac_key=<{_KEY_SIZE} 字节>)"

    @classmethod
    def from_ksync_bytes(cls, ksync: bytes) -> Self:
        """从 64 字节 kSync 切出密钥对：前 32 字节加密，后 32 字节签名。"""
        if len(ksync) != _KSYNC_SIZE:
            msg = f"kSync 必须是 {_KSYNC_SIZE} 字节，收到 {len(ksync)} 字节"
            raise KeyDerivationError(msg)
        return cls(encryption_key=ksync[:_KEY_SIZE], hmac_key=ksync[_KEY_SIZE:])

    @classmethod
    def from_ksync_base64(cls, ksync: str) -> Self:
        """从 base64url（无填充）编码的 kSync 切出密钥对。"""
        return cls.from_ksync_bytes(key_b64url(ksync, "kSync"))

    @classmethod
    def from_base64(cls, encryption_key: str, hmac_key: str) -> Self:
        """从两个标准 base64 字符串（带填充）构造。"""
        return cls(
            encryption_key=key_b64(encryption_key, "加密密钥"),
            hmac_key=key_b64(hmac_key, "HMAC 密钥"),
        )

    def decrypt(self, ciphertext_b64: str, iv_b64: str, hmac_hex: str) -> str:
        """校验 HMAC 后解密，返回明文。

        HMAC 不过就**绝不**解密 —— 任何异常都收敛成 :class:`~ffinfo.errors.DecryptionError`。
        """
        ciphertext = record_b64(ciphertext_b64, "ciphertext")
        iv = record_b64(iv_b64, "IV")
        expected_mac = record_hex(hmac_hex, "hmac")

        if not hmac_lib.compare_digest(self._sign(ciphertext_b64), expected_mac):
            msg = "HMAC 校验失败：记录被篡改，或密钥不对"
            raise DecryptionError(msg)

        cleartext = self._aes_decrypt(ciphertext, iv)
        try:
            return cleartext.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = "解密结果不是合法的 UTF-8"
            raise DecryptionError(msg) from exc

    def encrypt(self, cleartext: str, *, iv: bytes | None = None) -> tuple[str, str, str]:
        """加密，返回 ``(ciphertext_b64, iv_b64, hmac_hex)``。

        不传 ``iv`` 时随机生成；传入固定 IV 只用于对着官方向量做逐字节验证。
        """
        if iv is None:
            iv = os.urandom(_IV_SIZE)
        elif len(iv) != _IV_SIZE:
            msg = f"IV 必须是 {_IV_SIZE} 字节，收到 {len(iv)} 字节"
            raise ConfigurationError(msg)

        ciphertext = self._aes_encrypt(cleartext.encode("utf-8"), iv)
        ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")
        return (
            ciphertext_b64,
            base64.b64encode(iv).decode("ascii"),
            self._sign(ciphertext_b64).hex(),
        )

    def _sign(self, ciphertext_b64: str) -> bytes:
        """HMAC-SHA256 —— 注意签的是 **base64 字符串**，不是原始密文字节。"""
        return hmac_lib.new(self.hmac_key, ciphertext_b64.encode("ascii"), hashlib.sha256).digest()

    def _aes_encrypt(self, cleartext: bytes, iv: bytes) -> bytes:
        padder = padding.PKCS7(_BLOCK_BITS).padder()
        padded = padder.update(cleartext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self.encryption_key), modes.CBC(iv)).encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def _aes_decrypt(self, ciphertext: bytes, iv: bytes) -> bytes:
        if len(iv) != _IV_SIZE:
            msg = f"IV 必须是 {_IV_SIZE} 字节，收到 {len(iv)} 字节"
            raise DecryptionError(msg)
        try:
            decryptor = Cipher(algorithms.AES(self.encryption_key), modes.CBC(iv)).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = padding.PKCS7(_BLOCK_BITS).unpadder()
            return unpadder.update(padded) + unpadder.finalize()
        except ValueError as exc:
            # 密文长度不是块大小的整数倍，或 PKCS#7 填充非法
            msg = "密文无法解密：长度或填充非法"
            raise DecryptionError(msg) from exc


class EncryptedPayload(BaseModel):
    """服务器上一条记录的 ``payload`` 字段：``{"IV": …, "hmac": …, "ciphertext": …}``。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, populate_by_name=True, extra="ignore"
    )

    iv: str = Field(alias="IV")
    hmac: str
    ciphertext: str

    @classmethod
    def from_json(cls, payload: str) -> Self:
        """解析服务器给的 payload 字符串。结构不对就抛 :class:`DecryptionError`。"""
        try:
            return cls.model_validate_json(payload)
        except ValidationError as exc:
            msg = "payload 不是合法的加密记录"
            raise DecryptionError(msg) from exc

    def to_json(self) -> str:
        """序列化回 payload 字符串（字段顺序与官方一致：IV / hmac / ciphertext）。"""
        return self.model_dump_json(by_alias=True)

    @classmethod
    def from_cleartext(cls, key: KeyBundle, cleartext: str) -> Self:
        """用给定密钥加密一段明文，得到 payload。"""
        ciphertext_b64, iv_b64, hmac_hex = key.encrypt(cleartext)
        # 按线上别名构造 —— pyright 认不出 populate_by_name（它以为参数名是 "IV"）
        return cls.model_validate({"IV": iv_b64, "hmac": hmac_hex, "ciphertext": ciphertext_b64})

    def decrypt(self, key: KeyBundle) -> str:
        """用给定密钥解开，返回明文。"""
        return key.decrypt(self.ciphertext, self.iv, self.hmac)
