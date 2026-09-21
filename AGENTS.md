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

```bash
uv sync                                   # 装依赖（含两个 workspace 成员）
uv run ruff check .                       # lint
uv run ruff format .                      # format
uv run pyright --project pyproject.toml   # 类型检查（strict）
uv run pytest                             # 测试
uv run prek install                       # 装 git 钩子
```

> ⚠️ **`pyright` 必须带 `--project pyproject.toml`。**
> pyright 会向上遍历目录找 `pyrightconfig.json`，**优先于**本地的 `pyproject.toml`。
> 如果祖先目录里恰好有一个，裸跑 `pyright` 会去扫别人的代码。
> 在干净的环境里裸跑没问题，但带上参数永远安全。
>
> 📌 `include` 里**不能写通配**（pyright 不认，会静默地一个文件都不分析）—— 写死目录。
> 依赖没有类型信息带来的 unknown 系列（pyrage / piccolo）**已豁免**，见 `pyproject.toml`
> 里的注释；补存根后把豁免开回来（记在待办里）。

> **git 钩子用 [prek](https://github.com/j178/prek)** —— pre-commit 的 Rust 替代，
> 读同一份 `.pre-commit-config.yaml`。ruff / ruff-format / pyright 都走 **local 钩子**：
> 跑的就是 `uv run` 那一份，不会像钉死 rev 的钩子那样跟 `pyproject.toml` 漂开。

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

  ⚠️ **没有 lint 规则能强制这一条**（ruff 的 ANN 系列只管函数签名），靠 review 兜住。
  检查方式：用 AST 扫 `ast.Assign` 落在 `ClassDef` 顶层的裸 `Name` 赋值
  —— 注意必须用 **Python 3.14** 跑（代码里有 PEP 758 的 `except A, B:` 新语法，3.12 解析不了）。

## 硬性约束

- **库不持有任何默认路径。** `ffinfo` 的所有 I/O 位置由调用者注入，构造函数不设默认参数；
  默认路径只在 CLI 层决定。
- **密码永不进 CLI。** 认证走 OAuth + PKCE + `keys_jwk`，密码只在 `accounts.firefox.com`
  的网页里输入。
- **代码严格只读。** 一行写回 Mozilla 的操作都没有。
  但要说清楚：scope 层面**做不到只读** —— `…/oldsync#read` 不返回 `keys_jwe`，
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
| 工单状态 | 每个文件顶部一行 `Status:` | 取值见 [`docs/agents/triage-labels.md`](docs/agents/triage-labels.md) |
| 格式约定 | [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md) | 一票一文件、`## Comments` 追加在末尾 |
| 领域词汇 | [`CONTEXT.md`](CONTEXT.md) | 一词一义；`docs/adr/` 还没建，用到时再建 |

**这些都不是项目的一部分** —— 删掉不影响构建、测试、运行。
手里没有 `.scratch/` 的话，你该读的是 [`docs/design.md`](docs/design.md)。
