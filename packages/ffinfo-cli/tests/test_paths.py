"""CLI 层：默认路径落在哪 —— 三平台各走哪，以及 **macOS 为什么跟 Linux 共用 XDG**。

路径的决定写在 ``docs/design.md`` §2.5；这里只负责把它锁住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ffinfo_cli.paths import credentials_path, data_dir, identity_path


@pytest.mark.skipif(sys.platform == "win32", reason="XDG 是 POSIX 的约定")
def test_paths_follow_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert identity_path() == tmp_path / "cfg" / "ffinfo-cli" / "age-key.txt"
    assert credentials_path() == tmp_path / "data" / "ffinfo-cli" / "credentials.age"
    assert data_dir() == tmp_path / "data" / "ffinfo-cli"


@pytest.mark.skipif(sys.platform == "win32", reason="这条断言要在 POSIX 上跑")
def test_paths_use_xdg_on_macos_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """macOS **有意**走 XDG，不碰 ``~/Library/Application Support``。

    不是"蹭到了 else 分支"—— 是决定。所以这里把平台显式改成 ``darwin``：
    哪天有人给 macOS 单开一条原生路径，这条会红。
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert identity_path() == tmp_path / "cfg" / "ffinfo-cli" / "age-key.txt"
    assert credentials_path() == tmp_path / "data" / "ffinfo-cli" / "credentials.age"
    assert data_dir() == tmp_path / "data" / "ffinfo-cli"


@pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 才有 %APPDATA%")
def test_paths_use_appdata_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert identity_path() == tmp_path / "roaming" / "ffinfo-cli" / "age-key.txt"
    assert credentials_path() == tmp_path / "local" / "ffinfo-cli" / "credentials.age"


@pytest.mark.skipif(sys.platform == "win32", reason="XDG 是 POSIX 的约定")
def test_paths_fall_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert identity_path() == Path.home() / ".config" / "ffinfo-cli" / "age-key.txt"
    assert credentials_path() == (
        Path.home() / ".local" / "share" / "ffinfo-cli" / "credentials.age"
    )
