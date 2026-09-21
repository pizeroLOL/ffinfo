"""``ffinfo-cli list`` 的逻辑包 —— 只在这里 re-export 对外入口。

三种数据类型各是一个子命令，各有一个 ``run_*``（异步）与 ``list_*_blocking``（同步）：

* :mod:`ffinfo_cli.list.history` —— 双源合并 → 一次访问一行
* :mod:`ffinfo_cli.list.bookmarks` —— 建树 / 剪枝
* :mod:`ffinfo_cli.list.tabs` —— 按设备分组

``common`` 放三边共用的外壳、过滤口径与报告模型。``--data-type`` 字符串 dispatcher
已经删掉 —— 一个类型一个入口。
"""

from __future__ import annotations

from ffinfo_cli.list.bookmarks import list_bookmarks_blocking, run_bookmarks
from ffinfo_cli.list.common import (
    HistoryItem,
    ListReport,
    SourceName,
    VisitSource,
    matches_domain,
    matches_search,
    parse_since,
)
from ffinfo_cli.list.history import list_history_blocking, run_history
from ffinfo_cli.list.tabs import list_tabs_blocking, run_tabs

__all__ = [
    "HistoryItem",
    "ListReport",
    "SourceName",
    "VisitSource",
    "list_bookmarks_blocking",
    "list_history_blocking",
    "list_tabs_blocking",
    "matches_domain",
    "matches_search",
    "parse_since",
    "run_bookmarks",
    "run_history",
    "run_tabs",
]
