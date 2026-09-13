"""浏览历史记录的解析与解密（05 号 ticket）。

Sync 里的历史记录长这样（``places/src/history_sync/record.rs``）::

    {
        "id": "…",
        "title": "…",
        "histUri": "https://…",
        "visits": [{"date": 1788444520420000, "type": 1}],
    }

三个容易踩的点：

1. ``date`` 是**微秒**（``ServerVisitTimestamp`` = 毫秒 × 1000），不是秒也不是毫秒
2. 访问类型的字段名是 ``type``，不是 ``transition``（Rust 那边才叫 transition）
3. ``title`` 可能是 ``null`` 或者干脆没有 —— 上游明确处理过这种情况，我们也得认

一条记录可以有**多次访问**（最多 20 次，见 ``docs/design.md`` §3.1）。
本模块把它们拍平成"一次访问一行" —— 那才是"浏览历史"该有的样子。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import DecryptionError

__all__ = [
    "VISIT_TYPES",
    "DecryptionReport",
    "HistoryEntry",
    "HistoryRecord",
    "HistoryVisit",
    "decrypt_history",
    "visit_type_name",
]

VISIT_TYPES: Final[dict[int, str]] = {
    1: "link",
    2: "typed",
    3: "bookmark",
    4: "embed",
    5: "redirect_permanent",
    6: "redirect_temporary",
    7: "download",
    8: "framed_link",
    9: "reload",
    10: "update_place",
}
"""``VisitType``（``places/src/types.rs``）—— 数字对名字。

表里没有的取值不报错，``visit_type_name`` 会给 ``unknown`` ——
Mozilla 将来加新类型不该把我们打挂。

⚠️ ``10 = update_place`` 按上游注释**不是一次真正的页面访问**（"Internal visit type used
for meta data updates"），但它确实会出现在同步记录里。我们照实标名字、不偷偷丢掉 ——
要不要算进"浏览历史"由消费方决定。
"""

_MICROSECONDS: Final = 1_000_000


def visit_type_name(visit_type: int) -> str:
    """访问类型的名字；没见过的取值返回 ``unknown``。"""
    return VISIT_TYPES.get(visit_type, "unknown")


class HistoryVisit(BaseModel):
    """一次访问。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    date: int
    """访问时间，**微秒**（Unix epoch）。"""

    type: int = 0
    """``VisitType`` 的数值。"""

    @property
    def visited_at(self) -> datetime:
        """访问时间，UTC。"""
        return datetime.fromtimestamp(self.date / _MICROSECONDS, tz=UTC)

    @property
    def type_name(self) -> str:
        """访问类型的名字。"""
        return visit_type_name(self.type)


class HistoryRecord(BaseModel):
    """一条历史记录的**明文**（``histUri`` 那条记录解开之后的样子）。"""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    id: str
    title: str = ""
    hist_uri: str = Field(alias="histUri")
    visits: tuple[HistoryVisit, ...] = ()

    @field_validator("title", mode="before")
    @classmethod
    def _empty_title_for_null(cls, value: object) -> object:
        """``title`` 是 ``null`` 时当空串 —— 上游也这么干的（那边有专门的测试）。"""
        return "" if value is None else value

    def entries(self, *, record_id: str | None = None) -> list[HistoryEntry]:
        """拍平成"一次访问一行"。"""
        source = record_id if record_id is not None else self.id
        return [
            HistoryEntry(
                record_id=source,
                url=self.hist_uri,
                title=self.title,
                visited_at=visit.visited_at,
                visit_type=visit.type,
                visit_type_name=visit.type_name,
            )
            for visit in self.visits
        ]


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """拍平之后的一条浏览 —— 一次访问一行。"""

    record_id: str
    url: str
    title: str
    visited_at: datetime
    visit_type: int
    visit_type_name: str


@dataclass(frozen=True, slots=True)
class DecryptionReport:
    """批量解密的结果。

    ``skipped`` 里每项是 ``(记录 id, 失败原因)`` —— 单条坏掉不连坐，但也不假装没发生。
    """

    entries: tuple[HistoryEntry, ...]
    skipped: tuple[tuple[str, str], ...]
    tombstones: int
    """墓碑（在别的设备上被删掉的记录）—— 没有 payload，不是失败。"""

    records: int
    """看过的**记录**条数。注意不是 ``len(entries)`` —— 一条记录可以有多次访问。"""


def decrypt_history(records: Iterable[tuple[str, str | None]], key: KeyBundle) -> DecryptionReport:
    """批量解密并解析。

    ``records`` 是 ``(记录 id, payload)``；``payload`` 为 ``None`` 表示墓碑。

    **单条失败只跳过并记下来**（ticket 05 的硬要求）——
    一条被篡改的记录不该让你看不到另外四千条。
    """
    entries: list[HistoryEntry] = []
    skipped: list[tuple[str, str]] = []
    tombstones = 0
    seen = 0

    for record_id, payload in records:
        seen += 1
        if payload is None:
            tombstones += 1
            continue
        try:
            cleartext = EncryptedPayload.from_json(payload).decrypt(key)
            record = HistoryRecord.model_validate_json(cleartext)
        except (DecryptionError, ValidationError) as exc:
            skipped.append((record_id, _reason(exc)))
            continue
        entries.extend(record.entries(record_id=record_id))

    return DecryptionReport(
        entries=tuple(entries), skipped=tuple(skipped), tombstones=tombstones, records=seen
    )


def _reason(exc: Exception) -> str:
    """给跳过的那条记一个短原因 —— 别把整个堆栈塞进 JSON。"""
    if isinstance(exc, DecryptionError):
        return str(exc)
    return "明文不是合法的历史记录"
