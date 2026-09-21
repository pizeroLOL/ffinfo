"""CLI 命令的 adapter —— 每条命令一个 module，各自暴露 ``register(app)``。

**逻辑不在这里**：命令体只做「参数校验 + 调业务 module + 渲染」。这样装配点
（``ffinfo_cli/cli.py``）只 import 命令 module，命令 module 只 import 业务与 ``failures``，
不会成环。
"""
