# -*- coding: utf-8 -*-
"""安全清理 PyInstaller onefile 遗留的 ``_MEI*`` 临时目录。

**归属是删除的前提**：只有本产品自己打过归属标记（`OWNER_MARKER_NAME`）的运行
目录才可能被删。目录名（``_MEI数字``）+ 年龄是**筛选条件，不是归属证据**——按它
删除会把别的 PyInstaller 应用在系统临时目录里的解包目录当遗留清掉。没有标记的
目录一律保留（刻意的安全降级）；链接/junction/重解析点一律不跟随。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

_MEI_NAME = re.compile(r"^_MEI\d+$")
DEFAULT_STALE_AGE_SECONDS = 24 * 60 * 60

# 归属标记：冻结进程启动时写进自己的 onefile 运行目录。值用**产品级** id
# （不含变体后缀），因为 webm/gif/chat 变体共用同一套临时目录命名空间。
OWNER_MARKER_NAME = ".dsh-pet-runtime-owner"
PRODUCT_OWNER_ID = "dsh-pet-standalone"
_MARKER_READ_LIMIT = 4096


@dataclass
class CleanupResult:
    """一次清理操作的候选、成功和失败结果。"""

    candidates: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    failed: dict[Path, str] = field(default_factory=dict)


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _is_link_like(path: Path) -> bool:
    """符号链接 / junction / 其它重解析点一律不可信（不跟随）。

    ``os.path.islink`` 只认符号链接，junction 与其它重解析点要靠
    ``st_file_attributes``；取不到 stat 时按不可信处理。
    """
    info = _lstat(path)
    if info is None:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _is_mei_directory(path: Path) -> bool:
    """名字是本产品 onefile 运行目录的形态，且本身不是链接/重解析点。"""
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        return False
    if _is_link_like(path):
        return False
    return _MEI_NAME.fullmatch(path.name) is not None


def _read_owner_marker(directory: Path) -> dict | None:
    """读取本产品写的归属标记；标记文件不是普通文件（链接等）时不采信。"""
    marker = directory / OWNER_MARKER_NAME
    if _is_link_like(marker):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(marker, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            return None
        raw = os.read(handle, _MARKER_READ_LIMIT)
    except OSError:
        return None
    finally:
        os.close(handle)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _owned_identity(directory: Path) -> tuple[int, int] | None:
    """归属复核：形态 + 非链接 + 标记属于本产品；返回身份 ``(st_dev, st_ino)``。

    调用方在删除前再取一次身份做比对，用于发现扫描与删除之间的目录替换（TOCTOU）。
    """
    if not _is_mei_directory(directory):
        return None
    marker = _read_owner_marker(directory)
    if marker is None or marker.get("owner") != PRODUCT_OWNER_ID:
        return None
    info = _lstat(directory)
    if info is None or not stat.S_ISDIR(info.st_mode):
        return None
    return (int(info.st_dev), int(info.st_ino))


def mark_owned_runtime_dir(directory: Path | str) -> bool:
    """给本产品自己的运行目录写归属标记（失败返回 False，不抛异常）。

    写的是进程自己的 ``_MEIPASS`` 这类目录，不需要也不应经由链接写。
    """
    path = _resolved(directory)
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or _is_link_like(path):
        return False
    marker = path / OWNER_MARKER_NAME
    # 首次写时标记还不存在（不算“链接”）；已存在就必须是普通文件，不跟着链接写
    if _lstat(marker) is not None and _is_link_like(marker):
        return False
    payload = json.dumps(
        {"owner": PRODUCT_OWNER_ID, "pid": os.getpid(), "written": int(time.time())}
    ).encode("utf-8")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        handle = os.open(marker, flags, 0o600)
        try:
            os.write(handle, payload)
        finally:
            os.close(handle)
    except OSError:
        return False
    return True


def mark_frozen_runtime_dir() -> Path | None:
    """冻结进程启动时标记自己的 onefile 运行目录，返回标记路径或 None。

    只在 ``sys._MEIPASS`` 确实是**系统临时目录下的 `_MEI数字`目录**时写标记：
    onedir（现打包形态）的 ``_MEIPASS`` 是安装目录里的 ``_internal``，既不是
    临时目录里的解包目录，也没有可清理的对象。本次启动写的标记，让**下一次**
    启动能把这次崩溃留下的目录认成自己的。
    """
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    directory = _resolved(meipass)
    if not _is_mei_directory(directory):
        return None
    if directory.parent != _resolved(tempfile.gettempdir()):
        return None
    return directory if mark_owned_runtime_dir(directory) else None


def _process_alive(pid: int) -> bool:
    """探测 PID 是否活着；**只在 POSIX 使用**。

    Windows 的 ``os.kill`` 对非 0 信号是真结束进程（不是信号），因此这里直接
    拒绝执行；Windows 侧的在用判定由 `_dir_in_use` 的改名探测负责。
    """
    if os.name == "nt" or pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我们的进程
    except OSError:
        return False
    return True


def _marker_pid_alive(directory: Path) -> bool:
    """标记里记的写入进程是否还活着（PID 复用只会导致保守跳过，不会误删）。"""
    marker = _read_owner_marker(directory)
    if marker is None:
        return False
    try:
        pid = int(marker.get("pid"))
    except (TypeError, ValueError):
        return False
    return _process_alive(pid)


def _tree_has_link_like(directory: Path) -> bool:
    """目录树里有没有链接/junction/重解析点；不跟随，读不动即算有。"""
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            return True
        for entry in entries:
            try:
                if entry.is_symlink() or _is_link_like(Path(entry.path)):
                    return True
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
            except OSError:
                return True
    return False


def find_stale_runtime_dirs(
    temp_dir: Path | str | None = None,
    *,
    current_dir: Path | str | None = None,
    min_age_seconds: float = DEFAULT_STALE_AGE_SECONDS,
    now: float | None = None,
) -> list[Path]:
    """返回系统临时目录中**本产品所有**且明确过期的 `_MEI数字`目录。

    候选必须同时满足：目录形态 + 非链接 + 本产品归属标记 + 年龄超阈值。
    没有归属标记的目录（别的应用留下的、以及本产品修复前留下的）一律不是候选；
    只扫描临时目录的直接子目录；`current_dir` 与本进程的 ``sys._MEIPASS`` 始终跳过。
    """
    root = _resolved(temp_dir or tempfile.gettempdir())
    protected = {_resolved(current_dir)} if current_dir is not None else set()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        protected.add(_resolved(meipass))
    timestamp = time.time() if now is None else float(now)
    result: list[Path] = []

    try:
        children = list(root.iterdir())
    except OSError:
        return result

    for child in children:
        if _MEI_NAME.fullmatch(child.name) is None:
            continue
        if _owned_identity(child) is None:
            continue
        info = _lstat(child)
        if info is None:
            continue
        try:
            if _resolved(child) in protected:
                continue
            age = timestamp - info.st_mtime
        except OSError:
            continue
        if age >= max(0.0, float(min_age_seconds)):
            result.append(child)

    return sorted(result, key=lambda item: item.name.lower())


def _remove_readonly(func, path: str, _exc_info) -> None:
    """为 shutil.rmtree 的只读文件重试删除；不修改 ACL 或所有者。"""
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _dir_in_use(directory: Path) -> bool:
    """探活：目录被存活实例占用时不可删除。

    Windows 上运行中的 onefile 实例锁定其 _MEI 目录内的 DLL/pyd，
    对目录做原地改名会失败（WinError 32/5）——改名探测比按文件名猜 PID 可靠。
    探测成功会立刻改回原名。
    POSIX 上 rename 恒成功、无法作为占用信号，改用归属标记里记录的写入进程
    是否仍在运行（PID 复用只会让我们保守跳过，方向是安全的）。
    """
    if os.name != "nt":
        return _marker_pid_alive(directory)
    probe = directory.with_name(directory.name + ".probe")
    try:
        os.rename(directory, probe)
        os.rename(probe, directory)
    except OSError:
        # 改名失败：可能是占用（跳过），也可能是权限问题（rmtree 同样会失败）
        try:
            if probe.exists() and not directory.exists():
                os.rename(probe, directory)  # 尽力恢复原名
        except OSError:
            pass
        return True
    return False


def cleanup_stale_runtime_dirs(
    temp_dir: Path | str | None = None,
    *,
    current_dir: Path | str | None = None,
    min_age_seconds: float = DEFAULT_STALE_AGE_SECONDS,
    now: float | None = None,
    dry_run: bool = False,
) -> CleanupResult:
    """清理本产品所有、过期且未在使用的 `_MEI`目录。

    删除前逐条复核（不靠扫描结果）：归属标记仍在 + 身份未变（防扫描与删除之间
    的目录替换）+ 目录树内没有链接/junction + 占用探活通过；任一条不满足就跳过。
    """
    candidates = find_stale_runtime_dirs(
        temp_dir,
        current_dir=current_dir,
        min_age_seconds=min_age_seconds,
        now=now,
    )
    result = CleanupResult(candidates=list(candidates))
    if dry_run:
        return result

    for directory in candidates:
        identity = _owned_identity(directory)
        if identity is None:
            continue  # 归属证据已消失（被换掉/标记被改）：不删
        if _dir_in_use(directory):
            continue  # 另一个仍在运行的实例：跳过，等它退出后下次再清
        if _owned_identity(directory) != identity:
            continue  # 探活（会改名）期间目录被替换：不删
        if _tree_has_link_like(directory):
            continue  # 树内有重解析点：rmtree 会跟着它删到别处，整条放弃
        if _owned_identity(directory) != identity:
            continue  # 走树复核期间又被替换：不删（把删除前的窗口压到最小）
        try:
            shutil.rmtree(directory, onerror=_remove_readonly)
        except OSError as exc:
            result.failed[directory] = f"{type(exc).__name__}: {exc}"
        else:
            result.removed.append(directory)
    return result