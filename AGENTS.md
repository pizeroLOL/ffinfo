# AGENTS.md

本仓库的 agent 工作约定。**人也可以看** —— 里面没有只有 agent 才懂的东西。

## 项目速览

**ffinfo** —— 把 Firefox 浏览数据（云端 Sync + firefox `places.sqlite`）拉到本地 SQLite，
输出纯 JSON 给 agent 消费。真正的用户是 agent，不是人。

| 包 | 职责 |
| --- | --- |
| `packages/ffinfo` | 纯 Python Firefox Sync 客户端库：OAuth · 密钥派生 · 记录解密 · 存储协议 |
| `packages/ffinfo-cli` | CLI：本地 SQLite · 双源合并 · export/import · JSON 输出 |

**完整设计与决策见 [`docs/design.md`](docs/design.md)** —— 开工前先读它。
那份文档是**自包含**的：20 条决策、每条技术事实都带源码出处，读它一份就能接手。

## 开发命令

**全表见 [`README.md`](README.md)「开发」** —— ruff / format / pyright / pytest / prek 那一组。
agent 日常只需要：

```bash
uv sync                                   # 装依赖（含两个 workspace 成员）
uv run pytest                             # 测试（自带 --cov，覆盖率 ≥90 才过）
uv run prek install && uv run prek install --hook-type pre-push  # 钩子；检查：
test -f .git/hooks/pre-commit && test -f .git/hooks/pre-push
```

> ⚠️ **`pyright` 必须带 `--project pyproject.toml`**（防扫到祖先 `pyrightconfig.json`）；
> **`include` 不能写通配**（pyright 会静默 0 文件）。两条的完整解释在
> [`pyproject.toml`](pyproject.toml) `[tool.pyright]` 注释里 —— 改配置只看那里。
>
> **git 钩子用 [prek](https://github.com/j178/prek)**，读同一份 `.pre-commit-config.yaml`：
> `pre-commit` 挡 ruff / 格式 / pyright；`pre-push` 才跑 `pytest`。三平台矩阵以 CI 为准。
>
> push 被 `pytest` 拦下时：先同命令本地重跑，分清真红还是环境 flake，再决定重推 ——
> 判据见 [`CODING_STANDARDS.md`](CODING_STANDARDS.md)。

## 多 agent 并行：一票一 worktree

同一工作区里并行改代码会互踩（全量测试中途假红、同文件冲突）。**两个以上 agent 动代码时**，
不要挤在主 checkout 里：

```bash
# 主 checkout（验收 / 合并用）保持在开发分支上干净
git worktree add ../ffinfo-<NN> -b <type>/<NN>-<slug>   # 从当前开发分支拉出
cd ../ffinfo-<NN> && uv sync
# ……实现、scoped 测试、ruff/pyright、按票提交……
```

| 角色 | 做完什么算完 |
| --- | --- |
| 实现 agent | 在**自己的 worktree 分支**上提交；scoped `pytest` + `ruff` + `pyright` 绿；按 [`CODING_STANDARDS.md`](CODING_STANDARDS.md) 回写文档 |
| 验收 / 合并 | 在主 checkout `git merge` 该分支 → 跑**全量**门禁 → 绿则合入、删分支与 worktree：`git worktree remove ../ffinfo-<NN>` |

- 并行期间**不要**在别人的树里跑全量 `pytest` 当依据；全量只在合并后的主 checkout 跑。
- 一票一个分支名；票号与 `.scratch/.../issues/NN-…` 对齐。

## 动文档的节奏

**改前**：先确认 `docs/design.md` 对应节与代码仍一致（对不上先修文档或记漂移）。
**改后**：契约面（退出码、`format_version`、render union、硬性约束）必须同提交回写 ——
清单见 [`CODING_STANDARDS.md`](CODING_STANDARDS.md)。

## 代码约定

- **类级变量一律带类型标注** —— 写 `field: Type = ...`，不留裸赋值。
  框架元数据也算：

  ```python
  class Record(BaseModel):
      model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
      id: str


  class Client:
      __slots__: tuple[str, ...] = ("_http",)


  class Row(Table):
      collection: Varchar = Varchar(length=64, index=True)
  ```

  ruff 的 ANN 与 pyright strict 都管不到类属性 —— **由 `pytest` 强制**：
  `packages/ffinfo-cli/tests/test_conventions.py` 扫两个包的 `src`（须在 Python 3.14 下跑）。
  违规会在 `uv run pytest` 里红，不必再手写 AST。

## 评审标准

**review 阶段**另读 [`CODING_STANDARDS.md`](CODING_STANDARDS.md)：改前对文档、改后回写、
表征测试命名、工单状态。实现阶段不需要背那一页 —— 指针到此为止。

## 硬性约束

- **库不持有任何默认路径。** `ffinfo` 的所有 I/O 位置由调用者注入，构造函数不设默认参数；
  默认路径只在 CLI 层决定。
- **密码永不进 CLI。** 认证走 OAuth + PKCE + `keys_jwk`，密码只在 `accounts.firefox.com`
  的网页里输入。
- **「只读」只保证两处。** ① 读 Mozilla 账户：一行写回的操作都没有；
  ② 导入 firefox 记录：`places.sqlite` 用 `mode=ro` 打开，不碰源文件。
  本地库 `ffinfo.sqlite` **不在只读保证内** —— `sync` 落盘、`import` 合并、
  `list` 建库都会写它；`store.open_database(read_only=True)` 只是跳过
  建表/迁移/收敛，不是连接级只读。
  另外 scope 层面**做不到只读** —— `…/oldsync#read` 不返回 `keys_jwe`，
  只能用完整的 `…/oldsync`（读写）。"能力上可写"和"能力上不可写"是两回事，
  见 [`docs/design.md`](docs/design.md) §2.1 的实测修订。
- **不拉来历不明的 collection。** 白名单是 `history` / `bookmarks` / `tabs`；
  `forms` 里是遗留记录，`passwords` / `creditcards` / `addresses` 不在本项目范围内。
  代码见 `ffinfo_cli/sync.py` 的 `SYNCABLE_COLLECTIONS`。

## 本地工作材料（不在版本控制里）

> **这一节是写给"在作者这台机器上干活的 agent"的。**
> 别人 clone 下来**看不到**下面这些文件 —— 这是**有意的**，不是仓库缺了东西。

| 东西 | 在哪 | 说明 |
| --- | --- | --- |
| 工单 / spec | `.scratch/<feature-slug>/` | 本地 markdown；`.scratch/` 在 `.gitignore` 里。一票一文件：`issues/NN-<slug>.md` |
| 工单状态 | 每个文件顶部一行 `**Status:**` | 取值见 [`docs/agents/triage-labels.md`](docs/agents/triage-labels.md)；收工时翻成 `done` 并勾验收项 |
| 格式约定 | [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md) | 一票一文件、`## Comments` 追加在末尾 |
| 领域词汇 | [`CONTEXT.md`](CONTEXT.md) | 一词一义；`docs/adr/` 还没建，用到时再建 |

**这些都不是项目的一部分** —— 删掉不影响构建、测试、运行。
手里没有 `.scratch/` 的话，你该读的是 [`docs/design.md`](docs/design.md)。
