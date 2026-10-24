# -*- coding: utf-8 -*-
"""G1a 存储层：配置目录级**跨进程**短时写锁 + 锁内重读合并 + 唯一 tmp 原子替换。

设计约束（``docs/PRODUCT-NEXT-STEPS-SPEC-2026-10-09.md`` §五「并发写」）：

- **目录级、短时**：一把锁护住整个配置目录里的"读→改→写"窗口。粒度到目录而不是
  到文件，是因为一次写入可能涉及同一身份的多个键、活跃集合与代次（它们必须共享
  同一个串行点），而目录里的写者只有桌宠自己（写入量级 = 用户操作级，不是帧级）。
- **不是复活退役的多进程 slot/碰撞锁**：Phase 4.4b 删掉的是"每只宠独占一个 slot
  文件锁 + 定长 PID 记录 + taskkill 清理"那套身份/进程互斥体系（见
  ``pet/slot_manager.py`` 模块 docstring）；这里是**一把**与身份无关的目录写锁，
  只解决"多进程同时写同一个配置文件"的丢更新，不承载任何身份语义。
- **线程内重入必须显式处理**：Windows ``msvcrt.locking`` 与 POSIX ``fcntl.flock``
  都是"同一进程用第二个 fd 锁同一区域同样冲突"（本机实测 PermissionError，见
  ``.scratch/product-identity-storage-20261009/probe-msvcrt-lock-semantics.txt``），
  所以同线程二次获取**不能**靠内核放行，必须自己计数，否则任何"锁内调用另一个也会
  加锁的写函数"都会当场超时失败。同进程**另一个线程**不是重入：它必须真等到持有者
  释放（有界超时）。
- **锁文件永不删除**：删掉锁文件会让后来者在新 inode 上成功加锁，互斥直接失效——
  释放只解锁、不 unlink（锁文件是 0 字节的状态载体，不承载数据）。
- **超时有界、失败不写**：抢不到锁就是 ``WriteLockTimeout``，绝不"降级成无锁写入"
  （那正是丢更新的来源）。写盘失败（临时文件写不出 / 替换失败）抛
  ``AtomicWriteFailed``，**旧文件原样保留**，临时文件任何出口都清掉。
- **单进程假锁不算数**：用 ``threading.Lock`` 假装跨进程互斥是自欺——
  本模块的跨进程语义只能由真子进程验证（``tests/test_config_write_lock.py`` 全部
  用 ``sys.executable -c`` 起真进程；"另一个目录立即可用"那条用例专门证伪
  "拿一个进程内全局互斥假装目录锁"）。

异常合同（调用方按此分流）：``ConfigWriteError`` 为基类；
``WriteLockTimeout``（锁被占用超时）、``JsonStateError``（目标不是可信 JSON 对象，
默认拒绝覆盖）、``AtomicWriteFailed``（写盘失败，旧数据未动）。
``pet.pet_profiles`` 把这些异常翻译成返回结构，壳层接线者只需要看返回结构。
"""
from __future__ import annotations

import errno
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .config import atomic_replace_with_retry

logger = logging.getLogger(__name__)

__all__ = [
    "AtomicWriteFailed",
    "ConfigWriteError",
    "DEFAULT_POLL_S",
    "DEFAULT_TIMEOUT_S",
    "JsonStateError",
    "LOCK_FILENAME",
    "WriteLockTimeout",
    "atomic_write_json",
    "config_write_lock",
    "locked_update_json",
    "read_json_dict",
    "write_lock_path",
]

#: 锁文件名（固定，不带 pid/slot 变体：目录级一把锁）。刻意以 ``.`` 开头且不以
#: ``.json`` 结尾：不匹配 ``config-*.json`` / ``config-slot-*.json`` /
#: ``runtime-*.json`` / ``pet-runtime-v2-*.json`` 任何一个既有 glob。
LOCK_FILENAME = ".config-write.lock"

#: 默认等锁上限与轮询间隔。写者都是用户操作级（保存设置 / 拖完存位置），
#: 5s 足够骑过对方一次短临界区；抢不到就是真异常，必须如实上报。
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_POLL_S = 0.02


class ConfigWriteError(Exception):
    """写锁/写盘错误的基类。"""


class WriteLockTimeout(ConfigWriteError):
    """在 ``timeout`` 内没抢到目录写锁（对方仍在临界区，或对方进程卡住）。"""


class JsonStateError(ConfigWriteError):
    """目标不是可信的 JSON 对象（损坏 / 非对象 / 不可读），默认拒绝覆盖。"""


class AtomicWriteFailed(ConfigWriteError):
    """临时文件写不出或原子替换失败；旧文件保持原样，临时文件已清理。"""


# ------------------------------------------------------------------ 平台原语
#: 抢锁失败时内核给的 errno（各平台/各调用形态都可能出现，统一按"被占用"处理）。
_LOCK_BUSY_ERRNOS = frozenset(
    code for code in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLOCK", None))
    if code is not None
)

if os.name == "nt":
    import msvcrt

    def _os_try_lock(fd: int) -> bool:
        """非阻塞尝试锁住文件第 0 字节；被占用返回 False（其它 OSError 上抛）。"""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _LOCK_BUSY_ERRNOS:
                return False
            raise
        return True

    def _os_unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    _OS_LOCK_BACKEND = "msvcrt"
else:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - 非 Windows 且无 fcntl：没有可用原语
        fcntl = None  # type: ignore[assignment]

    if fcntl is None:  # pragma: no cover - 同上
        def _os_try_lock(fd: int) -> bool:  # type: ignore[misc]
            raise ConfigWriteError("本平台既没有 msvcrt 也没有 fcntl，无法提供文件锁")

        def _os_unlock(fd: int) -> None:  # type: ignore[misc]
            return None

        _OS_LOCK_BACKEND = "none"
    else:
        def _os_try_lock(fd: int) -> bool:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in _LOCK_BUSY_ERRNOS:
                    return False
                raise
            return True

        def _os_unlock(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_UN)

        _OS_LOCK_BACKEND = "fcntl"


# ------------------------------------------------------------------ 进程内重入簿
class _Holder:
    """本进程里当前持有某目录锁的记录（线程 id + 重入深度 + 持有的 fd）。"""

    __slots__ = ("thread", "depth", "fd")

    def __init__(self, thread: int, fd: int) -> None:
        self.thread = thread
        self.depth = 1
        self.fd = fd


_STATE_GUARD = threading.Condition()
_HOLDERS: dict[str, _Holder] = {}


def write_lock_path(config_dir: Path | str) -> Path:
    """目录写锁路径：``<config_dir>/.config-write.lock``。"""
    return Path(config_dir) / LOCK_FILENAME


def _lock_key(directory: Path) -> str:
    """进程内簿的键：规范化绝对路径（Windows 大小写不敏感，避免两种拼法各持一把）。"""
    return os.path.normcase(os.path.abspath(str(directory)))


def _open_lock_fd(path: Path) -> int:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ConfigWriteError(f"无法打开写锁文件 {path}: {exc}") from exc


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _acquire(key: str, lock_path: Path, deadline: float, poll: float) -> None:
    """获取目录锁：同线程重入计数，否则轮询内核锁直到 deadline。

    锁原语的**非"被占用"失败**（如非法 fd、驱动/文件系统不支持字节区间锁）必须翻成
    :class:`ConfigWriteError`：调用方按这个基类分流出"失败但不炸壳"的状态，裸
    ``OSError`` 逃逸会破坏写类 API 的合同。任何异常路径都要关掉刚打开的 fd
    （包括"本平台没有锁原语"那条自抛 ConfigWriteError 的分支）。
    """
    me = threading.get_ident()
    with _STATE_GUARD:
        held = _HOLDERS.get(key)
        if held is not None and held.thread == me:
            held.depth += 1
            return
        while True:
            fd = _open_lock_fd(lock_path)
            try:
                got = _os_try_lock(fd)
            except BaseException as exc:
                _close_fd(fd)
                if isinstance(exc, OSError):
                    raise ConfigWriteError(
                        f"文件锁原语失败（{lock_path}）: {exc}") from exc
                raise
            if got:
                _HOLDERS[key] = _Holder(me, fd)
                return
            _close_fd(fd)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WriteLockTimeout(
                    f"等目录写锁超时（{lock_path}）：另一个进程/线程仍在写")
            # 等通知（同进程释放会 notify）或轮询间隔到点（别的进程释放只能靠轮询）。
            _STATE_GUARD.wait(min(poll, remaining))
            held = _HOLDERS.get(key)
            if held is not None and held.thread == me:
                held.depth += 1
                return


def _release(key: str) -> None:
    """释放一层：深度归零才解锁并关闭 fd，然后唤醒同进程等待者。"""
    fd = None
    with _STATE_GUARD:
        held = _HOLDERS.get(key)
        if held is None:
            return
        held.depth -= 1
        if held.depth > 0:
            return
        _HOLDERS.pop(key, None)
        fd = held.fd
        try:
            _os_unlock(fd)
        except OSError:
            logger.warning("释放目录写锁失败（照常关闭 fd 释放）: %s", key,
                           exc_info=True)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        _STATE_GUARD.notify_all()


@contextmanager
def config_write_lock(config_dir: Path | str, *, timeout: float = DEFAULT_TIMEOUT_S,
                      poll: float = DEFAULT_POLL_S) -> Iterator[Path]:
    """持有 ``config_dir`` 的目录写锁，退出（含异常）时必定释放。

    重入：同一线程嵌套获取是允许的（计数），最外层退出才真正解锁。
    ``timeout`` 是等锁上限（秒）；超时抛 :class:`WriteLockTimeout`，调用方**不得**
    因此降级成无锁写入。
    """
    directory = Path(config_dir)
    lock_path = write_lock_path(directory)
    key = _lock_key(directory)
    _acquire(key, lock_path, time.monotonic() + max(0.0, float(timeout)),
             max(0.001, float(poll)))
    try:
        yield lock_path
    finally:
        _release(key)


# ------------------------------------------------------------------ JSON 读写
def read_json_dict(path: Path | str) -> tuple[dict | None, str]:
    """读一个 JSON 对象文件，返回 ``(数据, 状态)``。

    状态取值：``"ok"`` / ``"missing"`` / ``"corrupt"``（非法 JSON）/ ``"not-object"``
    （合法 JSON 但不是对象）/ ``"unreadable"``（OSError，如目录/权限/被占）。
    只有 ``"ok"`` 带数据；其余一律 ``None``。本函数**只读**，绝不改写或备份——
    "坏文件不覆盖"是调用方的责任，这里只如实分类。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except UnicodeDecodeError:
        # 非法 UTF-8（手改/半截写入/别的编码）也是"内容不可信"，不是读不出来。
        # 不显式接住它就会以 UnicodeDecodeError（ValueError 子类，非 OSError）逃逸到
        # 调用方，把"坏文件不覆盖"的合同炸成异常。
        logger.warning("目录写锁：文件不是 UTF-8 文本，按损坏处理: %s", target)
        return None, "corrupt"
    except OSError:
        logger.warning("目录写锁：JSON 不可读: %s", target, exc_info=True)
        return None, "unreadable"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "not-object"
    return data, "ok"


def atomic_write_json(path: Path | str, data) -> None:
    """唯一临时名 + 原子替换写 JSON；失败抛 :class:`AtomicWriteFailed`。

    临时名带 pid 与随机后缀：同进程两个写者（不同线程/重入出口）也不会撞名。
    替换走 ``config.atomic_replace_with_retry``（骑过 Windows 读句柄造成的瞬时
    共享冲突，与 ``Config.save`` / slot 写入同一份策略，不另造一套）。
    """
    target = Path(path)
    temp = target.with_name(
        f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        atomic_replace_with_retry(temp, target)
    except OSError as exc:
        raise AtomicWriteFailed(f"写盘失败: {target} ({exc})") from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def locked_update_json(path: Path | str, mutate: Callable[[dict], dict | None], *,
                       timeout: float = DEFAULT_TIMEOUT_S,
                       poll: float = DEFAULT_POLL_S,
                       on_broken: str = "refuse",
                       default_factory: Callable[[], dict] = dict) -> dict:
    """锁内"重读 → 合并 → 原子写"，返回写下去的那份字典。

    ``mutate(当前字典)`` 就地改并返回 ``None``，或直接返回一份新字典（两种都支持）。
    ``on_broken``：``"refuse"``（默认）＝ 现有文件损坏/非对象/不可读时抛
    :class:`JsonStateError`，一个字节都不写；``"replace"`` ＝ 从 ``default_factory()``
    重新开始（**备份由调用方自己负责**，本函数不做策略）。

    锁覆盖整个读→改→写：并发写者的更新不会被"整表覆盖"吞掉（真子进程用例验证）。

    **只做顶层合并**：``working`` 是浅拷贝，``mutate`` 只应替换顶层键。**禁止**依赖
    对嵌套 dict/list 的就地修改（那会绕过"合并"语义、也可能改到别人的共享对象）——
    要改嵌套结构就整键重新赋值。本批无此类消费者，故不加运行时 guard，改为在此明令禁止。
    """
    if on_broken not in ("refuse", "replace"):
        raise ValueError(f"on_broken 只接受 'refuse'/'replace'，收到 {on_broken!r}")
    target = Path(path)
    with config_write_lock(target.parent, timeout=timeout, poll=poll):
        data, status = read_json_dict(target)
        if status == "ok":
            working = dict(data or {})
        elif status == "missing" or on_broken == "replace":
            working = dict(default_factory())
        else:
            raise JsonStateError(
                f"拒绝覆盖不可信 JSON（{status}）: {target}")
        updated = mutate(working)
        if updated is None:
            updated = working
        elif not isinstance(updated, dict):
            raise TypeError(f"mutate 必须返回 dict 或 None，收到 {type(updated)!r}")
        atomic_write_json(target, updated)
        return updated
