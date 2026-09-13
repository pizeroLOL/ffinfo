# ffsync

纯 Python 的 Firefox Sync 客户端库。

**官方那个 Python 客户端（`mozilla-services/syncclient`）2019 年就归档了，至今没有替代品。**

## 职责

- OAuth 2.0 + PKCE + `keys_jwk` 认证流程
- 密钥派生链：scoped key(64B) → `KeyBundle(enc32+mac32)` → 解密 `crypto/keys` → collection 密钥
- Sync 存储协议：分页、增量、backoff
- 记录解密

## 设计约束

> **库不持有任何默认路径。** 所有 I/O 位置由调用者注入。

```python
client = SyncClient(
    credentials_path=...,
    keys_path=...,
    cache_dir=...,
)
```

## 许可

MPL-2.0
