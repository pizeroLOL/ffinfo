"""sync 记录的「写入何时覆盖」策略 —— 三条写路径共用的一个判定核心。

入站一批（可能含重复 id、陈数据、墓碑）对**已有行**的判定只在本 module 写一遍：

* **批内折到最新** —— 同键多次出现只认 ``modified`` 最大（平手留后面的）
* **陈数据永不赢** —— 已有行且入站 ``modified`` 不更大 ⇒ 不覆盖
  （``filter`` 是设计上的例外：全量整体替换以这一批为准，不与库里比新旧）
* **墓碑处置作参数** —— 三分支语义**故意不合并**（产品决策，见 :data:`TombstoneDisposition`）

三条路径怎么接：

===============  =====================  =========================================
路径             ``disposition``        消费的判定
===============  =====================  =========================================
增量 ``_apply``  ``delete``             ``inserts`` / ``updates`` / ``deletes``
全量 ``_replace`` ``filter``            ``live`` + 三份计数（对账口径）
import merge     ``keep``               ``inserts`` / ``updates`` / ``kept``
===============  =====================  =========================================

firefox 访问的身份键 ``(machine, url, visited_at)`` 是**另一套策略**（明文、无墓碑、
标题变了算更新）—— 不进本核心，留在 ``store.store_firefox_visits``。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "MergeableRecord",
    "SyncWritePlan",
    "TombstoneDisposition",
    "plan_sync_writes",
]


type TombstoneDisposition = Literal["delete", "keep", "filter"]
"""墓碑怎么处置 —— 三条路径三种语义，**不统一**：

* ``delete`` —— 增量：命中已有行就删；没行当没发生
* ``keep`` —— import：计进 ``kept``，一行不动
* ``filter`` —— 全量：从存活集筛掉；整批即新全集，不在里面的已有行按对账删除
"""


class MergeableRecord(Protocol):
    """策略核心认的入站形状 —— 两条路径的记录类型都结构上满足它。"""

    @property
    def modified(self) -> float:
        """这条记录自己的修改时间 —— 陈数据比较认它。"""
        ...

    @property
    def payload(self) -> str | None:
        """``None`` = 墓碑（这条在别的设备上被删了）。"""
        ...


@dataclass(frozen=True, slots=True)
class SyncWritePlan[K, R: MergeableRecord]:
    """入站一批（已折到每键最新）对已有行的判定结果。"""

    inserts: list[R]
    """库里没有这个键 —— 新行。"""

    updates: list[R]
    """已有行且判定要覆盖的入站（``modified`` 更大；``filter`` 下存活即覆盖）。"""

    stale: list[R]
    """已有行、入站较旧 —— **陈数据永不赢**。``filter`` 下不会出现（整批以批为准）。"""

    deletes: list[K]
    """要删的已有行键。``delete``：墓碑命中；``filter``：存活集之外的已有行（对账）。"""

    kept: list[R]
    """保住不写的入站 —— import 的 ``kept`` 口径：墓碑 + 陈数据。"""

    live: list[R]
    """真正落盘的入站（``inserts`` ∪ ``updates``），保持批内折叠后的顺序。"""


def plan_sync_writes[K, R: MergeableRecord](
    incoming: Sequence[R],
    existing: Mapping[K, float],
    *,
    key_of: Callable[[R], K],
    disposition: TombstoneDisposition,
) -> SyncWritePlan[K, R]:
    """入站集合 → 对已有行的判定。**三条 sync 记录写路径唯一的覆盖策略实现。**

    ``existing`` 是键 → 已有行的 ``modified``；全量路径用它对账（键集合决定
    ``deletes``，不比 ``modified`` —— 整体替换以这一批为准）。

    ``key_of`` 把入站折到身份键：单 collection 的路径用记录 id，import 用
    ``(collection, record_id)``。算法只此一份 —— 不再按记录形状复制两份。
    """
    inserts: list[R] = []
    updates: list[R] = []
    stale: list[R] = []
    deletes: list[K] = []
    kept: list[R] = []
    live: list[R] = []
    live_keys: set[K] = set()

    folded: dict[K, R] = {}
    for record in incoming:
        key = key_of(record)
        current = folded.get(key)
        if current is None or record.modified >= current.modified:
            folded[key] = record

    for key, record in folded.items():
        if record.payload is None:
            match disposition:
                case "delete":
                    if key in existing:
                        deletes.append(key)
                case "keep":
                    kept.append(record)
                case "filter":
                    pass  # 筛出存活集；已有行是否消失由下面的对账负责
            continue
        if key not in existing:
            inserts.append(record)
            live.append(record)
            live_keys.add(key)
        elif disposition == "filter" or record.modified > existing[key]:
            updates.append(record)
            live.append(record)
            live_keys.add(key)
        else:
            stale.append(record)
            if disposition == "keep":
                kept.append(record)

    if disposition == "filter":
        deletes = [key for key in existing if key not in live_keys]

    return SyncWritePlan(
        inserts=inserts,
        updates=updates,
        stale=stale,
        deletes=deletes,
        kept=kept,
        live=live,
    )
