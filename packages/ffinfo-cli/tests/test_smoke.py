"""冒烟测试：CLI 能加载、版本正确。"""

from __future__ import annotations

from typer.testing import CliRunner

from ffinfo import __version__
from ffinfo.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "sync" in result.stdout
