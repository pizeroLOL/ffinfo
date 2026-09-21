"""``ffinfo-cli`` 的失败契约：分档退出码 + 机器可读的错误 JSON。

成功路径的测试在各自的 ``test_*`` 里；这里只钉"失败时 agent 能看见什么"。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

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
from ffinfo_cli.cli import app, error_payload

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
        ["list", "--limit", "-1"],
        ["list", "--data-type", "nope"],
        ["list", "--since", "上周三"],
        ["sync", "--page-size", "0"],
        ["sync", "--page-size", "101"],
        ["sync", "--collection", "forms"],
    ],
)
def test_usage_errors_are_exit_2_and_json(argv: list[str]) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "usage"


def test_missing_credentials_is_configuration_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没登录 → 退出码 3（configuration），而不是含混的 1。

    ``list`` 默认的 history 现在能在没有凭据时降级（只有明文 firefox 数据也看得到），
    所以用**云端独有**的 bookmarks 来守这条契约。
    """
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))

    result = runner.invoke(app, ["list", "--data-type", "bookmarks"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "configuration"
    assert "私钥" in error["message"] or "凭据" in error["message"]


def test_convergence_warning_reaches_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """老库收敛不是悄悄干的：命令行那层把它接到 stderr（这不是失败，退出码照旧 0）。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
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

    result = runner.invoke(app, ["profiles"])

    assert result.exit_code == 0
    assert "1 条重复记录" in result.stderr


def test_login_without_oldsync_keys_is_auth_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    """登录成功、但 keys_jwe 里没有 oldsync scope —— 拿不到密钥也是认证失败。

    这一步曾经漏在契约外面：``sync_key_bundle()`` 抛 ``AuthError`` 没人接，
    变成 traceback + 退出码 1，把 README 那张表破掉一格。
    """
    credentials = Credentials(access_token="ACCESS-TOKEN", scope="profile", expires_at=1_000.0)

    def fake_login(**_kwargs: object) -> Credentials:
        return credentials

    monkeypatch.setattr("ffinfo_cli.cli.login_sync", fake_login)

    result = runner.invoke(app, ["login"])

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
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "usage"


def test_import_from_firefox_with_a_bad_profile_is_configuration_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--profile 配 --from-firefox 是合法的；目录不对是配置问题（退出码 3），不是用法错误。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))

    result = runner.invoke(app, ["import", "--from-firefox", "--profile", str(tmp_path / "nope")])

    assert result.exit_code == 3
    assert json.loads(result.stderr)["error"]["code"] == "configuration"
