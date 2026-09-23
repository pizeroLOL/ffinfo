"""``ffinfo-cli`` 的失败契约：分档退出码 + 机器可读的错误 JSON。

成功路径的测试在各自的 ``test_*`` 里；这里只钉"失败时 agent 能看见什么"。
"""

from __future__ import annotations

import enum
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import pytest
import typer
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
from ffinfo_cli.failures import (
    EXIT_CODES,
    CliTyper,
    error_payload,
    machine_from_argv,
)
from ffinfo_cli.list.bookmarks import BookmarksReport
from ffinfo_cli.list.tabs import TabsReport
from ffinfo_cli.sync import CollectedSync, SyncReport
from support import host

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


def test_readme_exit_code_table_matches_failures_module() -> None:
    """README 退出码表与 ``failures`` 同源 —— 漏一行 / 改错码就红。

    表是给 agent 的契约；代码里的 ``EXIT_CODES`` + ``fail_usage`` / ``fail_abort``
    兜底才是真源。两边 ``(退出码, code)`` 集合必须一致（``0`` 成功行无 code，排除）。
    """
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[3] / "README.md").read_text(encoding="utf-8")
    from_table = {
        (int(m.group(1)), m.group(2))
        for m in re.finditer(r"^\| (\d+) \| `(\w+)` \|", readme, re.MULTILINE)
    }
    from_code = {(code, name) for _, code, name in EXIT_CODES}
    from_code |= {(1, "error"), (2, "usage"), (130, "aborted")}

    assert from_table == from_code


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


@pytest.mark.parametrize(
    "argv",
    [
        ["nope"],  # 未知子命令
        ["list", "nope"],  # 子命令组下的未知子命令
        ["sync", "--nope"],  # 未知选项
        ["list", "tabs", "--nope"],  # 子命令下的未知选项
        ["export"],  # 缺参数
    ],
)
def test_parse_errors_are_exit_2_and_human_by_default(argv: list[str]) -> None:
    """Click/Typer **自己**解析阶段的错误也走契约：stderr 一行 ``错误：``、退出 2。"""
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("错误：")


@pytest.mark.parametrize(
    "argv",
    [
        ["nope"],
        ["list", "nope"],
        ["sync", "--nope"],
        ["list", "tabs", "--nope"],
        ["export"],
    ],
)
def test_parse_errors_are_exit_2_and_json_with_j(argv: list[str]) -> None:
    """``-j`` 写在子命令之前时，解析失败也走同一份错误 JSON。"""
    result = runner.invoke(app, ["-j", *argv])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "usage"


def test_parse_error_mode_scans_argv_even_after_the_subcommand() -> None:
    """``-j`` 写在子命令之后也认 —— 扫的是原始 argv，不靠只管子命令之前的 callback。"""
    result = runner.invoke(app, ["sync", "-j", "--nope"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "usage"


def test_parse_error_mode_does_not_leak_between_invocations() -> None:
    """一次 ``-j`` 不能把下一次解析失败也染成 JSON —— 每次进 ``main`` 都重新扫 argv。"""
    first = runner.invoke(app, ["-j", "sync", "--nope"])
    second = runner.invoke(app, ["sync", "--nope"])

    assert json.loads(first.stderr)["error"]["code"] == "usage"
    assert second.stderr.startswith("错误：")


@pytest.mark.parametrize(
    "argv",
    [["-j", "--help"], ["-j", "-h"], ["-j", "--version"], ["-j", "-V"]],
)
def test_eager_exits_ignore_machine_mode(argv: list[str]) -> None:
    """``--help`` / ``--version`` 抛的是 ``Exit(0)``，不走 UsageError —— stdout 永远人读。"""
    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.stderr
    assert result.stdout
    assert not result.stdout.lstrip().startswith("{")


def test_completion_ignores_machine_mode() -> None:
    """补全命令同样走 ``Exit`` —— 带 ``-j`` 也不变成 JSON。

    依赖 ``conftest`` 里钉的 ``_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION``：
    否则 ``--show-completion`` 是 bool flag，argv 的 ``bash`` 被丢掉、改走
    shellingham 进程树探测 —— pre-push 下父进程不是 shell 会假红。
    """
    result = runner.invoke(app, ["-j", "--show-completion", "bash"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("_ffinfo_cli_completion")
    assert not result.stdout.lstrip().startswith("{")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-j", "sync"], True),
        (["sync", "--json"], True),
        (["sync", "--nope"], False),
        ([], False),
    ],
)
def test_machine_from_argv_scans_the_json_flag(argv: list[str], expected: bool) -> None:
    """解析失败路径的模式判定：只看 argv 里有没有 ``-j`` / ``--json``。"""
    assert machine_from_argv(argv) is expected


class _Color(enum.Enum):
    red = "red"
    blue = "blue"


def test_enum_parse_error_follows_the_contract() -> None:
    """enum 不合法也是解析阶段错误 —— 用最小 app 钉住这条通用路径。"""
    small = CliTyper(name="small")

    @small.callback()
    def _root() -> None:
        """root callback —— 让 ``small`` 生成 Group（才会用上自定义的 group class）。"""

    @small.command()
    def pick(color: Annotated[_Color, typer.Option("--color")]) -> None:
        typer.echo(color.value)

    result = runner.invoke(small, ["-j", "pick", "--color", "green"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "usage"


def test_data_type_option_is_rejected() -> None:
    """``--data-type`` 硬删 —— 老用法现在就是用法错误（退出码 2）。"""
    result = runner.invoke(app, ["list", "--data-type", "history"])

    assert result.exit_code == 2


@pytest.mark.parametrize("option", ["--collection", "-c"])
def test_sync_collection_option_is_rejected(option: str) -> None:
    """``sync`` 永远拉白名单三件套 —— ``--collection / -c`` 硬删（用法错误、退出 2）。

    未知选项是**解析阶段**错误，同样走错误 JSON 契约（见本文件上面的解析错误用例）。
    """
    result = runner.invoke(app, ["-j", "sync", option, "history"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "usage"


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


def _local_paths_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))


def test_corrupt_database_is_configuration_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地库损坏 → ``ConfigurationError`` → exit 3 + stderr 错误 JSON。

    曾经 ``sqlite3.DatabaseError`` 不是 ``FfinfoError``、逃出 ``guard``：
    exit 1、stderr 空，``-j`` 拿不到任何错误 JSON。现在与 places/portable
    同款收敛进失败契约。
    """
    from ffinfo_cli.paths import database_path

    _local_paths_env(tmp_path, monkeypatch)
    database = database_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"this is not a database........")

    result = runner.invoke(app, ["-j", "list", "history"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "configuration"
    assert "读不了" in error["message"]


def test_corrupt_database_human_mode_says_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """人读模式同一场景：stderr 一行 ``错误：``、stdout 空、exit 3。"""
    from ffinfo_cli.paths import database_path

    _local_paths_env(tmp_path, monkeypatch)
    database = database_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"this is not a database........")

    result = runner.invoke(app, ["list", "history"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr.startswith("错误：")


def test_login_abort_is_failure_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """登录时 Ctrl-C → ``Abort`` 进失败契约 —— exit **130**、stderr 错误 JSON。

    退出码 130 取 POSIX ``128+SIGINT``，也与 Typer 自己对 ``KeyboardInterrupt``
    的 ``Exit(130)`` 同一口径；``error.code`` 为 ``aborted``（README 表新增一档）。
    ``click.Abort`` 与 ``typer.Abort`` 互不为子类 —— 这里钉前者（``typer.prompt``
    抛的后者由 guard 同一 except 兜住）。
    """
    import click

    def abort_login(**_kwargs: object) -> Credentials:
        raise click.Abort()

    monkeypatch.setattr("ffinfo_cli.commands.login.login_sync", abort_login)

    result = runner.invoke(app, ["-j", "login"])

    assert result.exit_code == 130
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "aborted"
    assert "中断" in error["message"]
    assert not isinstance(result.exception, click.Abort)


def test_login_abort_is_human_error_line_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人读模式同一场景：stderr 一行 ``错误：…``，stdout 无半截成功文案。"""
    import click

    def abort_login(**_kwargs: object) -> Credentials:
        raise click.Abort()

    monkeypatch.setattr("ffinfo_cli.commands.login.login_sync", abort_login)

    result = runner.invoke(app, ["login"])

    assert result.exit_code == 130
    assert result.stdout == ""
    assert result.stderr.startswith("错误：")
    assert "登录成功" not in result.stdout


def test_login_typer_abort_also_enters_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """``typer.prompt`` 抛的是 ``typer.Abort``（与 ``click.Abort`` 不同类）—— 同档。"""
    import typer as typer_mod

    def abort_login(**_kwargs: object) -> Credentials:
        raise typer_mod.Abort()

    monkeypatch.setattr("ffinfo_cli.commands.login.login_sync", abort_login)

    result = runner.invoke(app, ["-j", "login"])

    assert result.exit_code == 130
    assert json.loads(result.stderr)["error"]["code"] == "aborted"


def test_j_list_without_subcommand_is_usage_error_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子命令组缺子命令与根级缺命令同一形态：exit 2、stdout 空、stderr 错误。

    ``-j`` 时是一行 usage JSON；人读时 stderr 以 ``错误：`` 开头。
    根级 ``-j``（缺命令）对照不回归。
    """
    _local_paths_env(tmp_path, monkeypatch)

    sub = runner.invoke(app, ["-j", "list"])
    human = runner.invoke(app, ["list"])
    root = runner.invoke(app, ["-j"])

    assert sub.exit_code == 2
    assert sub.stdout == ""
    assert json.loads(sub.stderr)["error"]["code"] == "usage"

    assert human.exit_code == 2
    assert human.stdout == ""
    assert human.stderr.startswith("错误：")

    assert root.exit_code == 2
    assert json.loads(root.stderr)["error"]["code"] == "usage"


def _fake_successful_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ``login_sync`` 换成返回 oldsync 密钥的替身 —— 不联网、不落盘。"""
    import base64

    from ffinfo.keys import OLD_SYNC_SCOPE, ScopedKey

    k_sync = base64.b64encode(bytes(range(64))).decode("ascii")
    credentials = Credentials(
        access_token="ACCESS-TOKEN",
        scope=OLD_SYNC_SCOPE,
        expires_at=9_999_999_999.0,
        scoped_keys={
            OLD_SYNC_SCOPE: ScopedKey(kty="oct", scope=OLD_SYNC_SCOPE, k=k_sync, kid="k1")
        },
    )

    def fake_login(**_kwargs: object) -> Credentials:
        return credentials

    monkeypatch.setattr("ffinfo_cli.commands.login.login_sync", fake_login)


def test_login_success_json_with_j(monkeypatch: pytest.MonkeyPatch) -> None:
    """``-j login`` 成功经 ``render`` 出纯 JSON —— exit 0、可 ``json.loads``。

    曾经这里断言中文散文（表征「``render`` 不是唯一 seam」）：agent 按 README
    「``-j`` 输出纯 JSON」parse 会拿到垃圾，且退出码 0 无从判错。
    """
    _fake_successful_login(monkeypatch)

    result = runner.invoke(app, ["-j", "login"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["format_version"] == 1
    assert payload["status"] == "success"
    assert payload["credentials"].endswith("credentials.age")
    assert payload["encryption_key_bytes"] == 32
    assert payload["hmac_key_bytes"] == 32


def test_login_success_human_mode_is_still_chinese_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人读 login 成功仍是中文散文（含「登录成功」），不是 JSON。"""
    _fake_successful_login(monkeypatch)

    result = runner.invoke(app, ["login"])

    assert result.exit_code == 0
    assert "登录成功" in result.stdout
    assert "凭据已加密存到" in result.stdout
    assert "32 字节" in result.stdout
    assert not result.stdout.lstrip().startswith("{")


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
    """``--show-completion bash`` 吐 bash 补全脚本 —— 认 argv 里的 shell 名，不靠进程树探测。"""
    result = runner.invoke(app, ["--show-completion", "bash"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("_ffinfo_cli_completion")


def shell_completions(*args: str, incomplete: str) -> set[str]:
    """敲到 ``args`` 之后、正在补 ``incomplete`` 时 shell 会给出的候选。

    走 click 的 bash 补全协议：``COMP_WORDS`` 把正在补的那个词也算进去，
    ``COMP_CWORD`` 指到它上面。
    """
    words = ["ffinfo-cli", *args, incomplete]
    result = runner.invoke(
        app,
        [],
        env={
            "_FFINFO_CLI_COMPLETE": "complete_bash",
            "COMP_WORDS": " ".join(words),
            "COMP_CWORD": str(len(words) - 1),
        },
    )
    assert result.exit_code == 0, result.stderr
    return set(result.stdout.split())


def test_shell_completion_offers_subcommands_and_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """静态补全照常：``list`` 的子命令、``tabs`` 的选项名都出 —— 与数据感知补全并存。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))

    assert {"history", "bookmarks", "tabs"} <= shell_completions("list", incomplete="")
    assert {"--device", "--limit"} <= shell_completions("list", "tabs", incomplete="--")


def test_shell_completion_of_option_values_is_silent_without_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--device <TAB>`` 在没有库时给空候选、退出 0、不污染 stderr。"""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr("ffinfo_cli.commands.transfer._host", lambda: host(tmp_path))

    assert shell_completions("list", "tabs", "--device", incomplete="") == set()
    assert shell_completions("import", "--profile", incomplete="") == set()


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
