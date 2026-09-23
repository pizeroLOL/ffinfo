"""age 凭据存储。

测试打在 :class:`~ffinfo.credentials.AgeIdentity` 与
:class:`~ffinfo.credentials.CredentialStore` 的公开边界上，用真的文件系统（``tmp_path``），
不 mock pyrage —— 这里要验的正是"落盘之后到底安不安全"。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.errors import ConfigurationError, DecryptionError

_POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="POSIX 权限位在 Windows 上不存在（那边不做这一步）"
)

CREDENTIALS = '{"access_token": "secret-token-abc", "refresh_token": "secret-refresh-xyz"}'


def _identity_file(tmp_path: Path) -> Path:
    path = tmp_path / "age-key.txt"
    AgeIdentity.generate().to_file(path)
    return path


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(
        identity=AgeIdentity.from_file(_identity_file(tmp_path)),
        path=tmp_path / "credentials.age",
    )


def test_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.save(CREDENTIALS)

    assert store.load() == CREDENTIALS


def test_plaintext_never_lands_on_disk(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.save(CREDENTIALS)

    on_disk = store.path.read_bytes()
    assert b"secret-token-abc" not in on_disk
    assert b"secret-refresh-xyz" not in on_disk


def test_saved_credentials_survive_a_new_store_instance(tmp_path: Path) -> None:
    identity_path = _identity_file(tmp_path)
    credentials_path = tmp_path / "credentials.age"
    CredentialStore(identity=AgeIdentity.from_file(identity_path), path=credentials_path).save(
        CREDENTIALS
    )

    reopened = CredentialStore(
        identity=AgeIdentity.from_file(identity_path),
        path=credentials_path,
    )

    assert reopened.load() == CREDENTIALS


def test_save_overwrites_previous_credentials(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.save(CREDENTIALS)
    store.save('{"access_token": "second"}')

    assert store.load() == '{"access_token": "second"}'


@_POSIX_ONLY
def test_identity_is_written_0600(tmp_path: Path) -> None:
    path = tmp_path / "age-key.txt"

    AgeIdentity.generate().to_file(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@_POSIX_ONLY
def test_identity_is_written_0600_even_under_a_loose_umask(tmp_path: Path) -> None:
    """umask 会把 os.open 的 mode 削掉 —— fchmod 那一步就是防这个的。"""
    path = tmp_path / "age-key.txt"
    previous = os.umask(0o000)
    try:
        AgeIdentity.generate().to_file(path)
    finally:
        os.umask(previous)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@_POSIX_ONLY
def test_credentials_are_written_0600(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.save(CREDENTIALS)

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


@_POSIX_ONLY
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o777, 0o700])
def test_wide_or_odd_permissions_are_refused(tmp_path: Path, mode: int) -> None:
    path = _identity_file(tmp_path)
    path.chmod(mode)

    with pytest.raises(ConfigurationError) as excinfo:
        AgeIdentity.from_file(path)

    message = str(excinfo.value)
    assert "chmod" in message  # 错误信息要能直接照着修
    assert "600" in message


@_POSIX_ONLY
def test_narrower_permissions_are_accepted(tmp_path: Path) -> None:
    """0400 比 600 **更窄**，没有理由拒绝。"""
    path = _identity_file(tmp_path)
    path.chmod(0o400)

    identity = AgeIdentity.from_file(path)

    assert identity.decrypt(identity.encrypt(b"still works")) == b"still works"


def test_missing_identity_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        AgeIdentity.from_file(tmp_path / "nope.txt")

    assert "nope.txt" in str(excinfo.value)


@pytest.mark.parametrize(
    "content",
    ["", "not an age key", "AGE-SECRET-KEY-1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
)
def test_corrupted_identity_file_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "age-key.txt"
    path.write_text(content)
    path.chmod(0o600)

    with pytest.raises(ConfigurationError):
        AgeIdentity.from_file(path)


def test_missing_credentials_file_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ConfigurationError) as excinfo:
        store.load()

    assert "credentials.age" in str(excinfo.value)


def test_tampered_ciphertext_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(CREDENTIALS)

    blob = bytearray(store.path.read_bytes())
    blob[-1] ^= 0xFF
    store.path.write_bytes(bytes(blob))

    with pytest.raises(DecryptionError):
        store.load()


def test_non_utf8_cleartext_is_refused(tmp_path: Path) -> None:
    """解出来的明文不是 UTF-8 —— 也要收敛成 ConfigurationError，不漏裸 UnicodeDecodeError。"""
    identity_path = _identity_file(tmp_path)
    identity = AgeIdentity.from_file(identity_path)
    path = tmp_path / "credentials.age"
    path.write_bytes(identity.encrypt(b"\xff\xfe\x00 not utf-8 at all"))
    store = CredentialStore(identity=identity, path=path)

    with pytest.raises(ConfigurationError):
        store.load()


def test_ciphertext_from_another_identity_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(CREDENTIALS)

    stranger = CredentialStore(
        identity=AgeIdentity.generate(),
        path=store.path,
    )

    with pytest.raises(DecryptionError):
        stranger.load()


def test_store_requires_explicit_paths() -> None:
    with pytest.raises(TypeError):
        CredentialStore()  # type: ignore[call-arg]


def test_identity_requires_an_explicit_source() -> None:
    with pytest.raises(TypeError):
        AgeIdentity()  # type: ignore[call-arg]


@_POSIX_ONLY
def test_fchmod_tightens_before_write_and_failure_leaves_no_new_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """翻转后：``fchmod`` 在 ``os.write`` **之前**收紧；失败时新密文不进宽权限文件。

    对已存在的 0644 文件：``os.open`` 的 mode 不生效（只对新建生效），
    必须先 ``fchmod`` 收紧到 0600 再写。若 ``fchmod`` 失败，write 根本不执行 ——
    磁盘上不会留下「0644 + 新密钥」的组合。
    """
    identity_path = _identity_file(tmp_path)
    store = CredentialStore(
        identity=AgeIdentity.from_file(identity_path),
        path=tmp_path / "credentials.age",
    )
    store.path.write_bytes(b"OLD")
    store.path.chmod(0o644)

    observed: dict[str, int] = {}
    real_write = os.write

    def spy_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        info = os.fstat(fd)
        observed["mode"] = stat.S_IMODE(info.st_mode)
        observed["size"] = info.st_size
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", spy_write)
    store.save(CREDENTIALS)

    # write 发生时权限已被收紧到 0600，不再是覆盖进来的 0644
    assert observed["mode"] == 0o600
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load() == CREDENTIALS


@_POSIX_ONLY
def test_fchmod_failure_on_wide_file_leaves_no_new_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖 0644 文件时 ``fchmod`` 失败：异常收敛，且磁盘上不出现 0644 + 新内容。"""
    identity_path = _identity_file(tmp_path)
    store = CredentialStore(
        identity=AgeIdentity.from_file(identity_path),
        path=tmp_path / "credentials.age",
    )
    store.path.write_bytes(b"OLD")
    store.path.chmod(0o644)

    observed: dict[str, int] = {}

    def failing_fchmod(fd: int, mode: int) -> None:
        info = os.fstat(fd)
        observed["mode"] = stat.S_IMODE(info.st_mode)
        observed["size"] = info.st_size
        msg = "Permission denied"
        raise OSError(13, msg)

    monkeypatch.setattr(os, "fchmod", failing_fchmod)

    with pytest.raises(ConfigurationError, match="写不了"):
        store.save(CREDENTIALS)

    # fchmod 失败发生在 write 之前：没有新密文落盘（O_TRUNC 已清空旧内容）
    assert observed["size"] == 0
    final_mode = stat.S_IMODE(store.path.stat().st_mode)
    final_content = store.path.read_bytes()
    assert not (final_mode == 0o644 and final_content not in (b"", b"OLD"))
    assert final_content in (b"", b"OLD")


def test_short_writes_are_looped_until_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """短写（partial ``os.write``）不静默截断 —— 循环写完。"""
    store = _store(tmp_path)
    real_write = os.write
    call_count = 0

    def one_byte_at_a_time(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal call_count
        call_count += 1
        return real_write(fd, data[:1])

    monkeypatch.setattr(os, "write", one_byte_at_a_time)

    store.save(CREDENTIALS)

    assert call_count > 1
    assert store.load() == CREDENTIALS
