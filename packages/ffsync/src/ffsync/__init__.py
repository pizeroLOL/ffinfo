"""ffsync —— 纯 Python 的 Firefox Sync 客户端库。

官方 Python 客户端（``mozilla-services/syncclient``）已于 2019 年归档，
本库填补这个空白。

设计约束：**库不持有任何默认路径**，所有 I/O 位置由调用者注入。
"""

from ffsync.errors import FfsyncError

__version__ = "0.1.0"

__all__ = ["FfsyncError", "__version__"]
