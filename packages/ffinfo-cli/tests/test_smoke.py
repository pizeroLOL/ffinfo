"""冒烟测试：CLI 能加载、版本正确、公开面每个操作只有一个入口。"""

from __future__ import annotations

from typer.testing import CliRunner

from ffinfo_cli import __version__, login, profiles, sync, transfer
from ffinfo_cli import list as list_pkg
from ffinfo_cli.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("sync", "list", "export", "import"):
        assert command in result.stdout


def test_each_operation_exports_exactly_one_entry() -> None:
    """每个操作只有一份公开 interface —— ``*_blocking`` 薄包装与重复导出已消失。

    新参数只需要改**一个**名字（``run_*`` / ``build_report``）：导出面证明没有第二份
    签名要跟着动。async 入口仍可直接喂假 ``http`` / ``clock`` / ``warn``。
    """
    assert {"run_history", "run_bookmarks", "run_tabs"} <= set(list_pkg.__all__)
    assert not any(name.endswith("_blocking") for name in list_pkg.__all__)
    assert {"run_export", "run_import"} <= set(transfer.__all__)
    assert not any(name.endswith("_blocking") for name in transfer.__all__)
    assert "build_report" in profiles.__all__
    assert not any(name.endswith("_blocking") for name in profiles.__all__)
    for module, entry, wrapper in (
        (sync, "run_sync", "sync_blocking"),
        (login, "run_login", "login_sync"),
    ):
        assert hasattr(module, entry)
        assert not hasattr(module, wrapper)
