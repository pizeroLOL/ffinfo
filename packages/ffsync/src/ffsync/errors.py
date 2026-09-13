"""ffsync 的异常层次。

所有异常都继承自 :class:`FfsyncError`，调用方可以只捕获这一个。
"""

from __future__ import annotations


class FfsyncError(Exception):
    """ffsync 所有异常的基类。"""


class ConfigurationError(FfsyncError):
    """调用方传入的配置有问题（缺参数、路径不存在、权限不对等）。"""


class AuthError(FfsyncError):
    """认证相关失败（授权码无效、PKCE 校验失败、token 过期等）。"""


class KeyDerivationError(FfsyncError):
    """密钥派生失败（scoped key 结构异常、长度不对等）。"""


class DecryptionError(FfsyncError):
    """记录解密失败（HMAC 校验不过、密文损坏等）。"""


class SyncProtocolError(FfsyncError):
    """Sync 存储协议层面的错误（响应格式异常、服务器要求 backoff 等）。"""
