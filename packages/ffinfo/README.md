# ffinfo

纯 Python 的 Firefox Sync 客户端库。

**官方那个 Python 客户端（`mozilla-services/syncclient`）2019 年就归档了，至今没有替代品。**

## 职责

- OAuth 2.0 + PKCE + `keys_jwk` 认证流程（`OAuthClient` / `Credentials`）
- 密钥派生链：scoped key(64B) → `KeyBundle`(enc32+mac32) → 解密 `crypto/keys` → collection 密钥
  （`CollectionKeys`）
- Sync 存储协议：分页、增量、backoff（`SyncStorageClient`）
- 记录解密：历史 / 书签 / 标签页
- 凭据的 age 加密落盘（`CredentialStore`）

## 设计约束

> **库不持有任何默认路径。** 所有 I/O 位置（磁盘、HTTP、时钟）由调用者注入，
> 构造函数不设默认值 —— 默认位置只在 CLI 层决定。

## 用法

库只提供零件，编排由调用者做 —— **一份完整、能跑的编排见 `packages/ffinfo-cli`**
（`login.py` 是 OAuth 那一半，`sync.py` 是拉取与落盘那一半）。骨架长这样：

```python
import httpx

from ffinfo import OLD_SYNC_SCOPE, Credentials, OAuthClient, SyncStorageClient
from ffinfo.oauth import FIREFOX_IOS_CLIENT_ID, default_endpoints, firefox_redirect_uri

oauth = OAuthClient(
    client_id=FIREFOX_IOS_CLIENT_ID,
    redirect_uri=firefox_redirect_uri(FIREFOX_IOS_CLIENT_ID),
    http=httpx.AsyncClient(),
    endpoints=default_endpoints(),
)

# 授权：把 request.url 交给用户在浏览器里点，从回调 URL 里抄回 code
request = oauth.start_authorization(scopes=[OLD_SYNC_SCOPE])
tokens = await oauth.exchange_code(code=..., request=request)
credentials = Credentials.from_tokens(tokens, request.key_pair, now=...)

# 拉取：tokenserver 要求带 oldsync scoped key 的 kid（Hawk 签名要用）
storage = SyncStorageClient(
    http=...,
    access_token=credentials.access_token,
    key_id=credentials.scoped_keys[OLD_SYNC_SCOPE].kid,
    token_server_url=...,
)
fetch = await storage.fetch_collection("history")
```

`now`（时钟）由调用者给 —— 库不自己去读时钟，退避逻辑才能被测试向量驱动。
解密侧：`CollectionKeys.from_encrypted_payload(...)` 解出 collection 密钥，
再交给 `decrypt_history` / `parse_bookmarks` / `parse_tabs`。

## 许可

MPL-2.0
