"""``ffinfo-cli list`` 的逻辑包 —— 只在这里 re-export 对外入口。

三种数据类型各是一个子命令，各有一个 ``run_*``（异步）与 ``list_*_blocking``（同步）：

* :mod:`ffinfo_cli.list.history` —— 双源合并 → 一次访问一行
* :mod:`ffinfo_cli.list.bookmarks` —— 建树 / 剪枝
* :mod:`ffinfo_cli.list.tabs` —— 按设备分组

三份报告模型各自在自己的模块里（互不继承），``report`` 只放 union 别名；
``common`` 放三边共用的外壳、过滤口径与契约类型。``--data-type`` 字符串 dispatcher
已经删掉 —— 一个类型一个入口。序列化统一在 ``ffinfo_cli/render.py``。
"""

from __future__ import annotations

from ffinfo_cli.list.bookmarks import BookmarksReport, list_bookmarks_blocking, run_bookmarks
from ffinfo_cli.list.common import (
    SourceName,
    VisitSource,
    completion_values,
    matches_domain,
    matches_search,
    parse_since,
)
from ffinfo_cli.list.history import (
    HistoryItem,
    HistoryReport,
    list_history_blocking,
    run_history,
)
from ffinfo_cli.list.report import ListReport
from ffinfo_cli.list.tabs import TabsReport, list_tabs_blocking, run_tabs

__all__ = [
    "BookmarksReport",
    "HistoryItem",
    "HistoryReport",
    "ListReport",
    "SourceName",
    "TabsReport",
    "VisitSource",
    "completion_values",
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
