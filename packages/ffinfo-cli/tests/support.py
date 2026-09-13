"""测试共享的小工具 —— 现造一个**结构真实**的 ``places.sqlite``。

只建我们真正会读的两张表，字段按 Firefox 真实的 schema 来。要造出"Firefox 正在运行"
那种形态（老记录在主文件里、新记录停在 ``-wal`` 里）就用 ``wal=True``。

放在这里而不是某个 test 文件里，是因为 ``test_places_read`` 和 ``test_transfer``
都要用 —— 两份拷贝已经开始漂了。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

__all__ = ["SCHEMA", "US", "add_visits", "build_places", "micros", "us_of"]

US = 1_000_000

SCHEMA = """
CREATE TABLE moz_places (
    id INTEGER PRIMARY KEY,
    url LONGVARCHAR,
    title LONGVARCHAR,
    visit_count INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0,
    typed INTEGER DEFAULT 0
);
CREATE TABLE moz_historyvisits (
    id INTEGER PRIMARY KEY,
    from_visit INTEGER,
    place_id INTEGER,
    visit_date INTEGER,
    visit_type INTEGER,
    session INTEGER
);
"""

Visit = tuple[str, str | None, int, int]
"""``(url, title, visit_date_us, visit_type)``。"""


def us_of(moment: datetime) -> int:
    """UTC ``datetime`` → 微秒。

    **故意不复用产品代码的 ``_time.to_microseconds``** —— 合并两个源靠的就是时间戳
    逐微秒相等，测试里的换算得是独立的一份，否则它只是在验证自己。
    """
    return int(moment.timestamp() * US)


micros = us_of
"""老名字，留着省得改一堆调用点。"""


def build_places(
    path: Path,
    visits: Sequence[Visit],
    *,
    hidden: Sequence[str] = (),
    wal: bool = False,
) -> sqlite3.Connection:
    """造一个最小但结构真实的 ``places.sqlite``。

    ``wal=True`` 时留在 WAL 模式且**不关连接** —— 数据就停在 ``-wal`` 里，
    正是 Firefox 运行中的样子。调用方负责 ``close()``。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.executescript(SCHEMA)
    add_visits(connection, visits, hidden=hidden)
    connection.commit()
    return connection


def add_visits(
    connection: sqlite3.Connection,
    visits: Sequence[Visit],
    *,
    hidden: Sequence[str] = (),
) -> None:
    """往已有的库里加访问 —— 用来模拟"Firefox 又浏览了几页"。"""
    places: dict[str, int] = {
        str(row[0]): int(row[1]) for row in connection.execute("SELECT url, id FROM moz_places")
    }
    for url, title, visit_date, visit_type in visits:
        if url not in places:
            place_id = len(places) + 1
            places[url] = place_id
            connection.execute(
                "INSERT INTO moz_places (id, url, title, hidden) VALUES (?, ?, ?, ?)",
                (place_id, url, title, 1 if url in hidden else 0),
            )
        connection.execute(
            "INSERT INTO moz_historyvisits (place_id, visit_date, visit_type) VALUES (?, ?, ?)",
            (places[url], visit_date, visit_type),
        )
