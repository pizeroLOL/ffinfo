"""base64 / hex 解码 —— 全库只在这一处处理编码错误。

同步协议里两种数据的错误语义不同，上层要分开处理：

* **密钥材料**坏掉 → :class:`~ffsync.errors.KeyDerivationError` —— 整体失败，没救
* **记录**坏掉 → :class:`~ffsync.errors.DecryptionError` —— 单条跳过并计数（见 05 号 ticket）

所以这里**成对**提供解码器，让调用点一眼看出拿到的是哪种语义，
而不是把异常类型当参数传进来。
"""

from __future__ import annotations

import base64
import binascii
from typing import Final

from ffsync.errors import DecryptionError, FfsyncError, KeyDerivationError

__all__ = ["key_b64", "key_b64url", "record_b64", "record_hex"]

_URLSAFE_PAD: Final = 4


def key_b64(data: str, what: str) -> bytes:
    """标准 base64（带填充）→ 字节。**密钥材料**专用。"""
    return _decode_base64(data, what, KeyDerivationError)


def key_b64url(data: str, what: str) -> bytes:
    """base64url（无填充）→ 字节。**密钥材料**专用。"""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % _URLSAFE_PAD))
    except (binascii.Error, ValueError) as exc:
        msg = f"{what} 不是合法的 base64url"
        raise KeyDerivationError(msg) from exc


def record_b64(data: str, what: str) -> bytes:
    """标准 base64（带填充）→ 字节。**记录字段**专用。"""
    return _decode_base64(data, what, DecryptionError)


def record_hex(data: str, what: str) -> bytes:
    """十六进制 → 字节。**记录字段**专用。"""
    try:
        return binascii.unhexlify(data)
    except (binascii.Error, ValueError) as exc:
        msg = f"{what} 不是合法的十六进制"
        raise DecryptionError(msg) from exc


def _decode_base64(data: str, what: str, error: type[FfsyncError]) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = f"{what} 不是合法的 base64"
        raise error(msg) from exc
