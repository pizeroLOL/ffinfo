"""仓库级约定检查 —— 类级变量必须带类型标注（``AGENTS.md``「代码约定」）。

ruff 的 ANN 只管函数签名，pyright strict 也不强制类属性标注 —— 这条规则没有现成的
lint 能管，所以在这里用 AST 兜住。**检查逻辑本身也有测试**：写错的检查会静默变成
永远绿，``pyproject.toml`` 里 pyright 的 ``include`` 通配坑就是同一类事故。

⚠️ 必须在 **Python 3.14** 下跑（``ast.parse`` 要认 PEP 758 ``except A, B:`` 与
PEP 695 ``type X = ...``）；裸 ``python3`` 可能是 3.12，会把正常代码误报成语法错误。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
"""仓库根 —— 本文件在 ``packages/ffinfo-cli/tests/`` 下。"""

_SOURCE_DIRS = (
    REPO / "packages" / "ffinfo" / "src",
    REPO / "packages" / "ffinfo-cli" / "src",
)


def unannotated_class_attributes(source: str) -> list[tuple[str, int]]:
    """``source`` 里每个 ``ClassDef`` 顶层的裸 ``Name`` 赋值，返回 ``(类名, 行号)``。

    只看类的**直接子语句** —— 方法体、类里的 ``if`` 分支都不算（与 ``AGENTS.md``
    给的检查方式一致）。
    """
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) for target in statement.targets
            ):
                offenders.append((node.name, statement.lineno))
    return offenders


def test_the_checker_flags_a_bare_class_attribute() -> None:
    assert unannotated_class_attributes("class C:\n    x = 1\n") == [("C", 2)]


def test_the_checker_accepts_an_annotated_class_attribute() -> None:
    assert unannotated_class_attributes("class C:\n    x: int = 1\n") == []


def test_the_checker_ignores_locals_inside_methods() -> None:
    assert unannotated_class_attributes("class C:\n    def f(self) -> None:\n        x = 1\n") == []


def test_every_class_attribute_in_src_is_annotated() -> None:
    offenders: list[str] = []
    for directory in _SOURCE_DIRS:
        for path in sorted(directory.rglob("*.py")):
            for klass, lineno in unannotated_class_attributes(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}（class {klass}）")
    assert offenders == [], "类级变量缺类型标注：\n" + "\n".join(offenders)
