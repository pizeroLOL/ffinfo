"""OAuth oob 授权流程。

全部离线可测：PKCE、授权 URL、回调解析都是纯函数；token 交换用
``httpx.MockTransport`` 打桩，不打真实网络。

PKCE 那条用的是 **RFC 7636 Appendix B 的官方测试向量**，不是自己算一遍。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ffinfo.credentials import AgeIdentity, CredentialStore
from ffinfo.errors import AuthError
from ffinfo.jwe import EphemeralKeyPair
from ffinfo.keys import OLD_SYNC_SCOPE
from ffinfo.oauth import (
    Credentials,
    OAuthClient,
    OAuthEndpoints,
    OAuthTokens,
    PkcePair,
    parse_callback_url,
)
from vectors import JWE

CLIENT_ID = "5882386c6d801776"
REDIRECT_URI = f"https://accounts.firefox.com/oauth/success/{CLIENT_ID}"
SCOPE = "https://identity.mozilla.com/apps/oldsync"
STATE = "b8f3a1c9d7e5"


def _client(handler=None) -> OAuthClient:
    transport = httpx.MockTransport(handler or (lambda _: httpx.Response(500)))
    return OAuthClient(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        http=httpx.AsyncClient(transport=transport),
        endpoints=OAuthEndpoints(
            authorization="https://accounts.firefox.com/authorization",
            token="https://oauth.accounts.firefox.com/v1/token",
        ),
    )


# ── PKCE ──────────────────────────────────────────────────────────────────


def test_pkce_matches_rfc7636_appendix_b() -> None:
    """RFC 7636 Appendix B：verifier → challenge 的官方测试向量。"""
    pair = PkcePair.from_verifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")

    assert pair.challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_verifier_is_within_rfc7636_length() -> None:
    assert 43 <= len(PkcePair.generate().verifier) <= 128


def test_generated_pkce_pairs_are_unique() -> None:
    assert PkcePair.generate().verifier != PkcePair.generate().verifier


# ── 授权 URL ──────────────────────────────────────────────────────────────


def test_authorization_url_carries_everything_mozilla_needs() -> None:
    request = _client().start_authorization(scopes=[SCOPE])
    parsed = urlparse(request.url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://accounts.firefox.com/authorization"
    )
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["scope"] == [SCOPE]
    assert params["state"] == [request.state]
    assert params["response_type"] == ["code"]
    assert params["code_challenge"] == [request.pkce.challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]


def test_authorization_url_carries_a_public_keys_jwk() -> None:
    request = _client().start_authorization(scopes=[SCOPE])
    params = parse_qs(urlparse(request.url).query)

    # keys_jwk 是 base64url 编码的 JWK JSON（Mozilla 的格式要求）
    raw = params["keys_jwk"][0]
    jwk = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert jwk["kty"] == "EC"
    assert jwk["crv"] == "P-256"
    assert "d" not in jwk
    assert jwk == request.key_pair.public_jwk()


def test_each_authorization_request_has_its_own_state_and_key() -> None:
    client = _client()

    first = client.start_authorization(scopes=[SCOPE])
    second = client.start_authorization(scopes=[SCOPE])

    assert first.state != second.state
    assert first.key_pair.public_jwk() != second.key_pair.public_jwk()


# ── 用户粘回来的回调 URL ──────────────────────────────────────────────────


def test_parses_the_pasted_callback_url() -> None:
    url = f"{REDIRECT_URI}?code=abc123&state={STATE}"

    assert parse_callback_url(url, expected_state=STATE) == "abc123"


def test_state_mismatch_is_refused() -> None:
    url = f"{REDIRECT_URI}?code=abc123&state=someone-elses-state"

    with pytest.raises(AuthError) as excinfo:
        parse_callback_url(url, expected_state=STATE)

    assert "state" in str(excinfo.value)


def test_user_denying_access_is_reported_clearly() -> None:
    url = f"{REDIRECT_URI}?error=access_denied&state={STATE}"

    with pytest.raises(AuthError) as excinfo:
        parse_callback_url(url, expected_state=STATE)

    assert "access_denied" in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        REDIRECT_URI,
        f"{REDIRECT_URI}?state={STATE}",
        "not a url at all",
        "",
    ],
)
def test_callback_without_a_code_is_refused(url: str) -> None:
    with pytest.raises(AuthError):
        parse_callback_url(url, expected_state=STATE)


# ── 用授权码换 token ──────────────────────────────────────────────────────


def _token_response(**overrides) -> dict:
    payload = {
        "access_token": "access-token-abc",
        "token_type": "bearer",
        "scope": SCOPE,
        "expires_in": 3600,
        "refresh_token": "refresh-token-xyz",
        "keys_jwe": JWE.jwe,
    }
    payload.update(overrides)
    return payload


async def test_exchange_code_posts_the_rfc6749_parameters() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        return httpx.Response(200, json=_token_response())

    client = _client(handler)
    request = client.start_authorization(scopes=[SCOPE])

    tokens = await client.exchange_code(code="abc123", request=request)

    assert seen["url"] == "https://oauth.accounts.firefox.com/v1/token"
    assert seen["form"] == {
        "client_id": CLIENT_ID,
        "code": "abc123",
        "code_verifier": request.pkce.verifier,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }
    assert tokens.access_token == "access-token-abc"
    assert tokens.refresh_token == "refresh-token-xyz"


def _official_key_pair() -> EphemeralKeyPair:
    """官方向量里那把固定私钥 —— mock 回来的 keys_jwe 是给它加密的。"""
    d = base64.urlsafe_b64decode(JWE.private_key_d_b64 + "=" * (-len(JWE.private_key_d_b64) % 4))
    return EphemeralKeyPair.from_private_bytes(d)


async def test_exchange_code_yields_the_sync_key_bundle() -> None:
    """授权码 → token → keys_jwe → 64 字节 kSync。

    注意 mock 回来的是**官方那条** keys_jwe（配官方向量的固定私钥），
    不是本次请求现场生成的那把临时密钥 —— 「自己生成的公钥 → 真实 Mozilla 加密
    → 自己解开」这一环只能等真实账号才跑得了。
    """
    client = _client(lambda _: httpx.Response(200, json=_token_response()))
    request = client.start_authorization(scopes=[SCOPE])

    tokens = await client.exchange_code(code="abc123", request=request)
    scoped_keys = tokens.scoped_keys(_official_key_pair())

    assert OLD_SYNC_SCOPE in scoped_keys
    assert len(scoped_keys[OLD_SYNC_SCOPE].key_bytes()) == 64


@pytest.mark.parametrize(
    "error", ["invalid_grant", "invalid_client", "incorrect_redirect_uri", "unknown_error"]
)
async def test_oauth_error_responses_are_reported(error: str) -> None:
    client = _client(
        lambda _: httpx.Response(400, json={"error": error, "message": "nope", "errno": 110})
    )
    request = client.start_authorization(scopes=[SCOPE])

    with pytest.raises(AuthError) as excinfo:
        await client.exchange_code(code="stale-code", request=request)

    assert error in str(excinfo.value)


async def test_token_response_without_keys_jwe_is_not_silently_accepted() -> None:
    client = _client(lambda _: httpx.Response(200, json=_token_response(keys_jwe=None)))
    request = client.start_authorization(scopes=[SCOPE])

    tokens = await client.exchange_code(code="abc123", request=request)

    with pytest.raises(AuthError):
        tokens.scoped_keys(request.key_pair)


async def test_network_failure_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = _client(handler)
    request = client.start_authorization(scopes=[SCOPE])

    with pytest.raises(AuthError):
        await client.exchange_code(code="abc123", request=request)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _encrypt_for(public_jwk: dict[str, str], cleartext: str) -> str:
    """照 RFC 7518 §4.6.2 造一条 ECDH-ES + A256GCM 的 JWE —— **测试里的独立实现**。

    故意不复用库里的代码：这一条验的就是"我们发出去的公钥，别人按规范加密，
    我们解得开"，两侧各写各的才算数。
    """
    sender = ec.generate_private_key(ec.SECP256R1())
    numbers = sender.public_key().public_numbers()
    header = {
        "alg": "ECDH-ES",
        "enc": "A256GCM",
        "epk": {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64url(numbers.x.to_bytes(32, "big")),
            "y": _b64url(numbers.y.to_bytes(32, "big")),
        },
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))

    peer = ec.EllipticCurvePublicNumbers(
        int.from_bytes(_unb64url(public_jwk["x"]), "big"),
        int.from_bytes(_unb64url(public_jwk["y"]), "big"),
        ec.SECP256R1(),
    ).public_key()
    shared = sender.exchange(ec.ECDH(), peer)

    algorithm = b"A256GCM"
    other_info = (
        struct.pack(">I", len(algorithm))
        + algorithm
        + struct.pack(">I", 0)  # apu
        + struct.pack(">I", 0)  # apv
        + struct.pack(">I", 256)
    )
    cek = hashlib.sha256(struct.pack(">I", 1) + shared + other_info).digest()

    iv = os.urandom(12)
    sealed = AESGCM(cek).encrypt(iv, cleartext.encode("utf-8"), header_b64.encode("ascii"))
    return ".".join([header_b64, "", _b64url(iv), _b64url(sealed[:-16]), _b64url(sealed[-16:])])


def test_key_pair_survives_the_round_trip_to_mozilla() -> None:
    """授权请求里的公钥必须能解回"发给它"的那条 JWE —— 真做一次往返。"""
    request = _client().start_authorization(scopes=[SCOPE])

    jwe = _encrypt_for(request.key_pair.public_jwk(), '{"scope": "oldsync"}')

    assert request.key_pair.decrypt_jwe(jwe) == '{"scope": "oldsync"}'


# ── 凭据：整理、序列化、落盘 ──────────────────────────────────────────────


def _credentials(now: float = 1_000.0) -> Credentials:
    return Credentials.from_tokens(
        OAuthTokens.model_validate(_token_response()),
        _official_key_pair(),
        now=now,
    )


def test_credentials_expose_the_sync_key_bundle() -> None:
    """03 的终点：从一份凭据里拿到 oldsync 的 64 字节密钥。"""
    bundle = _credentials().sync_key_bundle()

    assert len(bundle.encryption_key) == 32
    assert len(bundle.hmac_key) == 32


def test_credentials_survive_a_json_round_trip() -> None:
    original = _credentials()

    restored = Credentials.from_json(original.to_json())

    assert restored.sync_key_bundle() == original.sync_key_bundle()
    assert restored.access_token == original.access_token


def test_credentials_expiry_uses_the_injected_clock() -> None:
    credentials = _credentials(now=1_000.0)

    assert not credentials.is_expired(now=1_000.0 + 3_599)
    assert credentials.is_expired(now=1_000.0 + 3_600)


def test_credentials_can_be_vaulted_with_age(tmp_path) -> None:
    """「换到的凭据按 02 的方式加密落盘」—— 03 和 02 串起来。"""
    store = CredentialStore(identity=AgeIdentity.generate(), path=tmp_path / "credentials.age")
    original = _credentials()

    store.save(original.to_json())
    restored = Credentials.from_json(store.load())

    assert restored.sync_key_bundle() == original.sync_key_bundle()
    assert b"access-token-abc" not in store.path.read_bytes()


def test_credentials_without_oldsync_scope_are_refused() -> None:
    credentials = Credentials(access_token="x", scoped_keys={})

    with pytest.raises(AuthError):
        credentials.sync_key_bundle()
