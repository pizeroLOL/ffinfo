# ffinfo-cli

把 Firefox 浏览数据拉到本地 SQLite，输出纯 JSON。

**真正的用户是 agent** —— 会有一个 AstrBot skill 调它的 JSON 输出做分析。

## 命令

```bash
ffinfo_cli sync                 # 从 Firefox Sync 拉取（显式触发）
ffinfo_cli export <path>        # 在【有 Firefox 的机器】上导出本地 places.sqlite
ffinfo_cli import <path>        # 在【目标机器】上导入
ffinfo_cli list --json          # 查询，输出 JSON
ffinfo_cli profiles             # 查看状态
```

## 数据源（双源）

| 源 | 上限 | 说明 |
|---|---|---|
| Firefox Sync | 5000 URL · 每 URL 20 visits · 60 天 | 跨设备，但量小 |
| 本地 `places.sqlite` | 无上限 | 需要源机器有 Firefox |

## 许可

MPL-2.0
