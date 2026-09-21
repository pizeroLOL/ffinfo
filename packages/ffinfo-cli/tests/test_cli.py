"""``ffinfo-cli`` 的失败契约：分档退出码 + 机器可读的错误 JSON。

成功路径的测试在各自的 ``test_*`` 里；这里只钉"失败时 agent 能看见什么"。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ffinfo.bookmarks import BookmarkNode
from ffinfo.errors import (
    AuthError,
    BackoffError,
    ConfigurationError,
    DecryptionError,
    FfinfoError,
    KeyDerivationError,
    SyncProtocolError,
)
from ffinfo.oauth import Credentials
from ffinfo.storage import FetchProgress
from ffinfo.tabs import ClientTabs, TabEntry
from ffinfo_cli.cli import app
from ffinfo_cli.failures import error_payload
from ffinfo_cli.list.bookmarks import BookmarksReport
from ffinfo_cli.list.tabs import TabsReport
from ffinfo_cli.sync import CollectedSync, SyncReport

runner = CliRunner()


@pytest.mark.parametrize(
    ("exc", "code", "name"),
    [
        (ConfigurationError("先 login"), 3, "configuration"),
        (AuthError("token 过期"), 4, "auth"),
        (SyncProtocolError("响应不对"), 6, "protocol"),
        (DecryptionError("解不开"), 7, "decryption"),
        (KeyDerivationError("派不出"), 8, "key_derivation"),
    ],
)
def test_error_payload_maps_each_class(exc: FfinfoError, code: int, name: str) -> None:
    assert error_payload(exc) == (code, {"error": {"code": name, "message": str(exc)}})


def test_backoff_carries_wait_and_soft() -> None:
    """退避是可重试的"现在别来" —— agent 需要知道等多久、是软是硬。"""
    code, payload = error_payload(BackoffError("等一会", wait_seconds=60.0, soft=True))

    assert code == 5
    assert payload["error"] == {
        "code": "backoff",
        "message": "等一会",
        "wait_seconds": 60.0,
        "soft": True,
    }


def test_unknown_error_falls_back_but_keeps_the_message() -> None:
    """没登记的异常类型落到兜底档 —— 消息绝不丢。"""

    class Weird(FfinfoError):
        pass

    assert error_payload(Weird("没见过")) == (1, {"error": {"code": "error", "message": "没见过"}})


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "history", "--limit", "-1"],
        ["list", "history", "--since", "上周三"],
        ["sync", "--page-size", "0"],
        ["sync", "--page-size", "101"],
    ],
)
def test_usage_errors_are_exit_2_and_human_by_default(argv: list[str]) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert result.stderr.startswith("错误：")


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "history", "--limit", "-1"],
        ["list", "history", "--since", "上周三"],
        ["sync", "--page-size", "0"],
        ["sync", "--page-size", "101"],
    ],
)
def test_usage_errors_are_exit_2_and_json_with_j(argv: list[str]) -> None:
    """错误形态跟随模式 —— ``-j`` 时才是 stderr JSON，退出码两种模式一致。"""
    result = runner.invoke(app, ["-j", *argv])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "usage"


def test_data_type_option_is_rejected() -> None:
    """``--data-type`` 硬删 —— 老用法现在就是用法错误（退出码 2）。"""
    result = runner.invoke(app, ["list", "--data-type", "history"])

    assert result.exit_code == 2


@pytest.mark.parametrize("option", ["--collection", "-c"])
def test_sync_collection_option_is_rejected(option: str) -> None:
    """``sync`` 永远拉白名单三件套 —— ``--collection / -c`` 硬删（用法错误、退出 2）。

    未知选项现在是**解析阶段**错误，走 Click 默认输出；08 会把它接到错误 JSON 契约上。
    """
    result = runner.invoke(app, ["sync", option, "history"])

    assert result.exit_code == 2


def test_per_type_filters_are_not_shared_across_subcommands() -> None:
    """筛选项按类型各给一套 —— 别的子命令不认这些选项（用法错误、退出 2）。"""
    assert runner.invoke(app, ["list", "history", "--path", "书签工具栏"]).exit_code == 2
    assert runner.invoke(app, ["list", "history", "--device", "alpha"]).exit_code == 2
    assert runner.invoke(app, ["list", "tabs", "--path", "x"]).exit_code == 2
    assert runner.invoke(app, ["list", "bookmarks", "--device", "x"]).exit_code == 2
    for subcommand in ("bookmarks", "tabs"):
        for option in ("--since", "--domain", "--search"):
            assert runner.invoke(app, ["list", subcommand, option, "x"]).exit_code == 2


def test_list_subcommands_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """三种数据类型各是一个子命令 —— 空库也照样出报告、退出 0。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))

    result = runner.invoke(app, ["-j", "list", "history"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["data_type"] == "history"


def test_default_output_is_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认人读 —— 空历史也有汇总行，绝不是一份 JSON。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))

    result = runner.invoke(app, ["list", "history"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("共 0 次访问")
    assert not result.stdout.lstrip().startswith("{")


def test_missing_credentials_is_configuration_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没登录 → 退出码 3（configuration），而不是含混的 1。

    ``list`` 默认的 history 现在能在没有凭据时降级（只有明文 firefox 数据也看得到），
    所以用**云端独有**的 bookmarks 来守这条契约。
    """
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))

    result = runner.invoke(app, ["-j", "list", "bookmarks"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "configuration"
    assert "私钥" in error["message"] or "凭据" in error["message"]


def _legacy_duplicate_database(tmp_path: Path) -> None:
    """造一个含重复 sync_records 的老库 —— 打开时应该收敛并警告。"""
    database = tmp_path / "ffinfo-cli" / "ffinfo.sqlite"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE sync_records (
            id INTEGER PRIMARY KEY,
            collection VARCHAR(64) NOT NULL,
            record_id VARCHAR(64) NOT NULL,
            modified DOUBLE PRECISION NOT NULL,
            payload TEXT,
            sortindex BIGINT,
            ttl BIGINT
        );
        CREATE TABLE sync_cursors (
            id INTEGER PRIMARY KEY,
            collection VARCHAR(64) NOT NULL,
            last_modified DOUBLE PRECISION NOT NULL,
            synced_at DOUBLE PRECISION NOT NULL,
            records INTEGER NOT NULL
        );
        INSERT INTO sync_records (collection, record_id, modified, payload) VALUES
            ('history', 'a', 1.0, 'old'),
            ('history', 'a', 2.0, 'new');
        """
    )
    connection.commit()
    connection.close()


@pytest.mark.parametrize("machine", [False, True])
def test_convergence_warning_follows_the_mode(
    machine: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """老库收敛不是悄悄干的：命令行那层把它接到 stderr（不是失败，退出码照旧 0）。

    形态跟随模式 —— 默认 ``警告：…``，``-j`` 时是一行 warning JSON。
    """
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    _legacy_duplicate_database(tmp_path)

    result = runner.invoke(app, ["-j", "profiles"] if machine else ["profiles"])

    assert result.exit_code == 0
    if machine:
        assert "1 条重复记录" in json.loads(result.stderr)["warning"]["message"]
    else:
        assert result.stderr.startswith("警告：")
        assert "1 条重复记录" in result.stderr


def test_login_without_oldsync_keys_is_auth_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    """登录成功、但 keys_jwe 里没有 oldsync scope —— 拿不到密钥也是认证失败。

    这一步曾经漏在契约外面：``sync_key_bundle()`` 抛 ``AuthError`` 没人接，
    变成 traceback + 退出码 1，把 README 那张表破掉一格。
    """
    credentials = Credentials(access_token="ACCESS-TOKEN", scope="profile", expires_at=1_000.0)

    def fake_login(**_kwargs: object) -> Credentials:
        return credentials

    monkeypatch.setattr("ffinfo_cli.commands.login.login_sync", fake_login)

    result = runner.invoke(app, ["-j", "login"])

    assert result.exit_code == 4
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "auth"
    assert "oldsync" in error["message"]


@pytest.mark.parametrize(
    "argv",
    [
        ["import"],  # 两种输入都不给
        ["import", "portable.sqlite", "--from-firefox"],  # 两种都给
        ["import", "portable.sqlite", "--profile", "/tmp/nope"],  # --profile 配错输入
    ],
)
def test_import_input_validation_is_exit_2_and_json(argv: list[str]) -> None:
    """两种输入二选一 —— 给多、给少、选项配错都是用法错误（退出码 2）。"""
    result = runner.invoke(app, ["-j", *argv])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "usage"


def test_import_from_firefox_with_a_bad_profile_is_configuration_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--profile 配 --from-firefox 是合法的；目录不对是配置问题（退出码 3），不是用法错误。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))

    result = runner.invoke(
        app, ["-j", "import", "--from-firefox", "--profile", str(tmp_path / "nope")]
    )

    assert result.exit_code == 3
    assert json.loads(result.stderr)["error"]["code"] == "configuration"


def test_short_help_flag_works() -> None:
    """``-h`` 与 ``--help`` 同效 —— 用户敲短的那一个。"""
    result = runner.invoke(app, ["-h"])

    assert result.exit_code == 0
    assert "login" in result.stdout
    assert "sync" in result.stdout


def test_completion_script_is_available() -> None:
    """``--show-completion`` 能吐出补全脚本 —— bash 补全的入口。"""
    result = runner.invoke(app, ["--show-completion", "bash"])

    assert result.exit_code == 0
    assert result.stdout.strip()


def _fake_sync(*, on_progress: object = None, **_kwargs: object) -> SyncReport:
    """替身 sync_blocking：报一页进度、返回一份最小报告 —— 不联网、不读凭据。"""
    if callable(on_progress):
        on_progress(FetchProgress(collection="history", pages=1, records=3))
    return SyncReport(
        collections=[
            CollectedSync(
                collection="history",
                mode="full",
                records=3,
                inserted=3,
                updated=0,
                deleted=0,
                pages=1,
                tombstones=0,
                server_count=3,
                cursor_before=None,
                cursor_after=1.0,
            )
        ],
        elapsed_seconds=0.1,
        database="/tmp/ffinfo.sqlite",
    )


@pytest.mark.parametrize(
    ("argv", "has_progress"),
    [
        (["-j", "sync"], False),
        (["-j", "sync", "--progress"], True),
        (["sync"], True),
        (["sync", "--no-progress"], False),
    ],
)
def test_progress_follows_the_mode(
    argv: list[str], has_progress: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-j`` 默认关进度（agent 的 stderr 不能混进进度行）；显式 ``--progress`` 仍开。"""
    monkeypatch.setattr("ffinfo_cli.commands.sync.sync_blocking", _fake_sync)

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.stderr
    assert ("拉取 history" in result.stderr) is has_progress


def test_sync_default_output_is_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ffinfo_cli.commands.sync.sync_blocking", _fake_sync)

    result = runner.invoke(app, ["sync", "--no-progress"])

    assert result.exit_code == 0, result.stderr
    assert not result.stdout.lstrip().startswith("{")
    assert "collections: history" in result.stdout


def test_sync_json_output_with_j(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ffinfo_cli.commands.sync.sync_blocking", _fake_sync)

    result = runner.invoke(app, ["-j", "sync", "--no-progress"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["format_version"] == 2
    assert payload["collections"][0]["collection"] == "history"


def _fake_bookmarks(**_kwargs: object) -> BookmarksReport:
    return BookmarksReport(
        data_type="bookmarks",
        generated_at="2026-09-13T17:30:12+00:00",
        filters={},
        records=2,
        skipped=0,
        matched=1,
        returned=1,
        tree=[
            BookmarkNode(
                id="folder",
                type="folder",
                title="工具",
                children=[
                    BookmarkNode(
                        id="bmk", type="bookmark", title="示例", url="https://example.com/"
                    )
                ],
            )
        ],
    )


def _fake_tabs(**_kwargs: object) -> TabsReport:
    return TabsReport(
        data_type="tabs",
        generated_at="2026-09-13T17:30:12+00:00",
        filters={},
        records=1,
        skipped=0,
        matched=1,
        returned=1,
        clients=[
            ClientTabs(
                client_id="dev-1",
                client_name="alpha",
                tabs=(
                    TabEntry(
                        client_id="dev-1",
                        client_name="alpha",
                        title="一",
                        url="https://a.test/",
                        last_used_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
                        icon=None,
                        window_id=None,
                    ),
                ),
            )
        ],
    )


def test_bookmarks_default_output_is_a_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ffinfo_cli.commands.list.list_bookmarks_blocking", _fake_bookmarks)

    result = runner.invoke(app, ["list", "bookmarks"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == ["▸ 工具", "  • 示例  https://example.com/"]


def test_tabs_default_output_groups_by_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ffinfo_cli.commands.list.list_tabs_blocking", _fake_tabs)

    result = runner.invoke(app, ["list", "tabs"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == ["alpha", "  • 一  https://a.test/"]
