"""age 加密的凭据存储（02 号 ticket）。

**库不知道任何默认路径** —— 私钥与凭据的落点全部由调用者注入（``docs/design.md`` §2.5），
默认位置只在 CLI 层决定。

用 pyrage 而不是外部 ``age`` 二进制：不要求用户预装东西（ticket 02 的技术选型）。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final, Self

import pyrage

from ffinfo.errors import ConfigurationError, DecryptionError

__all__ = ["AgeIdentity", "CredentialStore"]

_IS_WINDOWS: Final = os.name == "nt"
_IDENTITY_MODE: Final = 0o600


class AgeIdentity:
    """age 私钥，外加"文件权限"这条安全纪律。

    落盘一律 0600；读盘时**只接受 0600** —— 权限宽了就拒绝启动，不做"默默不安全"的事。

    Windows 没有 POSIX 权限位（文件保护走 ACL），那边跳过这项检查：
    报一个用户永远修不好的错没有意义。macOS / Linux 照常。
    """

    __slots__ = ("_identity",)

    def __init__(self, identity: pyrage.x25519.Identity) -> None:
        """包装一个 pyrage 私钥；一般走 :meth:`generate` 或 :meth:`from_file`。"""
        self._identity = identity

    def __repr__(self) -> str:
        """刻意不打印私钥。"""
        return "AgeIdentity(<已隐藏>)"

    # ── 构造 ──────────────────────────────────────────────────────────────

    @classmethod
    def generate(cls) -> Self:
        """现场生成一对新密钥。"""
        return cls(pyrage.x25519.Identity.generate())

    @classmethod
    def from_file(cls, path: Path) -> Self:
        """从私钥文件读取；**权限不是 0600 就直接失败**。"""
        _require_private_permissions(path)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"读不了私钥文件 {path}：{exc.strerror}"
            raise ConfigurationError(msg) from exc
        try:
            identity = pyrage.x25519.Identity.from_str(content.strip())
        except (pyrage.IdentityError, ValueError) as exc:
            msg = f"{path} 不是合法的 age 私钥（应形如 AGE-SECRET-KEY-1…）"
            raise ConfigurationError(msg) from exc
        return cls(identity)

    # ── 落盘 ──────────────────────────────────────────────────────────────

    def to_file(self, path: Path) -> None:
        """写到调用者指定的路径。POSIX 上权限 0600；Windows 上走 ACL。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _IDENTITY_MODE)
        except OSError as exc:
            msg = f"写不了私钥文件 {path}：{exc.strerror}"
            raise ConfigurationError(msg) from exc
        try:
            os.write(fd, str(self._identity).encode("utf-8"))
            if hasattr(os, "fchmod"):
                # os.open 的 mode 会被 umask 削，显式再设一次。
                # Windows 没有 fchmod，那边不做这一步。
                os.fchmod(fd, _IDENTITY_MODE)
        except OSError as exc:
            msg = f"写不了私钥文件 {path}：{exc.strerror}"
            raise ConfigurationError(msg) from exc
        finally:
            os.close(fd)

    # ── 加解密 ────────────────────────────────────────────────────────────

    def recipient(self) -> pyrage.x25519.Recipient:
        """对应的公钥 —— 给别人拿去加密用。"""
        return self._identity.to_public()

    def encrypt(self, cleartext: bytes) -> bytes:
        """用自己的公钥加密（自己加密给自己）。"""
        try:
            return pyrage.encrypt(cleartext, [self._identity.to_public()])
        except pyrage.EncryptError as exc:
            msg = "age 加密失败"
            raise ConfigurationError(msg) from exc

    def decrypt(self, ciphertext: bytes) -> bytes:
        """用自己的私钥解密。"""
        try:
            return pyrage.decrypt(ciphertext, [self._identity])
        except (pyrage.DecryptError, ValueError) as exc:
            msg = "age 解密失败：密文被篡改，或不是用这个私钥加密的"
            raise DecryptionError(msg) from exc


class CredentialStore:
    """把凭据（一段文本）用 age 加密存到调用者指定的路径。"""

    __slots__ = ("_identity", "_path")

    def __init__(self, *, identity: AgeIdentity, path: Path) -> None:
        """两个位置都由调用者给 —— 库不认识任何默认路径。"""
        self._identity = identity
        self._path = path

    def __repr__(self) -> str:
        """只打印路径，不打印凭据。"""
        return f"CredentialStore(path={self._path!r})"

    @property
    def path(self) -> Path:
        """凭据文件的落点。"""
        return self._path

    def save(self, credentials: str) -> None:
        """加密后落盘（覆盖已有内容）。"""
        ciphertext = self._identity.encrypt(credentials.encode("utf-8"))
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_bytes(ciphertext)
        except OSError as exc:
            msg = f"写不了凭据文件 {self._path}：{exc.strerror}"
            raise ConfigurationError(msg) from exc

    def load(self) -> str:
        """读盘并解密。"""
        try:
            ciphertext = self._path.read_bytes()
        except FileNotFoundError as exc:
            msg = f"凭据文件不存在：{self._path}"
            raise ConfigurationError(msg) from exc
        except OSError as exc:
            msg = f"读不了凭据文件 {self._path}：{exc.strerror}"
            raise ConfigurationError(msg) from exc
        return self._identity.decrypt(ciphertext).decode("utf-8")


def _require_private_permissions(path: Path) -> None:
    """只接受 0600；别的值一律带着"怎么修"一起报错。

    Windows 上直接放行 —— 见 :class:`AgeIdentity` 的说明。
    """
    try:
        stat_result = path.stat()
    except FileNotFoundError as exc:
        msg = f"私钥文件不存在：{path}"
        raise ConfigurationError(msg) from exc
    except OSError as exc:
        msg = f"读不了私钥文件 {path}：{exc.strerror}"
        raise ConfigurationError(msg) from exc

    if _IS_WINDOWS:
        return

    mode = stat.S_IMODE(stat_result.st_mode)
    if mode != _IDENTITY_MODE:
        msg = (
            f"私钥文件 {path} 的权限是 {mode:04o}，必须是 600 —— "
            f"否则同机器上的其他用户可能读到它。修：chmod 600 {path}"
        )
        raise ConfigurationError(msg)
