"""``ffinfo-cli profiles``：本地状态一览。

不碰真机 —— ``home`` / ``platform`` / ``env`` 全是注入的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ffinfo_cli.cli import app
from ffinfo_cli.profiles import LocalPaths, build_report
from ffinfo_cli.store import open_database
from support import build_places

NOW = 1_789_320_612.0


def paths_for(tmp_path: Path) -> LocalPaths:
    """一套落在临时目录里的"默认位置"。"""
    config = tmp_path / "config" / "ffinfo-cli"
    data = tmp_path / "data" / "ffinfo-cli"
    return LocalPaths(
        config_dir=config,
        data_dir=data,
        identity=config / "age-key.txt",
        credentials=data / "credentials.age",
        database=data / "ffinfo.sqlite",
    )


def make_profile(home: Path) -> Path:
    """造一个"Linux 上的 Firefox profile" —— 目录 + ``places.sqlite``。"""
    profile = home / ".mozilla" / "firefox" / "abcd1234.default-release"
    build_places(profile / "places.sqlite", [])
    return profile


async def test_reports_the_profiles_it_found(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = make_profile(home)

    report = await build_report(
        home=home, platform="linux", env={}, paths=paths_for(tmp_path), clock=lambda: NOW
    )

    assert [item.path for item in report.profiles] == [str(profile)]
    assert report.profiles[0].name == "abcd1234.default-release"


async def test_missing_firefox_says_where_it_looked(tmp_path: Path) -> None:
    """找不到 profile 不是错误 —— 把找过的地方列出来，并说明 firefox 数据怎么来。"""
    report = await build_report(
        home=tmp_path / "home",
        platform="linux",
        env={},
        paths=paths_for(tmp_path),
        clock=lambda: NOW,
    )

    assert report.profiles == []
    assert report.searched_roots
    assert all(not root.exists for root in report.searched_roots)
    assert any("export" in note for note in report.notes)


async def test_reports_credentials_and_database_state(tmp_path: Path) -> None:
    report = await build_report(
        home=tmp_path / "home",
        platform="linux",
        env={},
        paths=paths_for(tmp_path),
        clock=lambda: NOW,
    )

    assert report.identity.exists is False
    assert report.credentials.exists is False
    assert report.database.exists is False
    assert any("login" in note for note in report.notes)
    assert any("sync" in note for note in report.notes)


async def test_collections_come_from_the_cursors(tmp_path: Path) -> None:
    """同步过的话，各 collection 的进度要看得见。"""
    paths = paths_for(tmp_path)
    paths.database.parent.mkdir(parents=True, exist_ok=True)
    store = await open_database(paths.database)
    await store.save_cursor("history", last_modified=1_789_320_600.12, synced_at=NOW, records=4_921)

    report = await build_report(
        home=tmp_path / "home",
        platform="linux",
        env={},
        paths=paths,
        clock=lambda: NOW,
    )

    assert [item.collection for item in report.collections] == ["history"]
    assert report.collections[0].records == 4_921
    assert report.collections[0].synced_at == "2026-09-13T17:30:12+00:00"
    assert not any("sync" in note for note in report.notes)  # 同步过了，别再劝


async def test_json_is_serializable(tmp_path: Path) -> None:
    report = await build_report(
        home=tmp_path / "home",
        platform="linux",
        env={},
        paths=paths_for(tmp_path),
        clock=lambda: NOW,
    )

    payload = json.loads(report.to_json())

    assert payload["format_version"] == 1
    assert payload["generated_at"] == "2026-09-13T17:30:12+00:00"
    assert set(payload) >= {"profiles", "searched_roots", "collections", "notes"}


def test_cli_prints_json_and_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令本身：退出码 0 + stdout 上一份 JSON（默认位置被重定向到临时目录）。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))

    result = CliRunner().invoke(app, ["profiles"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["format_version"] == 1
    assert isinstance(payload["notes"], list)
