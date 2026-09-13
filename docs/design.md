# ffinfo 设计归档

> **状态**：设计完成，待开工
> **归档时间**：2026-09-13
> **2026-09-14 补记**：包名调整 —— 库 `ffsync` → `ffinfo`；CLI 的 Python 模块 → `ffinfo_cli`
> （PyPI 名仍是 `ffinfo-cli`，命令仍是 `ffinfo`）。下文已按新名字改写。
> **来源**：6 轮 grilling 拷问 + 3 轮源码调研 + 3 个浅克隆仓库
> **用途**：自包含参考文档 —— 未来任何会话（人或 agent）读这一份就能接手

---

## 0. 一句话

> **一个把 Firefox 浏览数据从 Mozilla 云端和本地 `places.sqlite` 拉到本地 SQLite、输出纯 JSON 给 agent 消费的 CLI 工具；核心协议逻辑独立成一个可复用的 Python 库。**

---

## 1. 项目结构：两个 repo

| | repo | 职责 | 依赖 |
|---|---|---|---|
| **A** | `ffinfo` | 纯 Python Firefox Sync 客户端库：OAuth 认证 · 密钥派生 · 记录解密 · 存储协议 | — |
| **B** | `pizero-firefox-info-cli`（PyPI: `ffinfo-cli`） | CLI · 本地 SQLite · 双源合并 · `export`/`import` | → `ffinfo` |

**为什么这样切**：官方那个 Python Sync 客户端（`mozilla-services/syncclient`）**2019 年就归档了**，至今没有任何替代品。`ffinfo` 单独成库本身就是市面上的稀缺品。

---

## 2. 决策清单（6 轮拷问成果，19 条）

### 2.1 用途与数据

| # | 决策 | 备注 |
|---|---|---|
| 1 | 用途：**个人复盘** | 不是取证、不是清理、不是数据管道 |
| 2 | 数据源：**双源** | ① Firefox Sync（远程）② 本地 `places.sqlite` |
| 3 | 本地源前提：**源机器必须有 Firefox** | 靠 `export`/`import` 搬运到目标机器 |
| 4 | 数据范围：**历史 + 书签 + 标签页**（~~表单~~ ❌ 已证实拿不到，见 §3.8） | 表单移入 TODO |
| 5 | 边界：**严格只读** | 用 `#read` scope，不写回 Mozilla |
| 6 | 输出：**纯数据** | 不做统计、不做 TUI（都进 TODO） |
| 7 | 真正用户：**agent（skill）** | 会再写一个类似 `bf-stats` 的 AstrBot skill 调它 |

### 2.2 认证与密钥

| # | 决策 | 备注 |
|---|---|---|
| 8 | **OAuth 2.0 + PKCE + `keys_jwk`** | ★ 密码永不进 CLI |
| 9 | `client_id`：**暂用 Firefox 的** | 申请自己的进 TODO |
| 10 | 授权码捕获：**抽象接口** | 先 oob（复制地址栏 URL），将来换 `localhost` 回调 |
| 11 | 密钥链：scoped key(64B) → `KeyBundle(enc32+mac32)` → 解密 `crypto/keys` → 各 collection 密钥 | 见 §3.2 |

### 2.3 存储与加密

| # | 决策 | 备注 |
|---|---|---|
| 12 | 本地 **SQLite** · 双源**分表** · 查询时合并 · **保留来源标记** | 不强行合并，保留"这条是哪台机器看的" |
| 13 | **age** 加密 · `age-keygen` 生成专用密钥 | 存 `~/.config/ffinfo/age-key.txt` |
| 14 | **权限校验写进代码** | 不是 `0600` 就拒绝启动 —— 拒绝"默默不安全" |
| 15 | 增量：**显式 `ffinfo sync`** + 超期提示 | 查询保持纯本地、瞬时 |

### 2.4 接口与工程

| # | 决策 | 备注 |
|---|---|---|
| 16 | CLI：**固定过滤器 + JSON** | `--since`/`--domain`/`--search`/`--limit`；**不做 SQL 直通** |
| 17 | `export`/`import` 格式：**SQLite** | 因为要支持增量（schema 版本 + 游标 + 校验） |
| 18 | 工程栈 | **Python 3.14 baseline** · uv · src layout · **pyproject 单文件配置** · ruff · pyright · pytest · pre-commit · 现代 typing · async · httpx · pydantic |
| 19 | 许可：**MPL-2.0** | 与 Mozilla 生态一致 |

### 2.5 库/应用职责分离（新增约束）

> **库不持有任何默认路径，所有 I/O 位置由调用者注入；CLI 决定默认位置。**

```python
# ffinfo —— 无默认值，必须显式传（构造函数不设默认参数）
client = SyncClient(credentials_path=..., keys_path=..., cache_dir=...)

# ffinfo-cli —— 在这里决定默认
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "ffinfo"
DATA_DIR   = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "ffinfo"
```

---

## 3. 已验证的技术事实（全部带出处）

### 3.1 ⚠️ 同步历史的硬上限 —— "非常大"不成立

`research/application-services/components/places/src/history_sync/mod.rs:18-21`

```rust
const MAX_INCOMING_PLACES: usize = 5000;   // 最多 5000 个 URL
const MAX_OUTGOING_PLACES: usize = 5000;
const MAX_VISITS: usize = 20;              // 每个 URL 最多 20 次访问
pub const HISTORY_TTL: u32 = 5_184_000;    // 60 天过期（毫秒）
```

**结论**：同步通道最多给你 5000 个 URL × 每个 20 次访问，且只保留最近 60 天。
**这就是"双数据源"存在的全部理由** —— 要"非常大"的历史，只能读本地 `places.sqlite`。

### 3.2 密钥派生链（核心算法，必须用测试向量验证）

```
① 密码（在 accounts.firefox.com 网页里输入，不进 CLI）
      ↓ PBKDF2 + HKDF
   authPW（发给服务器）+ unwrapBKey（永不上传）
      ↓
   kB = wrapKb ⊕ unwrapBKey          （wrapKb 从服务器取）
      ↓
② OAuth 授权时携带 keys_jwk（CLI 的临时 P-256 ECDH 公钥）
   Mozilla 网页端用该公钥加密 scope 密钥 → JWE (ECDH-ES + A256GCM)
      ↓
   CLI 收到 keys_jwe，用本地私钥解密
      ↓
   scoped key for oldsync = HKDF-SHA256(kB, size=64, context="identity.mozilla.com/picl/v1/oldsync")
      ↓
③ 这 64 字节 = KeyBundle(enc_key: 32B, mac_key: 32B)
   （`sync15/src/key_bundle.rs` 的 `from_ksync_bytes` 直接切）
      ↓
④ 用 root key 解密 `crypto/keys` collection → 得到每个 collection 的 KeyBundle
   （`sync15/src/client/collection_keys.rs` 的 `CollectionKeys::from_encrypted_payload`）
      ↓
⑤ 用 collection key 解密每条记录
```

**关键性质**：`unwrapBKey` 只存在于客户端，Mozilla 服务器上既没有明文数据也没有你的密钥。

### 3.3 OAuth 细节

出处：`research/ecosystem-platform/docs/reference/oauth-details.md` 和 `docs/explanation/scoped-keys.md`

- **PKCE 必须用 `S256`**，不支持 `plain`
- **只读 scope 存在**：`https://identity.mozilla.com/apps/oldsync#read`
- 按 collection 细分的 scope 也存在：`.../oldsync/history`、`.../oldsync/bookmarks`
- 授权端点：`https://accounts.firefox.com/authorization`
- token 端点：`https://oauth.accounts.firefox.com/v1/token`
- OIDC 发现：`https://accounts.firefox.com/.well-known/openid-configuration`

### 3.4 💣 `redirect_uri` 白名单（Q2 的地雷）

出处：`mozilla/fxa` → `packages/fxa-content-server/app/scripts/models/reliers/oauth.js`

```js
* If the relier is requesting keys, we check their redirect URI against
* an explicit allowlist and throw an error if it doesn't match.
...
if (validation[scope].redirectUris.includes(this.get('redirectUri'))) {
    this._wantsScopeThatHasKeys = true;
```

**含义**：请求 scoped keys 时 `redirect_uri` 必须命中显式白名单。

于是"用 Firefox 的 `client_id`"变成：

```
用 Firefox 的 client_id
  → 必须用 Firefox 注册的 redirect_uri
    → https://accounts.firefox.com/oauth/success/<client_id>
      → Mozilla 自己的页面，CLI 收不到回调 ❌
```

**活路**：**让用户从地址栏复制整个 URL**（`code` 就在 URL 里）。这是 RFC 6749 的 oob 模式，Firefox 自己在配对流程里也用 `urn:ietf:wg:oauth:2.0:oob:pair-auth-webchannel`。

### 3.5 数据模型

**历史记录**（`components/places/src/history_sync/record.rs`）：
```rust
struct HistoryRecord {
    id: String,        // GUID
    title: String,
    hist_uri: String,  // URL
    visits: Vec<{ date, timestamp微秒, transition: u8 }>,  // 最多 20 个
}
```

**Sync 集合**（`sync15/src/client/state.rs` 的 `DEFAULT_ENGINES`）：
`passwords` · `clients` · `addons` · `addresses` · `bookmarks` · `creditcards` · `forms` · `history` · `prefs` · `tabs`
另有内部集合：`meta` · `crypto` · `keys`

### 3.6 Sync 协议模式

出处：`research/application-services/docs/design/sync-overview.md`

- 每个 collection 的记录以 `guid` 为主键
- `meta/global` 记录各 collection 的 GUID，用于检测"某设备重置了集合"
- **增量靠时间戳**：只拉上次同步之后变更的记录
- 本地有 `syncChangeCounter` 追踪本地改动
- 有 **backoff** 机制，服务器会要求你退避

### 3.7 ❌ `forms` collection 已死（已定案）

**结论：现代 Firefox Sync 不同步表单历史。`forms` 是遗留的引擎名，没有实现。**

四条证据链：

1. **组件清单里没有它** —— `application-services/components/` 共 26 个组件，有 `autofill`/`logins`/`places`/`tabs`，**没有 `forms`**
2. **全仓只有 4 处提到它，全是"名字"不是"实现"**：
   ```
   state.rs:33                    ("forms", 1),        ← 常量列表
   clients_engine/engine.rs:380   if name == "forms"   ← 测试代码
   clients_engine/engine.rs:457   "args": ["forms"]    ← 测试 JSON 夹具
   clients_engine/engine.rs:510   "args": ["forms"]    ← 测试 JSON 夹具
   ```
3. **决定性注释**（`sync15/src/client/state.rs:22-25`）：
   > *"We include engines that **we don't implement** because they'll be disabled on other clients if we omit them (bug 1479929)."*
   —— 它在列表里，**恰恰是因为它不被实现**。列表是"生态声明"，不是"能力清单"。
4. **Mozilla 自己拿它当"不支持"的教学样本**：`clients_engine` 的 `TestProcessor` 里 `if name == "forms" { CommandStatus::Unsupported }`

**实际可同步的 collection**：
`history` · `bookmarks` · `passwords` · `tabs` · `addresses` · `creditcards` · `addons` · `prefs` · `clients`

**表单的两条出路**（首版选 A）：
- **A（推荐）**：降级跳过。表单只有本地源有（`formhistory.sqlite`），会让"双源"模型出现单边特例
- **B（进 TODO）**：本地通道加一个 `formhistory.sqlite` 读取器

### 3.8 生态现状

| 事实 | 出处 |
|---|---|
| 官方 Python 客户端 **已归档**（2019-03-28，43⭐） | `mozilla-services/syncclient` |
| 它用的是 **BrowserID/onepw 老协议**，不是现代 OAuth | `syncclient/client.py`（依赖 `PyBrowserID`、`PyFxA 0.3.0`） |
| **GitHub 上零个工具能从 Mozilla 服务器拉历史** | 第一轮调研 |
| 现存工具全是读本地 `places.sqlite` | `nexhq/firefox-dump`、`acquiredsecurity/forensic-webhistory` 等 |

---

## 4. 已知风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **暂用 Firefox 的 `client_id`** | 随时可能被封，所有用户同时失效；oob 交互脆弱 | 写进 TODO 去申请；把 `client_id`/`redirect_uri` 做成可配置，隔离变更 |
| 2 | **`forms` collection 可能已死** | 数据范围里的"表单"拿不到 | `clients_engine` 对 forms 的 reset 返回 `Unsupported`，像遗留项。**需实测** |
| 3 | **同步数据量远小于直觉** | 用户预期落差 | 已用"双源"解决 |
| 4 | **Mozilla 无第三方 CLI 自助注册通道** | 项目无法"干净地"发布 | 同上，先本地自用 |

---

## 5. TODO（明确推迟，不在首版范围）

- [ ] **TUI**（交互式浏览）
- [ ] **趋势 / 统计**（Top 域名、时段分布、每日趋势）
- [ ] **申请自己的 `client_id`**
- [ ] **`localhost` 回调**（替换 oob）
- [ ] **表单记录**（`forms` 已证实是死的 → 首版跳过；将来走本地 `formhistory.sqlite` 单边通道）
- [ ] **开源发布**

---

## 6. 参考资料（本地）

### 6.1 浅克隆仓库（`research/`，共 47M）

| 目录 | 分支 | 大小 | 里面有什么 |
|---|---|---|---|
| `research/application-services/` | `main`（稀疏） | 21M | `places/history_sync/mod.rs`（上限常数）、`sync15/src/key_bundle.rs`（64B 切法）、`sync15/src/client/collection_keys.rs`（collection 密钥）、`docs/design/sync-overview.md` |
| `research/ecosystem-platform/` | `master` | 26M | `docs/explanation/scoped-keys.md`（★ 最关键）、`docs/reference/oauth-details.md`、`docs/relying-parties/reference/integration-requirements.md` |
| `research/syncclient/` | `master` | 240K | 已废弃的官方 Python 客户端（**老协议，仅供参考，不可照抄**） |

克隆命令（复现用）：
```bash
git clone --depth 1 --branch master --single-branch https://github.com/mozilla/ecosystem-platform.git
git clone --depth 1 --branch master --single-branch https://github.com/mozilla-services/syncclient.git
git clone --depth 1 --branch main --single-branch --filter=blob:none --sparse https://github.com/mozilla/application-services.git
cd application-services && git sparse-checkout set components/places components/sync15 components/fxa-client components/support docs
```

### 6.2 调研输出

| 文件 | 内容 |
|---|---|
| `/tmp/ffsync_research.md` | 第一轮：生态现状、现成工具盘点 |
| `/tmp/ffsync_research2.txt` | 第二轮：syncclient 现状、fxa-client 结构 |
| `/tmp/ffsync_research3.txt` | 第三轮：上限常数、密钥派生、记录结构 |
| `tools/_research_ffsync*.py` | 调研脚本源码（可重跑） |

> ⚠️ `/tmp` 下的文件**重启会丢**。重要的结论已全部转写到本文档 §3。

### 6.3 上一版的代码骨架

| 路径 | 状态 |
|---|---|
| `firefox-history/` | 上一版（读本地 `places.sqlite` 的单文件项目）。**数据源层作废，但 `pyproject.toml` + src layout + CLI 结构可复用** |
| `tools/firefox_history.py` | 更早的单文件版本，可丢弃 |

---

## 7. 下一步（开工顺序）

```
① ffinfo 核心库
   ├── OAuth + PKCE + keys_jwk 流程
   ├── 密钥派生链（§3.2）★ 必须用官方测试向量
   ├── Sync 存储协议客户端（分页 / 增量 / backoff）
   └── 记录解密
        ↓
② pizero-firefox-info-cli
   ├── 本地 SQLite（双源分表 + 来源标记）
   ├── export / import
   ├── age 加密 + 权限校验
   └── 固定过滤器 + JSON 输出
        ↓
③ AstrBot skill（类似 bf-stats）
```

**测试策略**：逻辑全 TDD + 官方测试向量；网络层只做少量集成测试。
**验收标准**：`ffinfo sync` 能从真实账号拉到数据并解密成功，`ffinfo list --json` 输出可被 agent 消费。

---

## 8. 仍未拍板

| # | 问题 | 待定 |
|---|---|---|
| A | 如果 `forms` 确认已死 —— 降级跳过，还是另想办法？ | ❓ |
| B | `research/` 的 47M 克隆 —— 开工后保留还是删除？ | ❓ |
