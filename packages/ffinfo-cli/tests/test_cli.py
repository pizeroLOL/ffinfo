"""``ffinfo-cli`` 的失败契约：分档退出码 + 机器可读的错误 JSON。

成功路径的测试在各自的 ``test_*`` 里；这里只钉"失败时 agent 能看见什么"。
"""

from __future__ import annotations

import json
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
    """没登录 → 退出码 3（configuration），而不是含混的 1。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "configuration"
    assert "私钥" in error["message"] or "凭据" in error["message"]
