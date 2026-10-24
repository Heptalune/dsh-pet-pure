# -*- coding: utf-8 -*-
"""G1a 存储切片：目录级跨进程短时写锁（``pet/config_write_lock.py``）。

跨进程互斥**只能用真子进程证明**——单进程里的"假锁"（threading.Lock）证明不了
任何跨进程安全（本批任务书的硬要求）。因此重并发/超时/跨进程持久化用例一律
``sys.executable -c`` 起真子进程，用 stdout token + 文件哨兵做**事件同步**，
不用固定 sleep 猜时序，超时预算给足（60s，CI 慢机不赌）。

覆盖：锁文件位置稳定且永不删、同线程重入、跨线程互斥、锁占用超时且有界、
异常路径释放、跨进程互斥与释放、目录级作用域（不是全局互斥）、并发不丢更新、
唯一 tmp + 原子替换、写失败旧数据不丢、坏文件拒绝覆盖、读状态分类。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pet import config_write_lock as cwl

REPO = str(Path(__file__).resolve().parent.parent)
WAIT = 60.0  # 子进程同步宽预算

#: 子进程脚本（模式经 env 传入；每种模式只做一件事，便于失败归因）
#:
#: hold —— 持锁 → 报 LOCKED → 等 release 哨兵（最多 60s）→ 报 RELEASING → 退出释放
#: try  —— 按给定超时抢锁，抢到报 ACQUIRED，超时报 TIMEOUT
#: count—— 并发自增共享计数器（临界区内留真实工作窗口：无锁实现必丢更新）
#: key  —— 并发写自己那一个键（验证"不同键并写"不互相吞掉）
_CHILD = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["CWL_REPO"])
from pet import config_write_lock as cwl

mode = os.environ["CWL_MODE"]
lock_dir = os.environ["CWL_DIR"]
target = os.environ["CWL_TARGET"]

if mode == "hold":
    with cwl.config_write_lock(lock_dir, timeout=10.0):
        print("LOCKED", flush=True)
        release = os.environ["CWL_RELEASE"]
        deadline = time.monotonic() + 60.0
        while not os.path.exists(release) and time.monotonic() < deadline:
            time.sleep(0.01)
        print("RELEASING", flush=True)
elif mode == "try":
    try:
        with cwl.config_write_lock(lock_dir, timeout=float(os.environ["CWL_TIMEOUT"])):
            print("ACQUIRED", flush=True)
    except cwl.WriteLockTimeout:
        print("TIMEOUT", flush=True)
elif mode == "count":
    def bump(data):
        data["count"] = int(data.get("count", 0)) + 1
        time.sleep(0.001)
        return data
    for _ in range(int(os.environ["CWL_ITER"])):
        cwl.locked_update_json(target, bump, timeout=30.0)
    print("DONE", flush=True)
elif mode == "key":
    key = os.environ["CWL_KEY"]
    for i in range(int(os.environ["CWL_ITER"])):
        cwl.locked_update_json(
            target, lambda data: data.update({key: i}) or None, timeout=30.0)
    print("DONE", flush=True)
elif mode == "silent":
    time.sleep(30.0)
else:
    print("BAD-MODE", flush=True)
    sys.exit(2)
"""


class _Child:
    """真子进程 + **有界**通信（审查 P8）。

    旧写法用阻塞 ``readline()`` 等 token：deadline 形同虚设，子进程只要不说话就能把
    整个测试套件挂死。这里 stdout/stderr 各由一个读线程灌进 queue/列表，所有等待都带
    超时；超时立刻 kill 子进程并带诊断失败——**测试绝不无限等**。
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._lines: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:  # noqa: BLE001 - 读线程只负责转送
            pass
        finally:
            self._lines.put(None)

    def _pump_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                self._stderr.append(line)
        except Exception:  # noqa: BLE001
            pass

    @property
    def stderr(self) -> str:
        return "".join(self._stderr)

    def wait_token(self, token: str, timeout: float = WAIT) -> None:
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.kill()
                raise AssertionError(
                    f"子进程未在 {timeout}s 内输出 {token}（已见 {seen}）"
                    f"stderr={self.stderr[:500]!r}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(
                    f"子进程输出结束仍未出现 {token}（已见 {seen}，退出码 "
                    f"{self.proc.poll()}）stderr={self.stderr[:500]!r}")
            text = line.strip()
            seen.append(text)
            if text == token:
                return

    def join(self, timeout: float = WAIT) -> None:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError(
                f"子进程未在 {timeout}s 内退出；stderr={self.stderr[:500]!r}")
        assert code == 0, f"子进程退出码 {code}，stderr={self.stderr[:500]!r}"

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _spawn(mode: str, lock_dir: Path, target: Path, **extra) -> _Child:
    env = dict(os.environ)
    env.update({
        "CWL_REPO": REPO,
        "CWL_MODE": mode,
        "CWL_DIR": str(lock_dir),
        "CWL_TARGET": str(target),
    })
    env.update({key: str(value) for key, value in extra.items()})
    return _Child(subprocess.Popen([sys.executable, "-c", _CHILD], env=env,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True))


def _holder(lock_dir: Path, target: Path):
    """起一个真的持锁子进程并等到它确认持锁（返回 (child, release_path)）。"""
    release = lock_dir / "release.sentinel"
    child = _spawn("hold", lock_dir, target, CWL_RELEASE=release)
    child.wait_token("LOCKED")
    return child, release


def _release(child: _Child, release: Path) -> None:
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text("go", encoding="utf-8")
    child.join()


# ---------------------------------------------------------------- 锁文件本身
def test_write_lock_path_is_dir_scoped_and_never_unlinked(tmp_path):
    """锁文件位置 = 目录下一个固定名；释放后**不删**（删了会让另一进程锁到新
    inode，互斥失效——这是本实现的关键不变量）。"""
    lock = cwl.write_lock_path(tmp_path)
    assert lock == tmp_path / cwl.LOCK_FILENAME
    assert lock.name == ".config-write.lock"
    assert cwl.write_lock_path(tmp_path) == lock  # 稳定，不随调用漂移
    with cwl.config_write_lock(tmp_path):
        assert lock.exists()
    assert lock.exists(), "释放锁不得删除锁文件（否则下一个进程锁到另一个 inode）"


def test_lock_module_does_not_reintroduce_retired_slot_locks():
    """不是恢复旧碰撞/slot 文件锁：不暴露退役符号，且锁只有目录级一个名字。"""
    for retired in ("acquire_pet_slot", "acquire_file_lock", "SlotLockError"):
        assert not hasattr(cwl, retired), f"不得复活退役锁 API: {retired}"
    # 目录级：同一个目录永远同一个锁文件（没有 per-slot / per-pid 变体）
    assert cwl.LOCK_FILENAME.count("{") == 0 and "%" not in cwl.LOCK_FILENAME


# ---------------------------------------------------------------- 线程面
def test_lock_is_reentrant_in_same_thread(tmp_path):
    """同一线程重入是有意支持的（msvcrt 同一进程第二个 fd 会直接冲突，
    这里必须自己计数，否则内层获取会当场超时失败）。"""
    target = tmp_path / "state.json"
    with cwl.config_write_lock(tmp_path):
        with cwl.config_write_lock(tmp_path):
            with cwl.config_write_lock(tmp_path):
                pass
        # 内层全部退出后外层仍持锁：别的进程必须抢不到
        child = _spawn("try", tmp_path, target, CWL_TIMEOUT=0.4)
        child.wait_token("TIMEOUT")
        child.join()
    child = _spawn("try", tmp_path, target, CWL_TIMEOUT=10.0)
    child.wait_token("ACQUIRED")
    child.join()


def test_lock_released_even_when_body_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with cwl.config_write_lock(tmp_path):
            raise RuntimeError("boom")
    # 异常后必须已释放：同线程再获取立即成功，别的进程也抢得到
    with cwl.config_write_lock(tmp_path, timeout=2.0):
        pass
    child = _spawn("try", tmp_path, tmp_path / "state.json", CWL_TIMEOUT=10.0)
    child.wait_token("ACQUIRED")
    child.join()


def test_second_thread_in_same_process_times_out(tmp_path):
    """跨线程不是重入：同进程另一个线程必须真的等（等到超时）。"""
    with cwl.config_write_lock(tmp_path, timeout=5.0):
        outcome: dict = {}

        def worker():
            try:
                with cwl.config_write_lock(tmp_path, timeout=0.4):
                    outcome["acquired"] = True
            except cwl.WriteLockTimeout:
                outcome["timeout"] = True

        thread = threading.Thread(target=worker, name="lock-probe")
        thread.start()
        thread.join(WAIT)
        assert not thread.is_alive()
        assert outcome.get("timeout") is True, outcome
        assert "acquired" not in outcome
    # 主线程释放后，另一个线程可以正常拿到
    outcome.clear()
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(WAIT)
    assert outcome.get("acquired") is True, outcome


# ---------------------------------------------------------------- 跨进程面
def test_lock_held_by_child_process_times_out_parent_and_bounds_wait(tmp_path):
    """锁被另一个进程持有时：有界超时失败，且**超时绝不无锁写入**。"""
    target = tmp_path / "state.json"
    child, release = _holder(tmp_path, target)
    try:
        started = time.monotonic()
        with pytest.raises(cwl.WriteLockTimeout):
            cwl.locked_update_json(target, lambda data: {"count": 1}, timeout=0.5)
        elapsed = time.monotonic() - started
        assert 0.4 <= elapsed <= 30.0, f"超时不受控: {elapsed:.2f}s"
        assert not target.exists(), "抢不到锁时绝不许写盘（无锁写入=丢数据）"
    finally:
        _release(child, release)
    # 持锁进程退出后立刻可写
    data = cwl.locked_update_json(target, lambda d: {"count": 1}, timeout=10.0)
    assert data == {"count": 1}
    assert json.loads(target.read_text(encoding="utf-8")) == {"count": 1}


def test_lock_scope_is_the_directory_not_the_process(tmp_path):
    """作用域是"目录级"：目录 A 被别的进程锁住时，目录 B 照常可用。

    这条同时证伪"用一个进程内全局互斥假装跨进程锁"——那种实现会把 B 也挡住。
    """
    other = tmp_path / "other-dir"
    other.mkdir()
    child, release = _holder(tmp_path, tmp_path / "state.json")
    try:
        with cwl.config_write_lock(other, timeout=5.0):
            pass  # 别的目录独立
        with pytest.raises(cwl.WriteLockTimeout):
            with cwl.config_write_lock(tmp_path, timeout=0.3):
                pass
    finally:
        _release(child, release)


def test_concurrent_processes_do_not_lose_updates(tmp_path):
    """4 个真进程并发读→改→写同一个 JSON：计数必须一次不丢。

    无锁实现的读→改→写窗口必然互相覆盖（临界区里留了 1ms 真实工作窗口），
    所以这条不是"看运气"的并发测试：丢更新就是红。
    """
    target = tmp_path / "counter.json"
    per_child, child_count = 20, 4
    children = [_spawn("count", tmp_path, target, CWL_ITER=per_child)
                for _ in range(child_count)]
    for child in children:
        child.wait_token("DONE")
        child.join()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["count"] == per_child * child_count, data
    assert list(tmp_path.glob("*.tmp")) == [], "唯一 tmp 必须清理干净"


def test_concurrent_processes_distinct_keys_all_survive(tmp_path):
    """不同键并写：锁内重读合并，谁的键都不许被别人的整表写吞掉。"""
    target = tmp_path / "merged.json"
    keys = [f"k{index}" for index in range(4)]
    children = [_spawn("key", tmp_path, target, CWL_ITER=8, CWL_KEY=key)
             for key in keys]
    for child in children:
        child.wait_token("DONE")
        child.join()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data) == set(keys), data
    assert all(isinstance(data[key], int) for key in keys)


# ---------------------------------------------------------------- 原子写与失败面
def test_locked_update_json_creates_parent_and_leaves_no_temp(tmp_path):
    target = tmp_path / "nested" / "state.json"
    data = cwl.locked_update_json(target, lambda d: d.update({"a": 1}))
    assert data == {"a": 1}
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert list(target.parent.glob("*.tmp")) == []


def test_mutate_may_return_a_fresh_dict(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"a": 1, "b": 2}', encoding="utf-8")
    data = cwl.locked_update_json(target, lambda d: {"only": "new"})
    assert data == {"only": "new"}
    assert json.loads(target.read_text(encoding="utf-8")) == {"only": "new"}


def test_write_failure_keeps_old_data_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"keep": true}', encoding="utf-8")

    def boom(temp, dest, attempts=5):
        raise OSError("disk on fire")

    monkeypatch.setattr(cwl, "atomic_replace_with_retry", boom)
    with pytest.raises(cwl.AtomicWriteFailed):
        cwl.locked_update_json(target, lambda d: d.update({"lost": 1}))
    assert json.loads(target.read_text(encoding="utf-8")) == {"keep": True}
    assert list(tmp_path.glob("*.tmp")) == [], "失败路径不得留临时文件"
    # 失败后锁也必须释放（异常路径）
    monkeypatch.undo()
    assert cwl.locked_update_json(target, lambda d: d.update({"ok": 1})) == {
        "keep": True, "ok": 1}


def test_broken_file_is_refused_by_default_and_replaceable_on_demand(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(cwl.JsonStateError):
        cwl.locked_update_json(target, lambda d: {"fresh": True})
    assert target.read_text(encoding="utf-8") == "{ not json", "默认拒绝覆盖坏文件"
    data = cwl.locked_update_json(target, lambda d: {"fresh": True},
                                  on_broken="replace")
    assert data == {"fresh": True}
    assert json.loads(target.read_text(encoding="utf-8")) == {"fresh": True}


def test_read_json_dict_classifies_states(tmp_path):
    path = tmp_path / "state.json"
    assert cwl.read_json_dict(path) == (None, "missing")
    path.write_text('{"a": 1}', encoding="utf-8")
    assert cwl.read_json_dict(path) == ({"a": 1}, "ok")
    path.write_text("{ oops", encoding="utf-8")
    assert cwl.read_json_dict(path)[1] == "corrupt"
    path.write_text("[1, 2]", encoding="utf-8")
    assert cwl.read_json_dict(path)[1] == "not-object"
    assert cwl.read_json_dict(tmp_path)[1] == "unreadable"  # 目录：读必失败


def test_atomic_write_json_is_direct_and_raises_on_io_error(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    cwl.atomic_write_json(target, {"v": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}

    def boom(temp, dest, attempts=5):
        raise OSError("nope")

    monkeypatch.setattr(cwl, "atomic_replace_with_retry", boom)
    with pytest.raises(cwl.AtomicWriteFailed):
        cwl.atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert list(target.parent.glob("*.tmp")) == []


def test_mutate_exception_leaves_file_and_releases_lock(tmp_path):
    """临界区里抛异常：锁必须释放、旧数据不动、不留临时文件。"""
    target = tmp_path / "state.json"
    target.write_text('{"keep": true}', encoding="utf-8")

    def boom(_data):
        raise RuntimeError("mutate exploded")

    with pytest.raises(RuntimeError):
        cwl.locked_update_json(target, boom)
    assert json.loads(target.read_text(encoding="utf-8")) == {"keep": True}
    assert list(tmp_path.glob("*.tmp")) == []
    # 锁已释放：别的进程立刻抢得到
    child = _spawn("try", tmp_path, target, CWL_TIMEOUT=10.0)
    child.wait_token("ACQUIRED")
    child.join()


# ---------------------------------------------------------------- 审查修复批（P5/P7/P8）
def test_read_json_dict_reports_invalid_utf8_as_corrupt(tmp_path):
    """P5：非法 UTF-8 是"内容不可信"，不是读不出来，更不许异常逃逸。

    旧实现只接 ``OSError``，而 ``UnicodeDecodeError`` 是 ``ValueError`` 子类 ⇒ 直接
    从 ``read_json_dict`` 炸出去（连带 `locked_update_json` / 各写类 API 全炸）。
    """
    path = tmp_path / "state.json"
    path.write_bytes(b'{"a": 1}\xff\xfe')
    assert cwl.read_json_dict(path) == (None, "corrupt")
    before = path.read_bytes()
    with pytest.raises(cwl.JsonStateError):
        cwl.locked_update_json(path, lambda d: {"fresh": True})
    assert path.read_bytes() == before, "坏文件不得被改写"


def test_lock_primitive_oserror_is_wrapped_and_fd_always_released(tmp_path, monkeypatch):
    """P7：锁原语的非"被占用"失败必须翻成 ConfigWriteError，且 fd 一定关掉。

    裸 ``OSError`` 逃逸会破坏写类 API 的"失败返回状态、不炸壳"合同；
    fd 不关就是每次失败漏一个句柄（长跑进程的句柄泄漏）。
    """
    opened: list[int] = []
    real_open = cwl._open_lock_fd

    def spy_open(path):
        fd = real_open(path)
        opened.append(fd)
        return fd

    def boom(_fd):
        raise OSError(22, "invalid argument")

    monkeypatch.setattr(cwl, "_open_lock_fd", spy_open)
    monkeypatch.setattr(cwl, "_os_try_lock", boom)
    with pytest.raises(cwl.ConfigWriteError) as excinfo:
        with cwl.config_write_lock(tmp_path, timeout=1.0):
            pass
    assert not isinstance(excinfo.value, cwl.WriteLockTimeout)
    assert opened, "应至少打开过一次锁文件"
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)                      # 已关闭 → EBADF
    assert cwl._HOLDERS == {}, "失败路径不得在进程内簿里留下持有者"


def test_lock_primitive_configwriteerror_also_releases_fd(tmp_path, monkeypatch):
    """P7 另一半：无锁原语的平台自抛 ConfigWriteError，同样必须清掉 fd。"""
    opened: list[int] = []
    real_open = cwl._open_lock_fd

    def spy_open(path):
        fd = real_open(path)
        opened.append(fd)
        return fd

    def no_backend(_fd):
        raise cwl.ConfigWriteError("本平台没有锁原语")

    monkeypatch.setattr(cwl, "_open_lock_fd", spy_open)
    monkeypatch.setattr(cwl, "_os_try_lock", no_backend)
    with pytest.raises(cwl.ConfigWriteError):
        with cwl.config_write_lock(tmp_path, timeout=1.0):
            pass
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert cwl._HOLDERS == {}


def test_child_wait_helpers_are_bounded_and_kill_the_child(tmp_path):
    """P8：测试自己的等待必须有界——子进程不说话时，helper 要在 deadline 内失败。

    旧 helper 用阻塞 ``readline()``：deadline 形同虚设，子进程静默就能挂死整套测试。
    """
    child = _spawn("silent", tmp_path, tmp_path / "state.json")
    started = time.monotonic()
    with pytest.raises(AssertionError):
        child.wait_token("LOCKED", timeout=0.7)
    elapsed = time.monotonic() - started
    assert elapsed < 20.0, f"等待不受控: {elapsed:.2f}s"
    assert child.proc.poll() is not None, "超时后必须 kill 掉子进程"
