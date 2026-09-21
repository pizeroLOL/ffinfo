# ffinfo

把 Firefox 浏览数据（云端 Sync + firefox `places.sqlite`）搬到本地 SQLite、输出 JSON 给 agent 的工具。
这份词汇表是本领域**一词一义**的约定：出现同义词时，以本表选定的那个为准。

## 数据源与出处

**Sync**（云端）:
Mozilla 的 Firefox 同步服务；作为数据来源时指云端那一份数据。
_Avoid_: 服务器、云

**firefox**（firefox 源）:
装了 Firefox 的机器上 `places.sqlite` 里的浏览历史。它是"非常大"那半历史的唯一来源。
_Avoid_: places、本地源、本地历史、本地数据库

**来源 (source)**:
一次访问出自哪边 —— `sync` / `firefox` / `both`。`both` 不是源，是合并之后才有的结论。
_Avoid_: 出处、origin、local（旧取值）

**机器 (machine)**:
`export` 时记下的源机器名，回答"这条是哪个机器看的"。
_Avoid_: 设备、client

**设备 (client)**:
Sync `tabs` 里的一台 Firefox 客户端（有自己的 id 与 `clientName`）。和 `machine` 不是一回事。
_Avoid_: 机器、machine

**profile**:
Firefox 的一个配置档 —— `places.sqlite` 所在目录，外加人看的名字。
_Avoid_: 配置、用户

## 记录与访问

**BSO**:
Sync 服务器上的最小存储单元，带 id / modified / payload / sortindex / ttl。`payload` 为 null 时是墓碑。
_Avoid_: 记录（单独用时）

**记录 (record)**:
一个 collection 里的一条数据；解密后就是 HistoryRecord / BookmarkRecord / TabsRecord。
一条历史记录可以含多次访问。
_Avoid_: BSO、条目

**访问 (visit)**:
一次页面浏览 —— 历史记录里的一次 `visit`，或 firefox 里的一行 `moz_historyvisits`。
`list` 的历史输出以**访问**为单位，不是以记录为单位。
_Avoid_: 条目、记录

**墓碑 (tombstone)**:
删除标记。两种编码，同一个意思：这条没了 ——
云端 BSO 的墓碑是 `payload` 为 null；**历史与书签**的应用层墓碑是明文 `{"deleted": true}`
（这种常缺主体字段，要在模型校验之前认出来）。
_Avoid_: 删除记录

**collection**:
Sync 里的一个命名数据集（`history` / `bookmarks` / `tabs`，外加协议用的 `crypto`）。
_Avoid_: 表、集合

**数据类型 (data type)**:
CLI `--data-type` 的取值（`history` / `bookmarks` / `tabs`）。
它是 collection 在用户接口层的名字，不是另一个东西。
_Avoid_: collection

## Sync 机制

**游标 (cursor)**:
一个 collection 的同步进度 —— 服务器给的 collection 时间戳，下次增量拉取拿它当起点。
_Avoid_: last_modified、进度

**collection 时间戳**:
服务器给整个 collection 的 `X-Last-Modified`。**游标存的就是它**。
_Avoid_: last_modified、modified

**modified**:
一条 BSO / 记录自己的修改时间。
_Avoid_: 时间戳（单独用时）、last_modified

**全量 / 增量**:
sync 的两种模式。全量拉整个 collection 并整体替换（对账用）；增量只拉游标之后的变更。
_Avoid_: 完全同步、full 同步

**退避 (backoff)**:
服务器要求"现在别来"。软退避走 `X-Weave-Backoff`（可能出现在 200 上），硬退避走 `Retry-After`。
_Avoid_: 限流、等待

## 密钥与解密

**私钥 (identity)**:
age 私钥文件，只用来加解密**凭据**。与 Mozilla 的登录凭据是两样东西。
_Avoid_: 密钥

**凭据 (credentials)**:
Mozilla 的 OAuth token 与 scoped keys，加密后落盘。
_Avoid_: token、登录信息

**scoped key**:
OAuth 授权时 Mozilla 派发给某个 scope 的密钥；oldsync 那把是 64 字节 kSync。
_Avoid_: 应用密钥

**kSync**:
oldsync scoped key 的 64 字节本体，切成 enc_key + hmac_key。
_Avoid_: scoped key

**KeyBundle**:
一对 32 字节密钥（AES-256 加密 + HMAC-SHA256 签名），解密记录的最小单位。
_Avoid_: 密钥对（单独用时）

**keys_jwe / keys_jwk**:
OAuth 授权时的一次性 JWE —— CLI 交出临时公钥 `keys_jwk`，Mozilla 用它对 scoped keys 加密得到 `keys_jwe`。
_Avoid_: 密钥（单独用时）

**crypto/keys**:
Sync 里的密钥记录（id 为 `keys`），装着 default 与各 collection 的 KeyBundle。
_Avoid_: 密钥表

## 本地存储与搬运

**本地 (local)**:
CLI 自己的 SQLite —— `sync` 落盘、`list` 读它、`import` 往它里面并。
_Avoid_: ffinfo 库、本地库、本地 SQLite、数据库

**快照 (snapshot)**:
把 firefox 库（`places.sqlite`）连同 `-wal` / `-shm` 复制出来、再把 WAL 折进主文件的
**自包含**副本。export 只读快照。
_Avoid_: 备份、副本

**WAL**:
firefox 库的预写日志。Firefox 开着时最近的访问还在 `-wal` 里；只拷主文件会静默少数据。
_Avoid_: 日志

**便携文件 (portable file)**:
`export` 写出、`import` 读入的那份 SQLite，用来在机器之间搬数据。
_Avoid_: 导出文件、dump

**导出出处 (export source)**:
便携文件里记的"这份导出从哪来" —— 机器、profile、生成器、WAL 状态。
_Avoid_: 来源、source

**导出 (export)**:
只有一个选项：产出便携文件 —— 在有 Firefox 的机器上把快照与本地库里的云端状态写进去。
_Avoid_: 备份

**导入 (import)**:
把浏览数据并进本地库。有两种输入选项：本机 firefox，或一份 export 产出的便携文件。
_Avoid_: 恢复

## 报告口径

**format_version**:
每份 JSON 报告自己的格式版本，供 agent 判断结构。
_Avoid_: 版本

**records**:
报告里"从本地库读出来的记录条数"（加密记录，未拍平）。
_Avoid_: 条数

**visits**:
报告中**云端**记录拍平后的访问次数 —— 一条记录可以有多次访问。
_Avoid_: records

**firefox_records**:
firefox 源读出来的访问条数。
_Avoid_: local_records（旧字段名）

**matched / returned**:
`matched` = 过滤之后剩多少；`returned` = `--limit` 之后实际返回多少。两者口径相同。
_Avoid_: 总数

**skipped / dropped**:
`skipped` = 解密失败、被跳过的记录；`dropped` = 解密成功、但建树时丢弃的病态记录。
_Avoid_: 丢失

**新鲜度**:
`synced_at`（该 collection 上次成功 sync 的时间）与 `age_seconds`（距今多久）。
_Avoid_: 更新时间
