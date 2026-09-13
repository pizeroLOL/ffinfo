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
uv run ffinfo --help
```

## 开发

```bash
uv run ruff check .        # lint
uv run ruff format .       # format
uv run pyright             # 类型检查
uv run pytest              # 测试
uv run pre-commit install  # 装 git hooks
```

## 设计文档

见 [`docs/design.md`](docs/design.md)。

## 许可

MPL-2.0
