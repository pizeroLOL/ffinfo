"""CLI 层：``ffinfo_cli login`` 的整条编排。

（默认路径的断言在 ``test_paths.py``。）

HTTP 用 ``httpx.MockTransport`` 打桩。**keys_jwe 是测试里现场加密的** ——
产品代码只做解密方向（本库严格只读），所以这里手写一份最小的 JWE 加密器，
用来验证「我们发出去的 keys_jwk，真的能解回给它的东西」。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import struct
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ffinfo.credentials import AgeIdentity
from ffinfo.errors import AuthError
from ffinfo_cli.login import run_login
from vectors import SCOPED_KEY

_KEYS_JSON = SCOPED_KEY.payload_json
_AUTH_CODE = "auth-code-from-the-address-bar"


# ── login 的整条编排 ─────────────────────────────────────────────────────


class _FakeReceiver:
    """模拟用户：从授权 URL 里抄下 state，直接伪造一条回调。"""

    def __init__(self) -> None:
        self.authorization_url = ""
        self.keys_jwk: dict[str, str] = {}

    def receive(self, authorization_url: str) -> str:
        self.authorization_url = authorization_url
        query = parse_qs(urlparse(authorization_url).query)
        raw = query["keys_jwk"][0]
        # keys_jwk 是 base64url 编码的 JWK JSON
        self.keys_jwk = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return (
            "https://accounts.firefox.com/oauth/success/x"
            f"?code={_AUTH_CODE}&state={query['state'][0]}"
        )


class _BadStateReceiver(_FakeReceiver):
    """模拟用户粘错了 URL。"""

    def receive(self, authorization_url: str) -> str:
        super().receive(authorization_url)
        return "https://accounts.firefox.com/oauth/success/x?code=x&state=not-the-state"


def _token_handler(receiver: _FakeReceiver) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "access-token-abc",
                "token_type": "bearer",
                "scope": "https://identity.mozilla.com/apps/oldsync",
                "expires_in": 3600,
                "refresh_token": "refresh-token-xyz",
                "keys_jwe": _jwe_for(receiver.keys_jwk, _KEYS_JSON),
            },
        )

    return handler


async def _login(tmp_path: Path, receiver: _FakeReceiver, **overrides) -> object:
    http = httpx.AsyncClient(transport=httpx.MockTransport(_token_handler(receiver)))
    params = {
        "identity_path": tmp_path / "age-key.txt",
        "credentials_path": tmp_path / "credentials.age",
        "receiver": receiver,
        "http": http,
        "now": 1_000.0,
    }
    params.update(overrides)
    return await run_login(**params)


async def test_login_derives_the_sync_key_bundle(tmp_path: Path) -> None:
    """03 的验收路径：授权 URL → 回调 → token → keys_jwe → 64 字节密钥。"""
    credentials = await _login(tmp_path, _FakeReceiver())

    bundle = credentials.sync_key_bundle()

    assert len(bundle.encryption_key) == 32
    assert len(bundle.hmac_key) == 32


@pytest.mark.skipif(os.name != "posix", reason="POSIX 权限位在 Windows 上不存在")
async def test_login_writes_an_identity_with_tight_permissions(tmp_path: Path) -> None:
    await _login(tmp_path, _FakeReceiver())

    assert stat.S_IMODE((tmp_path / "age-key.txt").stat().st_mode) == 0o600


async def test_login_encrypts_the_credentials_on_disk(tmp_path: Path) -> None:
    await _login(tmp_path, _FakeReceiver())

    blob = (tmp_path / "credentials.age").read_bytes()
    assert b"access-token-abc" not in blob
    assert b"refresh-token-xyz" not in blob


async def test_login_round_trips_through_the_vault(tmp_path: Path) -> None:
    credentials = await _login(tmp_path, _FakeReceiver())

    from ffinfo.credentials import CredentialStore
    from ffinfo.oauth import Credentials

    reopened = CredentialStore(
        identity=AgeIdentity.from_file(tmp_path / "age-key.txt"),
        path=tmp_path / "credentials.age",
    )
    restored = Credentials.from_json(reopened.load())

    assert restored.sync_key_bundle() == credentials.sync_key_bundle()


async def test_login_reuses_an_existing_identity(tmp_path: Path) -> None:
    first = await _login(tmp_path, _FakeReceiver())
    identity_file = (tmp_path / "age-key.txt").read_bytes()

    second = await _login(tmp_path, _FakeReceiver())

    assert (tmp_path / "age-key.txt").read_bytes() == identity_file
    assert second.sync_key_bundle() == first.sync_key_bundle()


async def test_login_refuses_a_mismatched_state(tmp_path: Path) -> None:
    with pytest.raises(AuthError):
        await _login(tmp_path, _BadStateReceiver())


# ── 测试用的最小 JWE 加密器（产品代码只做解密方向） ──────────────────────


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _concat_kdf(shared_secret: bytes) -> bytes:
    """独立实现的 Concat KDF（RFC 7518 §4.6.2）—— 故意不复用产品代码。"""
    alg = b"A256GCM"
    other_info = (
        struct.pack(">I", len(alg))
        + alg
        + struct.pack(">I", 0)
        + struct.pack(">I", 0)
        + struct.pack(">I", 256)
    )
    return hashlib.sha256(struct.pack(">I", 1) + shared_secret + other_info).digest()


def _jwe_for(recipient_jwk: dict[str, str], plaintext: str) -> str:
    """ECDH-ES + A256GCM 的最小加密实现，只存在于测试里。"""
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    recipient = ec.EllipticCurvePublicNumbers(
        int.from_bytes(_b64d(recipient_jwk["x"]), "big"),
        int.from_bytes(_b64d(recipient_jwk["y"]), "big"),
        ec.SECP256R1(),
    ).public_key()
    cek = _concat_kdf(ephemeral.exchange(ec.ECDH(), recipient))

    public = ephemeral.public_key().public_numbers()
    header = {
        "alg": "ECDH-ES",
        "enc": "A256GCM",
        "epk": {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64e(public.x.to_bytes(32, "big")),
            "y": _b64e(public.y.to_bytes(32, "big")),
        },
    }
    header_b64 = _b64e(json.dumps(header, separators=(",", ":")).encode())
    iv = os.urandom(12)
    blob = AESGCM(cek).encrypt(iv, plaintext.encode(), header_b64.encode())
    return ".".join([header_b64, "", _b64e(iv), _b64e(blob[:-16]), _b64e(blob[-16:])])
