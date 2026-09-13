# ffinfo

[![CI](https://github.com/pizeroLOL/ffinfo/actions/workflows/ci.yml/badge.svg)](https://github.com/pizeroLOL/ffinfo/actions/workflows/ci.yml)

> 徽章指向的这个仓库是**私有**的 —— 没登录的访客看不到它（徽章会显示成灰色的
> "no status"）。想看真实的红绿，得先有仓库权限。

把 Firefox 浏览数据（云端 Sync + 本地 `places.sqlite`）拉到本地 SQLite，
输出纯 JSON 给 agent 消费。

## 结构

| 包 | 职责 |
|---|---|
| `packages/ffinfo` | 纯 Python Firefox Sync 客户端库：OAuth · 密钥派生 · 解密 · 存储协议 |
| `packages/ffinfo-cli` | CLI：本地 SQLite · 双源合并 · export/import · JSON 输出 |

## 快速开始

```bash
uv sync
uv run ffinfo-cli login              # 浏览器里授权一次，密码不经过本工具
uv run ffinfo-cli sync               # 从 Sync 拉数据，输出 JSON
uv run ffinfo-cli list --limit 20    # 解密后的浏览历史，输出 JSON
```

**本地那半历史**（云端同步只有 5000 条 / 60 天的上限）要在**装了 Firefox 的机器**上搬：

```bash
# 源机器（有 Firefox）
uv run ffinfo-cli export portable.sqlite     # 连 places.sqlite-wal 一起带走

# 目标机器
uv run ffinfo-cli import portable.sqlite     # 与云端数据合并，每条标出来源
```

## 退出码与错误 JSON

成功时 stdout 上只有那份 JSON；**失败时 stdout 是空的**，stderr 上是机器可读的错误
JSON，退出码分档 —— 调用方不用猜是"参数错了"还是"该重新登录了"：

| 退出码 | `code` | 含义 |
| --- | --- | --- |
| 0 | —— | 成功 |
| 1 | `error` | 兜底（没登记的错误类型） |
| 2 | `usage` | 用法错误：参数越界、不认识的值 |
| 3 | `configuration` | 本地配置或凭据问题（先 `login` / 先 `sync`） |
| 4 | `auth` | 认证失败 —— 重新 `login` |
| 5 | `backoff` | 服务器要求退避 —— 等 `wait_seconds` 秒再来（`soft` 区分软硬） |
| 6 | `protocol` | Sync 协议层面出错 |
| 7 | `decryption` | 记录解密失败 |
| 8 | `key_derivation` | 密钥派生失败 |

```json
{"error": {"code": "backoff", "message": "服务器要求退避，还需等待 60 秒（Retry-After）。库里没动任何东西。", "wait_seconds": 60.0, "soft": false}}
```

## 开发

```bash
uv run ruff check .                       # lint
uv run ruff format .                      # format
uv run pyright --project pyproject.toml   # 类型检查（strict）
uv run pytest                             # 测试
uv run prek install                       # 装 git 钩子
```

> `pyright` 要带 `--project pyproject.toml` —— 它会向上遍历目录找祖先里的
> `pyrightconfig.json` 并优先用那个。细节见 [`AGENTS.md`](AGENTS.md)。

## 文档

- [`docs/design.md`](docs/design.md) —— **设计与决策归档，自包含**：19 条决策、每条技术事实
  都带源码出处。想接手这个项目，读它一份就够。
- [`AGENTS.md`](AGENTS.md) —— 工作约定：开发命令、硬性约束、本地工单放在哪。
- `docs/agents/` —— 一套**可选**的工单/领域文档约定，描述的是作者本地的协作流程。
  里面提到的 `.scratch/` 不在版本控制里，clone 下来没有是正常的。

## 许可

MPL-2.0
