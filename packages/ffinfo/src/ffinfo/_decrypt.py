"""三个 collection 共用的一条解密管线。

历史、书签、标签页的批量解密**长得一模一样**：逐条试解，坏掉的跳过并记一笔，
墓碑不进结果。差别只有两处 —— 用哪个模型、以及解出来之后怎么展开
（历史一条记录拍成多次访问；书签与标签页一条记录就是一条）。

放在这里而不是各写一遍：三份拷贝已经开始漂了（跳过原因的文案、墓碑的判法），
而且**单条坏掉不连坐**是三个 collection 共同的硬要求，行为得一致。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from ffinfo.crypto import EncryptedPayload, KeyBundle
from ffinfo.errors import DecryptionError

__all__ = ["DecryptBatch", "decrypt_records", "single"]


@dataclass(frozen=True, slots=True)
class DecryptBatch[ItemT]:
    """一批解出来的东西 + 没解出来的账。"""

    items: tuple[ItemT, ...]
    skipped: tuple[tuple[str, str], ...]
    """坏掉的记录：(id, 短原因)。"""
    tombstones: int
    """墓碑（服务器上被删的）条数 —— 不是失败。"""
    records: int
    """看过的**记录**条数 —— 注意不是 ``len(items)``，历史一条记录能拍成多次访问。"""


def single[ModelT](record: ModelT, _record_id: str) -> tuple[ModelT, ...]:
    """一条记录就是一条 —— 书签 / 标签页的展开方式。"""
    return (record,)


def decrypt_records[ModelT: BaseModel, ItemT](
    records: Iterable[tuple[str, str | None]],
    key: KeyBundle,
    *,
    model: type[ModelT],
    expand: Callable[[ModelT, str], Iterable[ItemT]],
    what: str,
) -> DecryptBatch[ItemT]:
    """逐条解密 + 解析 + 展开。**单条坏掉只跳过并记下来，不连坐。**

    ``records`` 是 ``(记录 id, payload)``；``payload`` 为 ``None`` 表示墓碑。
    ``what`` 是报告里的名字（"历史" / "书签" / "标签页"），拼进跳过原因。
    """
    items: list[ItemT] = []
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
            record = model.model_validate_json(cleartext)
        except (DecryptionError, ValidationError) as exc:
            skipped.append((record_id, _reason(exc, what)))
            continue
        # 书签的墓碑长成 {"deleted": true} —— 模型里有这个字段的才看它
        if getattr(record, "deleted", False):
            tombstones += 1
            continue
        items.extend(expand(record, record_id))

    return DecryptBatch(
        items=tuple(items), skipped=tuple(skipped), tombstones=tombstones, records=seen
    )


def _reason(exc: Exception, what: str) -> str:
    """给跳过的那条记一个短原因 —— 别把整个堆栈塞进 JSON。"""
    if isinstance(exc, DecryptionError):
        return str(exc)
    return f"明文不是合法的{what}记录"
