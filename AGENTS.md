# AGENTS.md

本仓库的 agent 工作约定。

## 项目速览

**ffinfo** —— 把 Firefox 浏览数据（云端 Sync + 本地 `places.sqlite`）拉到本地 SQLite，
输出纯 JSON 给 agent 消费。真正的用户是 agent，不是人。

| 包 | 职责 |
| --- | --- |
| `packages/ffinfo` | 纯 Python Firefox Sync 客户端库：OAuth · 密钥派生 · 记录解密 · 存储协议 |
| `packages/ffinfo-cli` | CLI：本地 SQLite · 双源合并 · export/import · JSON 输出 |

**完整设计与决策见 [`docs/design.md`](docs/design.md)** —— 开工前先读它。

## 开发命令

```bash
uv sync                 # 装依赖（含两个 workspace 成员）
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pyright --project pyproject.toml   # 类型检查（strict）
uv run pytest           # 测试
uv run prek install      # 装 git 钩子（prek 是 pre-commit 的 Rust 替代，配置同一份）
```

> ⚠️ **`pyright` 必须带 `--project pyproject.toml`。**
> pyright 会向上遍历目录找 `pyrightconfig.json`，**优先于**本地的 `pyproject.toml`。
> 如果祖先目录里恰好有一个（本开发环境里就有），裸跑 `pyright` 会去扫别人的代码。
> 在干净的环境里裸跑没问题，但带上参数永远安全。

## 硬性约束

- **库不持有任何默认路径。** `ffinfo` 的所有 I/O 位置由调用者注入，构造函数不设默认参数；默认路径只在 CLI 层决定。
- **密码永不进 CLI。** 认证走 OAuth + PKCE + `keys_jwk`，密码只在 `accounts.firefox.com` 的网页里输入。
- **严格只读。** 用 `#read` scope，不写回 Mozilla 服务器。

## Agent skills

### Issue tracker

Issues and specs live as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles, label strings equal to their names. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
