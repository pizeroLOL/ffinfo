"""ffinfo 的异常层次。

所有异常都继承自 :class:`FfinfoError`，调用方可以只捕获这一个。
"""

from __future__ import annotations


class FfinfoError(Exception):
    """ffinfo 所有异常的基类。"""


class ConfigurationError(FfinfoError):
    """调用方传入的配置有问题（缺参数、路径不存在、权限不对等）。"""


class AuthError(FfinfoError):
    """认证相关失败（授权码无效、PKCE 校验失败、token 过期等）。"""


class KeyDerivationError(FfinfoError):
    """密钥派生失败（scoped key 结构异常、长度不对等）。"""


class DecryptionError(FfinfoError):
    """记录解密失败（HMAC 校验不过、密文损坏等）。"""


class SyncProtocolError(FfinfoError):
    """Sync 存储协议层面的错误（响应格式异常、节点重分配、拉取不完整等）。"""


class BackoffError(FfinfoError):
    """服务器要求退避 —— **不是错误，是"现在别来"**。

    与 :class:`SyncProtocolError` 平级而不是它的子类：调用方通常要单独处理
    （告诉用户"过 N 秒再来"），把它混进"协议出错"里会让这个分支藏起来。

    ``wait_seconds`` 是服务器要的秒数，``soft`` 区分两种退避：

    * ``soft=True`` —— ``X-Weave-Backoff``，服务器还能干活但压力大，**可以出现在 200 响应上**
    * ``soft=False`` —— ``Retry-After``，配 503（维护）或 409（冲突），硬性要求等待
    """

    def __init__(self, message: str, *, wait_seconds: float, soft: bool) -> None:
        """带上要等多久、以及是软退避还是硬退避。"""
        super().__init__(message)
        self.wait_seconds = wait_seconds
        self.soft = soft
