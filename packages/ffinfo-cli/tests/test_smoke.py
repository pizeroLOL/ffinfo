"""冒烟测试：CLI 能加载、版本正确。"""

from __future__ import annotations

from typer.testing import CliRunner

from ffinfo_cli import __version__
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
