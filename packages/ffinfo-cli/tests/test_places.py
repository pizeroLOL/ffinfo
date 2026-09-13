"""本地 places.sqlite：profile 定位。

**全部在 Linux 上跑三平台的分支** —— 所以 ``home`` / ``platform`` / ``env``
都是参数，函数自己不读 ``Path.home()`` 和 ``sys.platform``。否则 macOS / Windows
那两条路只能靠"相信它是对的"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffinfo.errors import ConfigurationError
from ffinfo_cli.places import discover_profiles, find_profile

LINUX_ROOT = Path(".mozilla/firefox")
MACOS_ROOT = Path("Library/Application Support/Firefox")


def make_profile(root: Path, name: str, *, with_db: bool = True) -> Path:
    """造一个 profile 目录。``with_db=False`` 用来验证"没 places.sqlite 就不算数"。"""
    profile = root / name
    profile.mkdir(parents=True, exist_ok=True)
    if with_db:
        (profile / "places.sqlite").write_bytes(b"SQLite format 3\x00")
    return profile


def write_ini(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "profiles.ini").write_text(body, encoding="utf-8")


def names(found: tuple[object, ...]) -> list[str]:
    return [profile.name for profile in found]  # type: ignore[attr-defined]


def test_linux_reads_default_profile_from_ini(tmp_path: Path) -> None:
    root = tmp_path / LINUX_ROOT
    make_profile(root, "abc123.default-release")
    write_ini(
        root,
        """
[Profile0]
Name=default-release
IsRelative=1
Path=abc123.default-release
Default=1
""",
    )

    found = discover_profiles(home=tmp_path, platform="linux", env={})

    assert names(found) == ["default-release"]
    assert found[0].path == root / "abc123.default-release"
    assert found[0].is_default is True


def test_macos_reads_application_support(tmp_path: Path) -> None:
    root = tmp_path / MACOS_ROOT
    make_profile(root, "mac.default")
    write_ini(
        root,
        """
[Profile0]
Name=mac
IsRelative=1
Path=mac.default
Default=1
""",
    )

    found = discover_profiles(home=tmp_path, platform="darwin", env={})

    assert names(found) == ["mac"]
    assert found[0].path == root / "mac.default"


def test_windows_reads_appdata_and_localappdata(tmp_path: Path) -> None:
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    make_profile(roaming / "Mozilla/Firefox", "roaming.default")
    make_profile(local / "Mozilla/Firefox", "local.default")
    for root, name in (
        (roaming / "Mozilla/Firefox", "roaming.default"),
        (local / "Mozilla/Firefox", "local.default"),
    ):
        write_ini(
            root,
            f"""
[Profile0]
Name={name}
IsRelative=1
Path={name}
""",
        )

    found = discover_profiles(
        home=tmp_path,
        platform="win32",
        env={"APPDATA": str(roaming), "LOCALAPPDATA": str(local)},
    )

    assert sorted(names(found)) == ["local.default", "roaming.default"]


def test_linux_covers_esr_snap_and_flatpak(tmp_path: Path) -> None:
    """Linux 上 Firefox 有四种落脚点，一个都不能漏。"""
    for root in (
        Path(".mozilla/firefox-esr"),
        Path("snap/firefox/common/.mozilla/firefox"),
        Path(".var/app/org.mozilla.firefox/.mozilla/firefox"),
    ):
        full = tmp_path / root
        make_profile(full, "here.default")
        write_ini(
            full,
            """
[Profile0]
Name=here
IsRelative=1
Path=here.default
Default=1
""",
        )

    found = discover_profiles(home=tmp_path, platform="linux", env={})

    assert len(found) == 3
    assert {profile.path.parent for profile in found} == {
        tmp_path / root
        for root in (
            Path(".mozilla/firefox-esr"),
            Path("snap/firefox/common/.mozilla/firefox"),
            Path(".var/app/org.mozilla.firefox/.mozilla/firefox"),
        )
    }


def test_profile_without_places_sqlite_is_ignored(tmp_path: Path) -> None:
    """光有目录不算数 —— 必须真有 places.sqlite，否则拉回来是空的。"""
    root = tmp_path / LINUX_ROOT
    make_profile(root, "empty.default", with_db=False)
    make_profile(root, "real.default")
    write_ini(
        root,
        """
[Profile0]
Name=empty
IsRelative=1
Path=empty.default

[Profile1]
Name=real
IsRelative=1
Path=real.default
Default=1
""",
    )

    found = discover_profiles(home=tmp_path, platform="linux", env={})

    assert names(found) == ["real"]


def test_absolute_path_profile(tmp_path: Path) -> None:
    """``IsRelative=0`` 时 ``Path`` 是绝对路径，别去拼 root。"""
    root = tmp_path / LINUX_ROOT
    elsewhere = make_profile(tmp_path / "somewhere-else", "moved.default")
    write_ini(
        root,
        f"""
[Profile0]
Name=moved
IsRelative=0
Path={elsewhere}
Default=1
""",
    )

    found = discover_profiles(home=tmp_path, platform="linux", env={})

    assert [profile.path for profile in found] == [elsewhere]


def test_install_section_marks_the_default(tmp_path: Path) -> None:
    """新版本 Firefox 把"默认是哪个"挪到了 ``[InstallXXXX]`` 段。"""
    root = tmp_path / LINUX_ROOT
    make_profile(root, "chosen.default")
    write_ini(
        root,
        """
[Install4F96D1932A9F858E]
Default=chosen.default

[Profile0]
Name=chosen
IsRelative=1
Path=chosen.default
""",
    )

    found = discover_profiles(home=tmp_path, platform="linux", env={})

    assert found[0].is_default is True


def test_missing_ini_falls_back_to_scanning(tmp_path: Path) -> None:
    """``profiles.ini`` 没了（或没见过）也得能找到 —— 直接扫 places.sqlite。"""
    root = tmp_path / LINUX_ROOT
    make_profile(root, "stray.default")

    found = discover_profiles(home=tmp_path, platform="linux", env={})

    assert names(found) == ["stray.default"]


def test_same_profile_seen_twice_is_reported_once(tmp_path: Path) -> None:
    """ini 和兜底扫描会撞见同一个目录 —— 去重按真实路径算。"""
    root = tmp_path / LINUX_ROOT
    make_profile(root, "only.default")
    write_ini(
        root,
        """
[Profile0]
Name=only
IsRelative=1
Path=only.default
Default=1
""",
    )

    found = discover_profiles(home=tmp_path, platform="linux", env={})

    assert len(found) == 1


def test_default_profile_sorts_first(tmp_path: Path) -> None:
    root = tmp_path / LINUX_ROOT
    make_profile(root, "aaa.default")
    make_profile(root, "zzz.default")
    write_ini(
        root,
        """
[Profile0]
Name=aaa
IsRelative=1
Path=aaa.default

[Profile1]
Name=zzz
IsRelative=1
Path=zzz.default
Default=1
""",
    )

    found = discover_profiles(home=tmp_path, platform="linux", env={})

    assert names(found)[0] == "zzz"


def test_find_profile_prefers_the_default(tmp_path: Path) -> None:
    root = tmp_path / LINUX_ROOT
    make_profile(root, "aaa.default")
    make_profile(root, "zzz.default")
    write_ini(
        root,
        """
[Profile0]
Name=aaa
IsRelative=1
Path=aaa.default

[Profile1]
Name=zzz
IsRelative=1
Path=zzz.default
Default=1
""",
    )

    chosen = find_profile(home=tmp_path, platform="linux", env={})

    assert chosen.name == "zzz"


def test_find_profile_takes_explicit_path(tmp_path: Path) -> None:
    """``--profile`` 指哪打哪 —— 哪怕它不在任何标准位置。"""
    profile = make_profile(tmp_path / "hand-made", "anywhere.default")

    chosen = find_profile(home=tmp_path, platform="linux", env={}, explicit=profile)

    assert chosen.path == profile
    assert chosen.is_default is True


def test_find_profile_rejects_explicit_path_without_database(tmp_path: Path) -> None:
    empty = tmp_path / "hand-made"

    with pytest.raises(ConfigurationError) as caught:
        find_profile(home=tmp_path, platform="linux", env={}, explicit=empty)

    message = str(caught.value)
    assert "places.sqlite" in message
    assert str(empty) in message


def test_find_profile_error_is_actionable(tmp_path: Path) -> None:
    """找不到 Firefox 时**不能**抛裸的 FileNotFoundError —— 得说清下一步干什么。"""
    with pytest.raises(ConfigurationError) as caught:
        find_profile(home=tmp_path, platform="linux", env={})

    message = str(caught.value)
    assert "--profile" in message
    assert "Firefox" in message
    assert str(tmp_path / LINUX_ROOT) in message
