# ffinfo

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
