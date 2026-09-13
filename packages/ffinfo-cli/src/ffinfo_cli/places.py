"""本地 Firefox profile 的定位（08 号 ticket）。

``export`` 只在**有 Firefox 的机器**上跑，它的第一件事就是找到那个 profile 的
``places.sqlite``。三平台路径不同，而且**不能猜目录名**（形如 ``<8位随机>.default-release``）——
所以走 Firefox 自己的 ``profiles.ini``，认它标的那个 ``Default=1``。

**``home`` / ``platform`` / ``env`` 一律是参数**，函数自己不读 ``Path.home()`` 和
``sys.platform``：这样在 Linux 上也能把 macOS / Windows 的分支测出来
（见 ``tests/test_places.py``），而不是靠"相信另一条分支是对的"。
"""

from __future__ import annotations

import configparser
import shutil
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from ffinfo.errors import ConfigurationError
from ffinfo_cli._time import from_microseconds, to_microseconds

__all__ = [
    "PLACES_FILENAME",
    "LocalVisit",
    "Profile",
    "Snapshot",
    "discover_profiles",
    "find_profile",
    "firefox_roots",
    "read_visits",
    "snapshot_places",
]

PLACES_FILENAME: Final = "places.sqlite"
"""Firefox 把历史与书签都放在这个文件里。"""

_INI_FILENAME: Final = "profiles.ini"

_SIDECARS: Final = ("-wal", "-shm")
"""WAL 模式下的附属文件。Firefox 跑着的时候最近的记录就躺在 ``-wal`` 里。"""

_VISITS_SQL: Final = """
SELECT p.url AS url,
       p.title AS title,
       v.visit_date AS visit_date,
       v.visit_type AS visit_type
FROM moz_historyvisits v
JOIN moz_places p ON p.id = v.place_id
WHERE IFNULL(p.hidden, 0) = 0
"""


@dataclass(frozen=True, slots=True)
class Profile:
    """一个 Firefox profile。"""

    name: str
    """``profiles.ini`` 里的 ``Name``（人看的名字，不是目录名）。

    用 ``--profile`` 手动指定时退化成**目录名** —— 那条路上我们不去翻 ini，
    编一个"看起来像 Name"的值反而更误导。
    """

    path: Path
    """profile 目录 —— ``places.sqlite`` 就在里面。"""

    is_default: bool
    """Firefox 自己标的默认 profile。"""


def firefox_roots(*, home: Path, platform: str, env: Mapping[str, str]) -> tuple[Path, ...]:
    """这台机器上**可能**放着 Firefox 配置的根目录。

    **不检查存在性** —— 找不到 profile 时要能把"我找过哪些地方"原样报给用户，
    过滤掉不存在的目录反而让报错变得没法照做。

    Linux 有四种落脚点：发行版包、``firefox-esr``、Snap、Flatpak。少一个就是
    "明明装了 Firefox 却说找不到"。
    """
    if platform.startswith("win"):
        return tuple(
            Path(env[var]) / "Mozilla" / "Firefox"
            for var in ("APPDATA", "LOCALAPPDATA")
            if env.get(var)
        )
    if platform == "darwin":
        return (home / "Library" / "Application Support" / "Firefox",)
    return (
        home / ".mozilla" / "firefox",
        home / ".mozilla" / "firefox-esr",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    )


def discover_profiles(*, home: Path, platform: str, env: Mapping[str, str]) -> tuple[Profile, ...]:
    """找出所有**真的有** ``places.sqlite`` 的 profile，默认那个排在最前。

    同一个 profile 可能被 ini 和兜底扫描各撞见一次，按真实路径去重。
    """
    found: dict[Path, Profile] = {}
    for root in firefox_roots(home=home, platform=platform, env=env):
        if not root.is_dir():
            continue
        for profile in (*_profiles_from_ini(root), *_scan(root)):
            key = _identity(profile.path)
            existing = found.get(key)
            if existing is None or (profile.is_default and not existing.is_default):
                found[key] = profile
    return tuple(sorted(found.values(), key=lambda profile: (not profile.is_default, profile.name)))


def find_profile(
    *,
    home: Path,
    platform: str,
    env: Mapping[str, str],
    explicit: Path | None = None,
) -> Profile:
    """挑一个 profile 来读：``explicit`` 优先，否则用 Firefox 标的默认那个。

    两种情况都给**能照做**的错误，不抛裸的 ``FileNotFoundError``。
    """
    if explicit is not None:
        path = Path(explicit)
        if not (path / PLACES_FILENAME).is_file():
            msg = (
                f"{path} 里没有 {PLACES_FILENAME} —— 这不像是一个 Firefox profile 目录。"
                f"确认一下路径，或者直接指向 profile 目录本身（不是它的上级）"
            )
            raise ConfigurationError(msg)
        return Profile(name=path.name, path=path, is_default=True)

    found = discover_profiles(home=home, platform=platform, env=env)
    if found:
        return found[0]

    searched = "、".join(str(root) for root in firefox_roots(home=home, platform=platform, env=env))
    msg = (
        f"没找到任何 Firefox profile（找过：{searched}）。"
        f"要么这台机器上没装过 Firefox，要么 profile 不在默认位置 —— "
        f"用 --profile <profile 目录> 直接指定。"
        f"另外：本地数据得先在**有 Firefox 的机器**上 export，再 import 过来"
    )
    raise ConfigurationError(msg)


# ── 快照与读取 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Snapshot:
    """一份**自包含**的 ``places.sqlite`` 快照。"""

    directory: Path
    """快照所在目录 —— 调用方负责清理。"""

    database: Path
    """快照里的 ``places.sqlite``，WAL 已经折进去了。"""

    sidecars: tuple[str, ...]
    """从源一起复制过来的附属文件（``-wal`` / ``-shm``）。空元组表示源本来就没有。"""

    wal_bytes: int
    """复制时 ``-wal`` 的字节数。**大于 0 说明源库当时在 WAL 模式下还有没落盘的记录**
    —— 也就是"Firefox 正开着"。这个数字要原样带进导出的元数据里。"""


@dataclass(frozen=True, slots=True)
class LocalVisit:
    """本地 ``places.sqlite`` 里的一次访问。"""

    url: str
    title: str
    """可能为空串 —— Firefox 允许一条 place 没有标题。"""
    visited_at: datetime
    """UTC。"""
    visit_type: int


def snapshot_places(database: Path, *, into: Path) -> Snapshot:
    """把 ``places.sqlite`` 连同 ``-wal`` / ``-shm`` 复制出来，再把 WAL 折进主文件。

    **为什么要连附属文件一起复制**：Firefox 跑着的时候库是 WAL 模式，最近的访问还躺在
    ``-wal`` 里。只拷主文件**不会报错**，只会静默少掉那部分 —— 这正是本票要防的坑。

    复制完把 WAL 折进快照（``journal_mode=DELETE``），快照就成了自包含的普通 SQLite：
    读它不再依赖附属文件，导出出去的文件也能随便拷。折完顺手 ``quick_check`` ——
    复制时 Firefox 正好在写的话，这里会拦下来，而不是把半截数据当成品。
    """
    if not database.is_file():
        msg = f"{database} 不存在 —— 这里应该是 Firefox 的 places.sqlite（先确认 profile 目录）"
        raise ConfigurationError(msg)

    into.mkdir(parents=True, exist_ok=True)
    target = into / PLACES_FILENAME
    shutil.copy2(database, target)

    carried: list[str] = []
    wal_bytes = 0
    for suffix in _SIDECARS:
        source = database.with_name(database.name + suffix)
        if not source.is_file():
            continue
        try:
            shutil.copy2(source, target.with_name(target.name + suffix))
        except OSError as exc:
            msg = (
                f"{source} 复制失败（{exc}）—— 最近的访问记录就在 -wal 里，缺了会静默丢数据。"
                f"关掉 Firefox 再试一次，或者用一个关着的 profile"
            )
            raise ConfigurationError(msg) from exc
        carried.append(suffix)
        if suffix == "-wal":
            wal_bytes = source.stat().st_size

    _fold_wal(target, carried=bool(carried))
    return Snapshot(directory=into, database=target, sidecars=tuple(carried), wal_bytes=wal_bytes)


def read_visits(database: Path, *, since: datetime | None = None) -> tuple[LocalVisit, ...]:
    """读出每一次访问，按时间升序。**只读打开**，不碰源文件。

    ``hidden`` 的 URL 跳过 —— 那是 Firefox 自己标的"别在历史里显示"（跳转落点之类），
    跟着它走才不会比用户看到的多出一堆噪声。
    """
    sql = _VISITS_SQL
    params: list[int] = []
    if since is not None:
        sql += "  AND v.visit_date >= ?\n"
        params.append(to_microseconds(since))
    sql += "ORDER BY v.visit_date"

    try:
        with closing(_open_read_only(database)) as connection:
            rows = connection.execute(sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        msg = f"{database} 读不了（{exc}）—— 它不像是 Firefox 的 places.sqlite"
        raise ConfigurationError(msg) from exc

    return tuple(
        LocalVisit(
            url=str(row["url"] or ""),
            title=str(row["title"] or ""),
            visited_at=from_microseconds(int(row["visit_date"])),
            visit_type=int(row["visit_type"]),
        )
        for row in rows
    )


def _fold_wal(target: Path, *, carried: bool) -> None:
    """把 WAL 折进主文件并自检 —— 之后快照就不再依赖附属文件。"""
    with closing(sqlite3.connect(target)) as connection:
        if carried:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        row = connection.execute("PRAGMA quick_check").fetchone()
    verdict = str(row[0]) if row else "空结果"
    if verdict != "ok":
        msg = (
            f"快照自检没过（{verdict}）—— 多半是复制的时候 Firefox 正好在写库。"
            f"再跑一次；还是不行就先关掉 Firefox"
        )
        raise ConfigurationError(msg)


def _open_read_only(database: Path) -> sqlite3.Connection:
    """``mode=ro`` 打开 —— 读快照不产生任何写入，也就不需要写权限。"""
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _profiles_from_ini(root: Path) -> list[Profile]:
    """读 ``profiles.ini``。读不懂就当没有 —— 兜底扫描还在后面。"""
    ini = root / _INI_FILENAME
    if not ini.is_file():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(ini, encoding="utf-8")
    except configparser.Error, OSError, UnicodeDecodeError:
        return []

    # 新版 Firefox 把"默认是哪个"记在 [InstallXXXX] 段里，而不是 Profile 段
    install_defaults = {
        _text(parser, section, "Default")
        for section in parser.sections()
        if section.startswith("Install")
    }

    profiles: list[Profile] = []
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        raw_path = _text(parser, section, "Path")
        if not raw_path:
            continue
        relative = _flag(parser, section, "IsRelative", fallback=True)
        path = (root / raw_path) if relative else Path(raw_path)
        if not (path / PLACES_FILENAME).is_file():
            continue
        profiles.append(
            Profile(
                name=_text(parser, section, "Name") or section,
                path=path,
                is_default=_flag(parser, section, "Default") or raw_path in install_defaults,
            )
        )
    return profiles


def _scan(root: Path) -> list[Profile]:
    """兜底：直接扫一层目录找 ``places.sqlite``。

    ``profiles.ini`` 可能没写全、或者用户手改过 —— 只要文件在，就不该说"找不到"。
    """
    return [
        Profile(name=found.parent.name, path=found.parent, is_default=False)
        for found in sorted(root.glob(f"*/{PLACES_FILENAME}"))
    ]


def _identity(path: Path) -> Path:
    """去重用的键 —— 软链接指向同一个 profile 时不该算两个。"""
    try:
        return path.resolve()
    except OSError:  # pragma: no cover —— 路径畸形到 resolve 都失败时，原样当键
        return path


def _text(parser: configparser.ConfigParser, section: str, option: str) -> str:
    """读一个字符串选项，缺失或读坏都返回空串。"""
    try:
        return parser.get(section, option, fallback="").strip()
    except configparser.Error, ValueError:
        return ""


def _flag(
    parser: configparser.ConfigParser, section: str, option: str, *, fallback: bool = False
) -> bool:
    """读一个布尔选项（ini 里写成 ``1`` / ``true``）。读坏就用 ``fallback``。"""
    try:
        return parser.getboolean(section, option, fallback=fallback)
    except configparser.Error, ValueError:
        return fallback
