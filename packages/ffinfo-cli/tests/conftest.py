"""``ffinfo-cli`` 测试的共享 fixture。

钉住 typer 的 shell 探测开关 —— 见 ``test_cli`` 里补全用例的 docstring。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_typer_shell_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer 默认用 shellingham 从**进程树**猜当前 shell。

    ``--show-completion`` 注册成 bool flag 时，argv 里的 ``bash`` 根本进不了
    callback；callback 只认 ``value`` 字符串，否则走探测。探测在 prek/pre-push
    （父进程不是 shell）下会失败 → ``Shell  not supported.`` 假红。

    设了这个开关，参数类型变成 ``Shells`` 选择，``--show-completion bash``
    的 ``bash`` 真正进 callback，与机器环境无关。
    """
    monkeypatch.setenv("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION", "1")
