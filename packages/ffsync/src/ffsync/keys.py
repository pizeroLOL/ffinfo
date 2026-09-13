"""密钥层次：scoped key（OAuth 的产物）→ 同步 KeyBundle → 各 collection 的 KeyBundle。

这一层是 01 号 ticket 的"密钥派生链"，链路是：

    keys_jwe 解密后的 JSON → ScopedKey.k（64 字节 kSync）→ KeyBundle
        → 用它解开 crypto/keys 记录 → 得到 default 与各 collection 的 KeyBundle

注意：**这里没有 HKDF**。HKDF 那一步（kB → kSync）是 Mozilla 在发 keys_jwe 之前替客户端做的，
我们拿到的 scoped key 就是 kSync 本体。见 ``docs/design.md`` §3.2 与 Sync storage format v5。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, ValidationError

from ffsync._encoding import key_b64url
from ffsync.crypto import EncryptedPayload, KeyBundle
from ffsync.errors import DecryptionError, KeyDerivationError

__all__ = ["OLD_SYNC_SCOPE", "CollectionKeys", "ScopedKey", "parse_scoped_keys"]

OLD_SYNC_SCOPE: Final = "https://identity.mozilla.com/apps/oldsync"
"""整本浏览数据（含 history / bookmarks / tabs）所在的 scope。"""


class ScopedKey(BaseModel):
    """``keys_jwe`` 解开后的一个 scope 密钥（Mozilla 的 ScopedKey 结构）。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kty: str
    scope: str
    k: str
    kid: str

    def key_bytes(self) -> bytes:
        """``k`` 的原始字节。oldsync 的 scoped key 是 64 字节，就是 kSync。"""
        return key_b64url(self.k, f"scoped key 的 k（scope={self.scope}）")

    def to_key_bundle(self) -> KeyBundle:
        """切出同步用的 :class:`~ffsync.crypto.KeyBundle`。"""
        return KeyBundle.from_ksync_bytes(self.key_bytes())


def parse_scoped_keys(payload: str) -> dict[str, ScopedKey]:
    """解析 ``keys_jwe`` 解密后的 JSON：``{scope: ScopedKey}``。"""
    try:
        raw = json.loads(payload)
        parsed = {scope: ScopedKey.model_validate(value) for scope, value in raw.items()}
    except (json.JSONDecodeError, ValidationError, AttributeError) as exc:
        msg = "keys_jwe 的内容不是合法的 scoped keys JSON"
        raise KeyDerivationError(msg) from exc
    return parsed


@dataclass(frozen=True, slots=True)
class CollectionKeys:
    """``crypto/keys`` 记录解开后的样子：一个 default 密钥对，加上若干 collection 覆盖。"""

    default: KeyBundle
    collections: dict[str, KeyBundle]

    @classmethod
    def from_encrypted_payload(cls, payload: EncryptedPayload, root_key: KeyBundle) -> Self:
        """用同步 KeyBundle 解开 ``crypto/keys`` 记录。"""
        record = _CryptoKeysRecord.from_json(payload.decrypt(root_key))
        return cls(
            default=KeyBundle.from_base64(*record.default),
            collections={
                name: KeyBundle.from_base64(*pair) for name, pair in record.collections.items()
            },
        )

    def key_for_collection(self, collection: str) -> KeyBundle:
        """取某个 collection 的密钥；没有覆盖时回落到 default。"""
        return self.collections.get(collection, self.default)


class _CryptoKeysRecord(BaseModel):
    """``crypto/keys`` 的明文结构（``sync15/src/record_types.rs`` 的 ``CryptoKeysRecord``）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    collection: str
    default: tuple[str, str]
    collections: dict[str, tuple[str, str]] = {}

    @classmethod
    def from_json(cls, cleartext: str) -> Self:
        try:
            return cls.model_validate_json(cleartext)
        except ValidationError as exc:
            msg = "crypto/keys 记录的结构不对"
            raise DecryptionError(msg) from exc
