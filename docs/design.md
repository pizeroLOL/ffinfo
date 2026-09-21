# ffinfo 设计归档

> **状态**：设计归档 + 已落地 —— 两个包都能跑：CLI 六条命令（`login` / `sync` / `list` /
> `export` / `import` / `profiles`）真机验证过，三平台 CI 全绿（2026-09-14）。
> 本文档仍然自包含：**读这一份就能接手**。
> **归档时间**：2026-09-13
> **2026-09-14 补记**：包名调整 —— 库 `ffsync` → `ffinfo`；CLI 的 Python 模块 → `ffinfo_cli`
> （PyPI 名仍是 `ffinfo-cli`，命令仍是 `ffinfo`）。下文已按新名字改写。
> **来源**：6 轮 grilling 拷问 + 3 轮源码调研 + 3 个浅克隆仓库
> **用途**：自包含参考文档 —— 未来任何会话（人或 agent）读这一份就能接手

---

## 0. 一句话

> **一个把 Firefox 浏览数据从 Mozilla 云端和 firefox `places.sqlite` 拉到本地 SQLite、输出纯 JSON 给 agent 消费的 CLI 工具；核心协议逻辑独立成一个可复用的 Python 库。**

---

## 1. 项目结构：两个包，一个仓

| | 包 | 职责 | 依赖 |
|---|---|---|---|
| **A** | `packages/ffinfo`（PyPI: `ffinfo`） | 纯 Python Firefox Sync 客户端库：OAuth 认证 · 密钥派生 · 记录解密 · 存储协议 | — |
| **B** | `packages/ffinfo-cli`（PyPI: `ffinfo-cli`） | CLI · 本地 SQLite · 双源合并 · `export`/`import` | → `ffinfo` |

物理上是一个 **uv workspace 单仓**：两个包各有自己的 `pyproject.toml`（可各自独立发布），
仓库根只是把它们拉成依赖的壳 —— 这样裸 `uv sync` 就能装全。

**为什么这样切**：官方那个 Python Sync 客户端（`mozilla-services/syncclient`）**2019 年就归档了**，至今没有任何替代品。`ffinfo` 单独成库本身就是市面上的稀缺品。

---

## 2. 决策清单（6 轮拷问成果，20 条）

### 2.1 用途与数据

| # | 决策 | 备注 |
|---|---|---|
| 1 | 用途：**个人复盘** | 不是取证、不是清理、不是数据管道 |
| 2 | 数据源：**双源** | ① Firefox Sync（远程）② firefox `places.sqlite` |
| 3 | firefox 源前提：**源机器必须有 Firefox** | 靠 `export`/`import` 搬运到目标机器 |
| 4 | 数据范围：**历史 + 书签 + 标签页**（~~表单~~ ❌ 已证实拿不到，见 §3.7） | 表单移入 TODO |
| 5 | 边界：**严格只读** | 用 `#read` scope，不写回 Mozilla |
| 6 | 输出：**纯数据** | 不做统计、不做 TUI（都进 TODO） |
| 7 | 真正用户：**agent（skill）** | 已有一个类似 `bf-stats` 的 AstrBot skill 调它（作者工作区 `skills/ffinfo/`） |

> ⚠️ **决策 5 的实测修订（2026-09-14）**：原写「用 `#read` scope，严格只读」。
> 实测：`…/oldsync#read` **不返回 `keys_jwe`** —— FxA 只给完整的 `…/oldsync` 派发密钥。
> 所以 scope 层面**只能是读写**。本库代码仍然严格只读（一行写操作都没有），
> 但「能力上可写」和「能力上不可写」是两回事，不能含糊其辞。

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
| 13 | **age** 加密 · `age-keygen` 生成专用密钥 | 存 `~/.config/ffinfo-cli/age-key.txt` |
| 14 | **权限校验写进代码** | 不是 `0600` 就拒绝启动 —— 拒绝"默默不安全" |
| 15 | 增量：**显式 `ffinfo-cli sync`** + 超期提示 | 查询保持纯本地、瞬时 |

### 2.4 接口与工程

| # | 决策 | 备注 |
|---|---|---|
| 16 | CLI：**固定过滤器 + JSON** | `--since`/`--domain`/`--search`/`--limit`；**不做 SQL 直通** |
| 17 | `export`/`import` 格式：**SQLite** | 因为要支持增量（schema 版本 + 游标 + 校验） |
| 18 | 工程栈 | **Python 3.14 baseline** · uv · src layout · **pyproject 单文件配置** · ruff · pyright · pytest · pre-commit · 现代 typing · async · httpx · pydantic |
| 19 | 许可：**MPL-2.0** | 与 Mozilla 生态一致 |
| 20 | CLI 失败契约：**分档退出码 + stderr 错误 JSON** | 成功时 stdout 只有 JSON；失败时 stdout 为空、stderr 是 `{"error": {"code", "message"}}`，退出码表见 `README.md` |

### 2.5 库/应用职责分离（新增约束）

> **库不持有任何默认路径，所有 I/O 位置由调用者注入；CLI 决定默认位置。**

```python
# ffinfo —— 无默认值，必须显式传（构造函数不设默认参数）
client = SyncClient(credentials_path=..., keys_path=..., cache_dir=...)

# ffinfo-cli —— 在这里决定默认
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "ffinfo-cli"
DATA_DIR   = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "ffinfo-cli"
```

#### 默认位置落在哪（三平台）

| 平台 | 配置（私钥） | 数据（凭据 / SQLite） |
| --- | --- | --- |
| Linux | `$XDG_CONFIG_HOME` 或 `~/.config` | `$XDG_DATA_HOME` 或 `~/.local/share` |
| **macOS** | **同上 —— 走 XDG** | **同上 —— 走 XDG** |
| Windows | `%APPDATA%` | `%LOCALAPPDATA%` |

**macOS 为什么不走 `~/Library/Application Support`**：这里的凭据和私钥是给**命令行工具**
用的，XDG 在 macOS 的开发者圈子里是通行做法（`~/.config` 到处都是）。改成原生路径的唯一
收益是"更像 macOS 应用"，代价是同一份文档、同一套排错步骤跨平台不再通用。真要改，等有
macOS 用户提出来再说 —— 在那之前，这是个**写下来的决定**，不是"漏了 macOS"。

行为锁在 `packages/ffinfo-cli/tests/test_paths.py`，其中一条显式把平台改成 `darwin`。

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
**这就是"双数据源"存在的全部理由** —— 要"非常大"的历史，只能读 firefox `places.sqlite`。

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

**刷新（RFC 6749 §6）** —— 出处：`application-services/components/fxa-client/src/internal/http_client.rs`
（`OAauthTokenRequest::UsingRefreshToken` 与 `OAuthTokenResponse`，注释指明它照着
`fxa-auth-server/lib/oauth/routes/token.js` 写的）：

- 请求：`grant_type=refresh_token` + `client_id` + `refresh_token`（`scope` / `ttl` 可选，本库不发）
- 响应里的 `refresh_token` 与 `keys_jwe` **都是可选的**：
  - `refresh_token` 缺席 = 没轮换，沿用旧的；**来了就得存回去**（轮换过的那份才有用）
  - `keys_jwe` 我们**不会收到**：请求里没带 `keys_jwk`，服务器没有公钥可加密。
    就算收到了也解不开（登录时的临时私钥早丢了）—— 而 scoped key 本来不过期，旧的仍然对
- `expires_in` 是必填（上游结构里就是 `u64`，不是 Option）—— 本库缺席时当场报错，不猜
- 失败长什么样 —— **FxA 不用 RFC 那套 `invalid_grant`**（2026-09-14 实测）：
  - 坏的 refresh token → `400` + `error: "Bad Request"` + `errno: 108`（格式对但服务器不认）/
    `errno: 109`（参数不合法，`validation.keys` 会点名 `refresh_token`）
  - 上游指南（`ecosystem-platform/docs/relying-parties/reference/using-apis.md`）：
    **刷新请求也回 401 = 用户已经把这个应用的授权断开了** —— 该重新授权，而不是继续重试
  - 这三条都翻译成了"重新授权一次"的可操作提示（`oauth.py` 的 `_ERRNO_HINTS` / 401 分支）

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

> **2026-09-14 实测补记 —— 落到 JSON 上的字段名和单位**
>
> 上面是 Rust 结构体，实际传输的 JSON 长这样（`#[serde(rename_all = "camelCase")]`）：
>
> ```json
> {"id": "…", "title": "…", "histUri": "https://…",
>  "visits": [{"date": 1788444520420000, "type": 1}]}
> ```
>
> - 访问类型的字段名是 **`type`**，不是 Rust 里的 `transition` —— 照结构体抄会 KeyError
> - `date` 是**微秒**（`ServerVisitTimestamp` = 毫秒 × 1000）。差三个数量级，时间会飘到 1970
> - `title` 可能是 `null`，也可能整个字段不存在 —— 两种都当空串（上游也是这么处理的）
> - `type = 10`（`UpdatePlace`）按上游注释**不是一次真正的页面访问**，但确实会出现在记录里。
>   照实标名字、不偷偷丢掉，算不算"浏览"由消费方决定
>
> **数量级**：一个真实账号 4892 条记录解出 **12110 次访问**（平均 2.5 次/条）——
> 所以"拍平成一次访问一行"不是可选项：不拍平会丢掉六成的浏览记录。

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

### 3.7 ❌ `forms` collection 已死（已定案）—— 但服务器上还躺着记录

**结论：现代 Firefox Sync 不同步表单历史。`forms` 是遗留的引擎名，没有实现。**

> **2026-09-14 实测补记 —— 把话说准**
>
> `/info/collection_counts` 显示测试账号的 `forms` 上有 **42768 条**记录，
> 全账号第二多（仅次于 history 的 4890）。所以**死的是引擎，不是数据**：
> 记录还躺在 Mozilla 服务器上，只是 2019 年前后再没有任何客户端去同步它。
>
> ⚠️ **本项目的决定：不拉 `forms`，一条都不拉。**
> 这批记录是遗留物，内容无从考证，而老 Firefox 的表单历史里可能混着
> 当年在网页表单里填过的敏感内容（**包括密码**）。
> 来历不明的东西不碰 —— 宁可漏，不可错。代码层面见
> `ffinfo_cli/sync.py` 的 `SYNCABLE_COLLECTIONS` 白名单。

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
- **A（推荐）**：降级跳过。表单只有 firefox 源有（`formhistory.sqlite`），会让"双源"模型出现单边特例
- **B（进 TODO）**：firefox 通道加一个 `formhistory.sqlite` 读取器

> **云端 `forms` 和 firefox `formhistory.sqlite` 是两回事，别混**：
> 云端那 42768 条遗留记录**永远不拉**，白名单已经把它挡在门外；
> 这里说的 A / B 是**firefox** 那个文件要不要读 —— 那是用户自己机器上的文件，来源清楚，
> 将来想做还能做。

### 3.8 firefox 源：places.sqlite 与 export/import（08 号，2026-09-14）

**profile 定位不猜目录名**（形如 `<8位随机>.default-release`），走 Firefox 自己的
`profiles.ini`。默认是哪个：**`[InstallXXXX] Default` 命中某个 profile 时优先**，
Profile 段的 `Default=1` 只在 Install 段没命中时作数（没有 Install 段，或它指向的 profile
连 `places.sqlite` 都没有）。老 `default` profile 升级后往往不清掉 `Default=1` —— 若与
Install 段平权，两者会并列成默认，再按名字排序就把旧 profile 挑出来（`default` <
`default-release`），`import --from-firefox` 静默导入 0 条。三平台根目录：

| 平台 | 根目录 |
| --- | --- |
| macOS | `~/Library/Application Support/Firefox` |
| Linux | `~/.mozilla/firefox` · `firefox-esr` · **Snap** · **Flatpak**（四种落脚点） |
| Windows | `%APPDATA%` 与 `%LOCALAPPDATA%` 下的 `Mozilla/Firefox` |

`profiles.ini` 没了就兜底扫一层目录找 `places.sqlite` —— 文件在就不该说"找不到"。
`home` / `platform` / `env` 全部从参数进，所以 macOS 与 Windows 的分支在 Linux 上就能测。

#### ⚠️ WAL：导出必须连 `-wal` / `-shm` 一起带走

Firefox 跑着的时候库是 WAL 模式，**最近的访问还在 `places.sqlite-wal` 里**。
只拷主文件**不会报错**，只会静默少掉那部分。

**事后判断不出 `-wal` 是不是丢了**：Firefox 正常关闭之后，库照样是 WAL 模式、
照样没有 `-wal` 文件 —— "WAL 模式 + 没有 -wal" 根本不是证据。所以**防线只能在导出端**：
`snapshot_places` 把附属文件一起复制、再把 WAL 折进快照（`journal_mode=DELETE`），
折完 `quick_check` 自检。便携文件的元数据里记下导出当时的 WAL 状态
（`wal_bytes` / `wal_carried`），`import` 在"说有 WAL 却没带出来"时**明确报警**。

#### 合并键：`(url, 访问时刻)`

云端那条的时刻来自记录里的 `date`，firefox 那条来自 `moz_historyvisits.visit_date` ——
两边都是 PRTime **微秒**。所以只要换算不引入误差，它们就能精确对上。这也是
`ffinfo_cli/_time.py` 坚持整数运算（而不是 `value / 1_000_000`）的原因：
**差 1 微秒，同一次访问就会出两行。**

两个源都有 → 一行，标 `both`；只有一边 → 标 `sync` 或 `firefox`；
firefox 源是空的（目标机器没导入过）就自然降级成单源。**单源降级不需要登录**：
库里没有任何云端记录时，`list` 一步都不碰 age 私钥（凭据 / scoped key 只为解密云端密文而存在）；
只有 `bookmarks` / `tabs` 这些云端独有类型才在缺凭据时报错。

#### 便携文件（决策 17 的落地）

一份 SQLite，表名带 `ffinfo_` 前缀免得跟 `moz_*` 撞上：

| 表 | 装什么 |
| --- | --- |
| `ffinfo_export` | `schema_version` · 来源机器 · profile · 导出时间 · WAL 状态 · 计数 |
| `ffinfo_visits` | firefox 访问 |
| `ffinfo_records` | 云端加密记录（原样搬，不解密） |
| `ffinfo_cursors` | 每个 collection 的同步游标 |

读的一方负责校验：**不是我们的文件 / schema 版本不认识 → 拒收**；
**条数对不上账 / WAL 没带出来 → 收下但必须把告警交出去**，不静默接受。
有人直接把 Firefox 的 `places.sqlite` 拷过来时，当场告诉他 `-wal` 的坑在哪。

导入时三样东西各按各的规矩合并：firefox 访问按 `(机器, url, 时刻)` 认（重复导入幂等）；
云端记录**只在导出的那条更新时才覆盖**（不拿旧数据盖新数据）；游标**只往前推**。
批量插入一律**分块**（每批 100 行）—— piccolo 把整批拼成一条多值 `INSERT`，
真实历史一次上万条会撞上 SQLite 的变量数上限（`too many SQL variables`）。

#### 表单：两条出路，首版选 A

云端 `forms` 永远不拉（白名单挡着，见 §3.7）；firefox `formhistory.sqlite` 是另一回事
（来源清楚），但会让"双源"模型出现单边特例 —— 首版**降级跳过**，进 TODO。

#### 实测（2026-09-14，真账号）

| 检查 | 结果 |
| --- | --- |
| 云端 | 4907 条记录 / 12139 次访问 |
| 导入一条"与云端同一次访问"的 firefox 记录后 | `matched` 11420 → **11421** —— 只多了 firefox 独有的那条，**重合的没有重复** |
| 重合那条的标记 | `source: "both"`，带云端 `record_id` 与 firefox 机器名 |
| 重复导入同一份文件 | `visits_inserted: 0` / `visits_skipped: 2` —— 幂等 |

---

### 3.9 生态现状

| 事实 | 出处 |
|---|---|
| 官方 Python 客户端 **已归档**（2019-03-28，43⭐） | `mozilla-services/syncclient` |
| 它用的是 **BrowserID/onepw 老协议**，不是现代 OAuth | `syncclient/client.py`（依赖 `PyBrowserID`、`PyFxA 0.3.0`） |
| **GitHub 上零个工具能从 Mozilla 服务器拉历史** | 第一轮调研 |
| 现存工具全是读 firefox `places.sqlite` | `nexhq/firefox-dump`、`acquiredsecurity/forensic-webhistory` 等 |

---

## 4. 已知风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **暂用 Firefox 的 `client_id`** | 随时可能被封，所有用户同时失效；oob 交互脆弱 | 写进 TODO 去申请；把 `client_id`/`redirect_uri` 做成可配置，隔离变更 |
| 1b | ↑ **已兑现（2026-09-14 实测）** | Desktop 的 `client_id` **根本用不了**（只注册了 webchannel 的 `urn:` redirect_uri）。改用 **Firefox iOS 的 `client_id`**（`1b1a3e44c54fbb58`）—— 它注册了普通 HTTPS 回调，授权码能落到地址栏 |
| 1c | **`keys_jwk` 要 base64url 编码**（2026-09-14 实测） | 传原始 JSON 会被 FxA 拒（`constraints: matches`） | 已在 `oauth.py` 修正并加了回归测试 |
| 2 | **`forms` collection 可能已死** | 数据范围里的"表单"拿不到 | `clients_engine` 对 forms 的 reset 返回 `Unsupported`，像遗留项。**需实测** |
| 3 | **同步数据量远小于直觉** | 用户预期落差 | 已用"双源"解决 |
| 4 | **Mozilla 无第三方 CLI 自助注册通道** | 项目无法"干净地"发布 | 同上，先本地自用 |
| 5 | **access token 过期后只能人工重跑 `login`** | 与"无人值守"矛盾 —— 过期那一刻起，`sync` 只能报"重新登录" | ✅ **已解决**：过期时 `sync` 先用 `refresh_token` 自动续（RFC 6749 §6，见 §3.3）；refresh token 也失效（`invalid_grant`）才提示重新 `login` |

### 已知取舍

不是风险，是**明知可以改、现在故意不改**的地方。每条都记下"何时该动"，免得后来者（或未来的自己）
把"没写"误当成"没想到"。

| # | 取舍 | 是什么 | 为何不做 | 何时该做 |
|---|---|---|---|---|
| Q2 | `run_import` 两分支各建一份 `ImportReport` | 便携路与 firefox 路各构造一份报告，共享约 4 行（`open_database` / `_store_visits` / `visits_skipped` / `elapsed`） | 分叉是本质的：便携路还带云端记录与游标，firefox 路没有。抽 helper 会退化成十来个参数、一半对某条分支恒空的开关（Repeated Switches / Speculative Generality） | 出现第三种 import 输入时（Rule of Three） |

---

## 5. TODO（明确推迟，不在首版范围）

- [ ] **TUI**（交互式浏览）
- [ ] **趋势 / 统计**（Top 域名、时段分布、每日趋势）
- [ ] **申请自己的 `client_id`**
- [ ] **`localhost` 回调**（替换 oob）
- [ ] **表单记录**（`forms` 已证实是死的 → 首版跳过；将来走 firefox `formhistory.sqlite` 单边通道）
- [ ] **开源发布**

---

## 6. 参考资料（本地）

### 6.1 上游仓库（浅克隆，按需）

调研时浅克隆过三个仓库（共 47M）。它们**不在版本控制里** —— 别人 clone 下来没有是正常的，
§3 的每条事实都写清了上游出处（仓库 + 文件路径），不依赖本地副本就能核对。
要复现这份副本：

```bash
git clone --depth 1 --branch master --single-branch https://github.com/mozilla/ecosystem-platform.git
git clone --depth 1 --branch master --single-branch https://github.com/mozilla-services/syncclient.git
git clone --depth 1 --branch main --single-branch --filter=blob:none --sparse https://github.com/mozilla/application-services.git
cd application-services && git sparse-checkout set components/places components/sync15 components/fxa-client components/support docs
```

| 仓库 | 里面有什么 |
|---|---|
| `mozilla/application-services` | `places/history_sync/mod.rs`（上限常数）、`sync15/src/key_bundle.rs`（64B 切法）、`sync15/src/client/collection_keys.rs`（collection 密钥）、`docs/design/sync-overview.md` |
| `mozilla/ecosystem-platform` | `docs/explanation/scoped-keys.md`（★ 最关键）、`docs/reference/oauth-details.md`、`docs/relying-parties/reference/integration-requirements.md` |
| `mozilla-services/syncclient` | 已废弃的官方 Python 客户端（**老协议，仅供参考，不可照抄**） |

### 6.2 调研输出（作者本机）

| 文件 | 内容 |
|---|---|
| `/tmp/ffsync_research.md` | 第一轮：生态现状、现成工具盘点 |
| `/tmp/ffsync_research2.txt` | 第二轮：syncclient 现状、fxa-client 结构 |
| `/tmp/ffsync_research3.txt` | 第三轮：上限常数、密钥派生、记录结构 |
| `tools/_research_ffsync*.py` | 调研脚本源码（可重跑） |

> 这些是过程材料，**不在版本控制里**；`/tmp` 下的**重启会丢**。
> 重要的结论已全部转写到本文档 §3 —— 丢了也不影响接手。

### 6.3 上一版的代码骨架（作者本机）

| 路径 | 状态 |
|---|---|
| `firefox-history/` | 上一版（读 firefox `places.sqlite` 的单文件项目）。**数据源层作废，但 `pyproject.toml` + src layout + CLI 结构可复用** |
| `tools/firefox_history.py` | 更早的单文件版本，可丢弃 |

---

## 7. 开工顺序（已走完）

> **① ② ③ 都已落地（2026-09-14）** —— 库 + CLI 真机验证过，AstrBot skill 在作者工作区的
> `skills/ffinfo/`（不在本仓库）。下面是当初定的路线，留作记录。

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
**验收标准（已达成）**：`ffinfo-cli sync` 能从真实账号拉到数据并解密成功，
`ffinfo-cli list` 输出可被 agent 消费（`list` 恒输出 JSON，没有 `--json` 这个开关 ——
见 `README.md` 的退出码与错误契约）。

---

## 8. 拍板记录（原「仍未拍板」）

| # | 问题 | 结论 |
|---|---|---|
| A | 如果 `forms` 确认已死 —— 降级跳过，还是另想办法？ | ✅ **已定案（见 §3.7）**：云端 `forms` 一条都不拉（白名单挡着）；firefox `formhistory.sqlite` 留作将来的单边通道 |
| B | `research/` 的 47M 克隆 —— 开工后保留还是删除？ | ✅ **已定案（2026-09-14）**：浅克隆只留在作者本机、不进版本控制；§6.1 改成上游链接 + 复现命令，文档不再依赖本地副本 |
| C | **pyright 修法** | ✅ **已拍板（2026-09-14）**：**先豁免** unknown 系列（`reportUnknown*` + `reportAttributeAccessIssue`），把"自己写的类型"这层检查保住；`include` 的通配已修，检查器真的在跑（52 个文件 / 0 errors）。补依赖存根（pyrage / piccolo）记进待办，补完把豁免开回来 |
| D | Python 版本策略：现在 `requires-python = ">=3.14"` 且真用了 PEP 758。库的卖点是「官方客户端归档后市面唯一替代品」—— 锁死在 3.14 等于掐死卖点；放宽要改写那处语法 | ✅ **已拍板（2026-09-14）：保持 3.14 不动** —— `uv sync` 会自己把解释器拉下来，"锁死 3.14"不构成使用门槛。将来真要放宽，再改写那处语法 |
