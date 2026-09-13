"""ffsync 的测试向量 —— 全部有出处，**不由被测代码自己产生**。

三条向量：

* :data:`AES` —— 官方 AES-256-CBC + HMAC-SHA256 记录向量，明文本身是一条历史记录。
* :data:`SCOPED_KEY` —— 官方 scoped key 向量（OAuth 的产物，64 字节 = 同步 KeyBundle）。
* :data:`CRYPTO_KEYS` —— 本地生成的 ``crypto/keys`` 向量（上游没有现成的，生成方式见下）。

任何"看起来对"的期望值都不许出现在这里。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 向量 1：AES-256-CBC + HMAC-SHA256 记录加解密 ────────────────────────────
# 出处：application-services/components/sync15/src/key_bundle.rs
#       （mod test 的常量 + test_decrypt / test_encrypt）
# 明文本身是一条**历史记录**，正好覆盖 01 的「解密一条历史记录」。


@dataclass(frozen=True)
class AesVector:
    """一条官方加密记录：密钥、IV、密文、HMAC，以及它对应的明文。"""

    enc_key_b64: str
    hmac_key_b64: str
    iv_b64: str
    hmac_hex: str
    ciphertext_b64: str
    cleartext: str


AES = AesVector(
    enc_key_b64="9K/wLdXdw+nrTtXo4ZpECyHFNr4d7aYHqeg3KW9+m6Q=",
    hmac_key_b64="MMntEfutgLTc8FlTLQFms8/xMPmCldqPlq/QQXEjx70=",
    iv_b64="GX8L37AAb2FZJMzIoXlX8w==",
    hmac_hex="b1e6c18ac30deb70236bc0d65a46f7a4dce3b8b0e02cf92182b914e3afa5eebc",
    ciphertext_b64=(
        "NMsdnRulLwQsVcwxKW9XwaUe7ouJk5Wn80QhbD80l0HEcZGCynh45qIbeYBik0lgcHbK"
        "mlIxTJNwU+OeqipN+/j7MqhjKOGIlvbpiPQQLC6/ffF2vbzL0nzMUuSyvaQzyGGkSYM2"
        "xUFt06aNivoQTvU2GgGmUK6MvadoY38hhW2LCMkoZcNfgCqJ26lO1O0sEO6zHsk3IVz6"
        "vsKiJ2Hq6VCo7hu123wNegmujHWQSGyf8JeudZjKzfi0OFRRvvm4QAKyBWf0MgrW1F8S"
        "FDnVfkq8amCB7NhdwhgLWbN+21NitNwWYknoEWe1m6hmGZDgDT32uxzWxCV8QqqrpH/Z"
        "ggViEr9uMgoy4lYaWqP7G5WKvvechc62aqnsNEYhH26A5QgzmlNyvB+KPFvPsYzxDnSC"
        "jOoRSLx7GG86wT59QZw="
    ),
    cleartext=(
        '{"id":"5qRsgXWRJZXr",'
        '"histUri":"file:///Users/jason/Library/Application%20Support/Firefox/Profiles/'
        'ksgd7wpk.LocalSyncServer/weave/logs/",'
        '"title":"Index of file:///Users/jason/Library/Application Support/Firefox/Profiles/'
        'ksgd7wpk.LocalSyncServer/weave/logs/",'
        '"visits":[{"date":1319149012372425,"type":1}]}'
    ),
)


# ── 向量 2：scoped key（OAuth 的产物，64 字节 = 同步 KeyBundle） ─────────────
# 出处：application-services/components/fxa-client/src/internal/scoped_keys.rs
#       test_flow —— 用固定 P-256 私钥解开真实 keys_jwe 后得到的 keys JSON。
# 切分期望值 = 该 k 的 base64url 解码结果，独立算出来后写死在这里。


@dataclass(frozen=True)
class ScopedKeyVector:
    """``keys_jwe`` 解密后的 JSON，以及其中 kSync 的期望切分结果。"""

    payload_json: str
    ksync_hex: str
    enc_key_hex: str
    hmac_key_hex: str
    scope: str


SCOPED_KEY = ScopedKeyVector(
    payload_json=(
        '{"https://identity.mozilla.com/apps/oldsync":{'
        '"kty":"oct","scope":"https://identity.mozilla.com/apps/oldsync",'
        '"k":"8ek1VNk4sjrNP0DhGC4crzQtwmpoR64zHuFMHb4Tw-exR70Z2SSIfMSrJDTLEZid9lD05-hbA3n2Q4Esjlu1tA",'
        '"kid":"1526414944666-zgTjf5oXmPmBjxwXWFsDWg"}}'
    ),
    ksync_hex=(
        "f1e93554d938b23acd3f40e1182e1caf342dc26a6847ae331ee14c1dbe13c3e7"
        "b147bd19d924887cc4ab2434cb11989df650f4e7e85b0379f643812c8e5bb5b4"
    ),
    enc_key_hex="f1e93554d938b23acd3f40e1182e1caf342dc26a6847ae331ee14c1dbe13c3e7",
    hmac_key_hex="b147bd19d924887cc4ab2434cb11989df650f4e7e85b0379f643812c8e5bb5b4",
    scope="https://identity.mozilla.com/apps/oldsync",
)


# ── 向量 3：crypto/keys 记录 ────────────────────────────────────────────────
# ⚠️ 上游**没有**现成的 crypto/keys 测试向量，所以这条是本地生成的：
#   · 明文结构 = `sync15/src/record_types.rs` 的 CryptoKeysRecord +
#     `syncstorage-rs/docs/src/sync-client/global-storage-v5.md` §crypto/keys
#   · 根密钥与 IV = 向量 1 的官方密钥材料（key_bundle.rs）
#   · 密文 / HMAC = 用 pyca/cryptography 独立实现生成（不经过 ffsync）
#   · default / history / bookmarks 的 bulk key = sha256("ffinfo vector: ...")，纯占位


@dataclass(frozen=True)
class CryptoKeysVector:
    """一条 ``crypto/keys`` 记录的加密形态，以及解开后各 collection 密钥的期望值。"""

    enc_key_b64: str
    hmac_key_b64: str
    iv_b64: str
    hmac_hex: str
    ciphertext_b64: str
    cleartext: str
    default_enc_hex: str
    default_mac_hex: str
    history_enc_hex: str
    history_mac_hex: str
    bookmarks_enc_hex: str
    bookmarks_mac_hex: str


CRYPTO_KEYS = CryptoKeysVector(
    enc_key_b64=AES.enc_key_b64,
    hmac_key_b64=AES.hmac_key_b64,
    iv_b64=AES.iv_b64,
    hmac_hex="5ae631ce2705f9a2cd6fbd82e1b0e64f2c9bc147a20bccbfc223ac85cdb2b7cf",
    ciphertext_b64=(
        "91EgeFkfopYrkeJj1KQmfe+KkKIHQfEjWpCF+C4yQGmGXeuZmA+jOlRvzQtIBK29ySl4VNl5DpbgHX8ZG7vl"
        "JQx0dcHIL2uY9hBGKYs0VIoNjzMBbgTrQbMJiO2Q3w52819ci+rOdQoEH82+GsZZLr2cMJBJVZdJsK1osIKn"
        "by8gY4ISFd/6gyZJMOYD57XLdedmN7CeuLdfyd0O9vDbpwFr2hU6Z8z+iRTYEPbsjdVEo5uymqmSaC30kbyP"
        "m4/dNMtYbmA6vurgRwWk/6NmwqYzjvBRy3loVC88dH5xcuNBeaOxxtGMlEyFw6x3bV1BVFCDRdd65TynwgFh"
        "n939B92DvvyV9VRS/Ij7jf8Q/t/QjjulHHWv4Ykz0vgxbvyblYSmu+to38fZ/qdsCww/lyMXbxf6uD0wfMK0"
        "0I9O1jO9QDcq2jiP4wdxe05y+zgwa1PWyOdv+reM7zLX6XXgaHyNnL0PawuGSvREZBh3A64VjKvdSTpuL+aO"
        "l/XdlY7Q"
    ),
    cleartext=(
        '{"id":"keys","collection":"crypto",'
        '"default":["OjgPcB0MgSwdfGk3km9TNuj2z3KvSznm+kW+pwSkLo0=",'
        '"cfln9exAAtq4E8muv/FuIF8Ousice9Hjg9GJA316pyU="],'
        '"collections":{'
        '"history":["rkeQkNvpkLNIrs45Nmov8DxQSMVqngsWy33wbAT7RgA=",'
        '"5dMVpk6Nlk2WF4XJCSFFAE7x621+vra2y3p3Oz3egFw="],'
        '"bookmarks":["bvB00Nk5PxRqkmJGmLL+Gk2SGTWW4A49nN4365fAw5U=",'
        '"PagkjdCqLnGgeRDip+O6OlyuK8Trk4sNrT578fLdb9s="]}}'
    ),
    default_enc_hex="3a380f701d0c812c1d7c6937926f5336e8f6cf72af4b39e6fa45bea704a42e8d",
    default_mac_hex="71f967f5ec4002dab813c9aebff16e205f0ebac89c7bd1e383d189037d7aa725",
    history_enc_hex="ae479090dbe990b348aece39366a2ff03c5048c56a9e0b16cb7df06c04fb4600",
    history_mac_hex="e5d315a64e8d964d961785c9092145004ef1eb6d7ebeb6b6cb7a773b3dde805c",
    bookmarks_enc_hex="6ef074d0d9393f146a92624698b2fe1a4d92193596e00e3d9cde37eb97c0c395",
    bookmarks_mac_hex="3da8248dd0aa2e71a07910e2a7e3ba3a5cae2bc4eb938b0dad3e7bf1f2dd6fdb",
)
