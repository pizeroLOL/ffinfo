"""``list`` 的报告 union 与**临时**序列化 —— 06 会把序列化收进 ``render.py``。

三份报告模型 ``HistoryReport`` / ``BookmarksReport`` / ``TabsReport`` 各自在自己的模块里声明
（``history.py`` / ``bookmarks.py`` / ``tabs.py``），**互不继承**；这里只声明它们的 union，
以及一个临时的自由 ``to_json``。06 落地后本模块只剩 union 别名。
"""

from __future__ import annotations

import json
from datetime import datetime

from ffinfo_cli.list.bookmarks import BookmarksReport
from ffinfo_cli.list.history import HistoryReport
from ffinfo_cli.list.tabs import TabsReport

type ListReport = HistoryReport | BookmarksReport | TabsReport
"""``list`` 三种子命令的报告 —— 共享的是**形状**，不是基类。"""


def to_json(report: ListReport) -> str:
    """给 agent 消费的扁平 JSON。**临时**放这里：06 会收进 ``render.py`` 并删掉本函数。"""
    return json.dumps(report.model_dump(), ensure_ascii=False, indent=2, default=_json_default)


def _json_default(value: object) -> str:
    """``json.dumps`` 遇到富类型时的兜底 —— 目前只有时间字段的 ``datetime``。

    **统一走 ``isoformat()``**（``+00:00``），与 history 的字符串格式逐字节一致；
    pydantic 自己的 json 模式会写成 ``Z``，两种风格混在一份输出里不好。
    """
    if isinstance(value, datetime):
        return value.isoformat()
    msg = f"JSON 不认识这个类型：{type(value).__name__}"
    raise TypeError(msg)
