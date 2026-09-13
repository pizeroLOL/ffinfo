"""ffsync —— 纯 Python 的 Firefox Sync 客户端库。

官方 Python 客户端（``mozilla-services/syncclient``）已于 2019 年归档，
本库填补这个空白。

设计约束：**库不持有任何默认路径**，所有 I/O 位置由调用者注入。
"""

from ffsync.credentials import AgeIdentity, CredentialStore
from ffsync.crypto import EncryptedPayload, KeyBundle
from ffsync.errors import FfsyncError
from ffsync.keys import OLD_SYNC_SCOPE, CollectionKeys, ScopedKey, parse_scoped_keys
from ffsync.oauth import Credentials, OAuthClient, OAuthEndpoints, PkcePair

__version__ = "0.1.0"

__all__ = [
    "OLD_SYNC_SCOPE",
    "AgeIdentity",
    "CollectionKeys",
    "CredentialStore",
    "Credentials",
    "EncryptedPayload",
    "FfsyncError",
    "KeyBundle",
    "OAuthClient",
    "OAuthEndpoints",
    "PkcePair",
    "ScopedKey",
    "__version__",
    "parse_scoped_keys",
]
