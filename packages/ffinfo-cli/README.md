# ffinfo-cli

把 Firefox 浏览数据拉到本地 SQLite，输出纯 JSON。

**真正的用户是 agent** —— AstrBot skill（作者工作区的 `skills/ffinfo/`）就调它的 JSON 输出做分析。

## 命令

```bash
ffinfo-cli login                # 浏览器里授权一次（密码不经过本工具）
ffinfo-cli sync                 # 从 Firefox Sync 拉取（显式触发；进度走 stderr）
ffinfo-cli list                 # 查询，恒输出 JSON
ffinfo-cli export <path>        # 在【有 Firefox 的机器】上导出 firefox places.sqlite
ffinfo-cli import <path>        # 在【目标机器】上导入便携文件
ffinfo-cli import --from-firefox [--profile <目录>]  # 就在有 Firefox 的机器上，直接读本机
ffinfo-cli profiles             # 查看本地状态（profile、密钥、各 collection 的同步进度）
```

`list` 的常用开关：`--data-type history|bookmarks|tabs`（默认 `history`）、
`--since` / `--domain` / `--search` / `--limit`。**没有 `--json` 这个开关** ——
输出本来就是 JSON，成功时 stdout 上只有那份结果。

失败时 stdout 是空的：stderr 上是机器可读的错误 JSON（`{"error": {"code", "message"}}`），
退出码分档 —— 表在[根 README](../../README.md)。

## 数据源（双源）

| 源 | 上限 | 说明 |
|---|---|---|
| Firefox Sync | 5000 URL · 每 URL 20 visits · 60 天 | 跨设备，但量小 |
| firefox `places.sqlite` | 无上限 | 需要源机器有 Firefox |

> ⚠️ **破坏性改名（`ListReport.format_version` 3 → 4）**：历史条目的 `source` 取值
> `local` → `firefox`，`sources` 里的 `local` → `firefox`，计数字段 `local_records`
> → `firefox_records`。消费 JSON 的调用方要跟着改。用 `format_version` 判版本。

`list` 会告诉你怎么查：`synced_at` / `age_seconds` 是**数据新鲜度** ——
从没同步过就是 `null`，不假装有数据。

## 许可

MPL-2.0
