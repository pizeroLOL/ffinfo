"""冒烟测试：确保包能导入、异常层次正确。"""

from __future__ import annotations

import pytest

import ffinfo
from ffinfo import FfinfoError
from ffinfo.errors import (
    AuthError,
    BackoffError,
    ConfigurationError,
    DecryptionError,
    KeyDerivationError,
    SyncProtocolError,
)


def test_version_exposed() -> None:
    assert ffinfo.__version__ == "0.1.0"


@pytest.mark.parametrize(
    "exc",
    [
        AuthError,
        BackoffError,
        ConfigurationError,
        DecryptionError,
        KeyDerivationError,
        SyncProtocolError,
    ],
)
def test_all_errors_inherit_from_base(exc: type[Exception]) -> None:
    assert issubclass(exc, FfinfoError)


def test_backoff_is_not_a_protocol_error() -> None:
    """退避刻意与 ``SyncProtocolError`` 平级 —— 调用方要单独处理它，别混进"协议出错"。"""
    assert not issubclass(BackoffError, SyncProtocolError)
