"""默认位置 —— **只有 CLI 层才决定路径**。

库（``ffsync``）不持有任何默认值，所有 I/O 位置由调用者注入；
默认位置在这里定，见 ``docs/design.md`` §2.5。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

_APP: Final = "ffinfo"


def config_dir() -> Path:
    """配置目录 —— 私钥放这里。"""
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / _APP


def data_dir() -> Path:
    """数据目录 —— 加密凭据（以及将来的 SQLite）放这里。"""
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / _APP


def identity_path() -> Path:
    """age 私钥文件（必须 0600）。"""
    return config_dir() / "age-key.txt"


def credentials_path() -> Path:
    """加密后的 Mozilla 凭据。"""
    return data_dir() / "credentials.age"
