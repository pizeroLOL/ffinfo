"""冒烟测试：确保包能导入、异常层次正确。"""

from __future__ import annotations

import pytest

import ffsync
from ffsync import FfsyncError
from ffsync.errors import (
    AuthError,
    ConfigurationError,
    DecryptionError,
    KeyDerivationError,
    SyncProtocolError,
)


def test_version_exposed() -> None:
    assert ffsync.__version__ == "0.1.0"


@pytest.mark.parametrize(
    "exc",
    [AuthError, ConfigurationError, DecryptionError, KeyDerivationError, SyncProtocolError],
)
def test_all_errors_inherit_from_base(exc: type[Exception]) -> None:
    assert issubclass(exc, FfsyncError)
