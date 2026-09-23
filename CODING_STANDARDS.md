# Coding standards

**评审（review）阶段读这一页**；实现 agent 只靠 [`AGENTS.md`](AGENTS.md) 的指针进来。
能落 lint / 测试的规则不写在这里 —— 那些由 prek 与 `pytest` 强制（含
`test_conventions.py` 的类级标注扫描）。这里只留**判断题**。

## 动代码之前：文档先对上

改一块行为前，打开它在 [`docs/design.md`](docs/design.md) 的对应节（目录里的决策号 / §9.x），
确认文档描述仍与**当前代码**一致：

- 对得上 → 再改；本票结束时文档跟着改。
- 对不上（先漂了）→ **先修文档或在工单里记漂移**，不要按过期文档实现。

`CONTEXT.md` 的词与报告字段名以词汇表为准；实现引入新术语先回写词汇表。

## 动代码之后：契约面回写文档

改动碰到下列任一**agent 可见面**，同一提交里必须改文档，缺一即 review 打回：

| 改动 | 回写到 |
| --- | --- |
| 退出码 / `error.code` | `README.md` 退出码表 + `docs/design.md` §9 失败契约 |
| 报告 `format_version` 或顶层字段增删 | `README.md`（若有示例）+ `docs/design.md` 对应报告节 |
| `render` 的 report union 成员 | `docs/design.md` §9.2（union 不再是「五元组」之类旧口径） |
| 公开 API / 硬性约束的行为含义 | `docs/design.md` 决策条目；触达 `AGENTS.md` 硬性约束则同步那一条 |
| 领域新词 | `CONTEXT.md` |

没碰这些面的纯内部改动：不必为了「好像该写」去扩 design。

## 表征测试与命名

- 翻转 `test_current_*`（或同类「钉现状」测试）后，**函数名与 docstring 必须描述新行为**。
- 有意保留的表征（协议事实、wontfix 行为）可留 `test_current_` 前缀，docstring 写明
  「协议如此 / 设计如此」，不得写成已修复。
- 名实不符（名说 bug、断言已修）= review 打回。

## 工单状态

- 实现方：scoped 测试绿后勾验收项，`**Status:**` 可标 `done`。
- 跨票全量、文档回写、并行合并顺序：归**验收 / 合并**那一环，不靠实现方自证。
