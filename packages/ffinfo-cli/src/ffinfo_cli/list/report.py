"""``list`` 的报告 union —— **只放 union 别名**。

三份报告模型 ``HistoryReport`` / ``BookmarksReport`` / ``TabsReport`` 各自在自己的模块里声明
（``history.py`` / ``bookmarks.py`` / ``tabs.py``），**互不继承**；这里只声明它们的 union。
序列化全部在 ``ffinfo_cli/render.py`` —— 04 的临时自由 ``to_json`` 已在 06 收走。
"""

from __future__ import annotations

from ffinfo_cli.list.bookmarks import BookmarksReport
from ffinfo_cli.list.history import HistoryReport
from ffinfo_cli.list.tabs import TabsReport

__all__ = ["ListReport"]

type ListReport = HistoryReport | BookmarksReport | TabsReport
"""``list`` 三种子命令的报告 —— 共享的是**形状**，不是基类。"""
