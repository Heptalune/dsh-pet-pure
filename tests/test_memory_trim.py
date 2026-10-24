# -*- coding: utf-8 -*-
"""「回收内存」(#210) 单测：过滤真值表 / 句柄回收 / 后台线程 / 汇报口径 / 菜单接线。

纯逻辑层 ``pet/memory_trim.py`` 零 Qt；本用例**绝不碰本机真实进程**——
ctypes 层（EnumProcesses / OpenProcess / EmptyWorkingSet / CloseHandle）全部
替身化。两组替身：

1. ``FakeApi``：整层替换 ``Win32ProcessApi``（真值表、句柄回收、汇报口径）；
2. 假 ``kernel32`` / ``psapi`` / ``user32`` 模块：喂给真实的
   ``Win32ProcessApi``，钉死访问权限常量、缓冲区解析与"每个 OpenProcess 必有
   CloseHandle"。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication, QMenu

from pet import memory_trim
from pet.context_menus import registry as registry_mod
from pet.context_menus import shared as shared_mod

MB = 1024 * 1024


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _wait_until(app, predicate, *, timeout=10.0, interval=0.02):
    """宽预算条件轮询：worker 在**真线程**里跑、结果经 Qt 队列信号回 GUI 线程，
    固定次数的 ``processEvents`` 赌不到那一次投递（CI 慢 runner 尤甚）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(interval)
    app.processEvents()
    return predicate()


# ---------------------------------------------------------------- 替身：整层 API
class FakeApi:
    """``Win32ProcessApi`` 的行为替身（只实现编排层真正读到的方法）。

    ``open_process`` 只对 ``denied`` 里的 pid 返回 0（权限不足），其余返回递增
    句柄；``close_handle`` / ``empty_working_set`` 全量记录，供"句柄必关"与
    "只对允许的进程下手"两条断言。
    """

    supported = True

    def __init__(self, processes=(), *, denied=(), trim_fail=(), size_fail=(),
                 children=(), foreground=None):
        self.processes = list(processes)
        self.denied = set(denied)
        self.trim_fail = set(trim_fail)
        self.size_fail = set(size_fail)
        self.children = frozenset(children)
        self.foreground = foreground
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.trimmed: list[int] = []
        self._handles: dict[int, int] = {}
        self._next = 0x1000
        self.queried_sizes: list[int] = []

    def handle_for(self, pid: int) -> int:
        handle = self._handles.get(pid)
        if handle is None:
            self._next += 1
            handle = self._next
            self._handles[pid] = handle
        return handle

    def list_pids(self):
        return [entry.pid for entry in self.processes]

    def open_process(self, pid: int) -> int:
        if pid in self.denied:
            return 0
        self.opened.append(pid)
        return self.handle_for(pid)

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)

    def image_name(self, handle: int) -> str:
        for pid, candidate in self._handles.items():
            if candidate == handle:
                return next(
                    entry.name for entry in self.processes if entry.pid == pid
                )
        return ""

    def working_set_bytes(self, handle: int) -> int:
        for pid, candidate in self._handles.items():
            if candidate == handle:
                self.queried_sizes.append(pid)
                if pid in self.size_fail:
                    return -1
                return next(
                    entry.working_set_bytes for entry in self.processes
                    if entry.pid == pid
                )
        return -1

    def empty_working_set(self, handle: int) -> bool:
        for pid, candidate in self._handles.items():
            if candidate == handle:
                if pid in self.trim_fail:
                    return False
                self.trimmed.append(pid)
                return True
        return False

    def foreground_pid(self):
        return self.foreground

    def own_child_pids(self, own_pid: int):
        return self.children


def _entry(pid: int, name: str, size_mb: float, *, child: bool = False):
    return memory_trim.ProcessInfo(
        pid=pid, name=name, working_set_bytes=int(size_mb * MB), is_own_child=child,
    )


# ---------------------------------------------------------------- 真值表
@pytest.mark.parametrize(
    "pid,name,size_mb,child,own_pid,foreground,skip_foreground,"
    "expect_action,expect_reason",
    [
        # 1) 自己：桌宠自己的工作集被抖 = 自己动画卡帧，永远不碰
        (9001, "pythonw.exe", 400, False, 9001, 5555, True, "skip", memory_trim.REASON_SELF),
        # 2) 当前前台：用户正在用的窗口抖出工作集是可感知卡顿，默认跳过
        (5555, "chrome.exe", 800, False, 9001, 5555, True,
         "skip", memory_trim.REASON_FOREGROUND),
        # 3) 同上但关掉 mem_skip_foreground：按正常进程处理
        (5555, "chrome.exe", 800, False, 9001, 5555, False, "trim", memory_trim.REASON_OK),
        # 4) 自己的子进程（解码 ffmpeg 读者）：抖它 = 动画当场掉帧
        (3333, "ffmpeg.exe", 64, True, 9001, 5555, True, "skip", memory_trim.REASON_OWN_CHILD),
        # 5) 系统关键进程（按可执行名识别）
        (700, "csrss.exe", 300, False, 9001, 5555, True,
         "skip", memory_trim.REASON_SYSTEM_CRITICAL),
        (701, "Smss.exe", 12, False, 9001, 5555, True,
         "skip", memory_trim.REASON_SYSTEM_CRITICAL),
        (702, "lsass.exe", 20, False, 9001, 5555, True,
         "skip", memory_trim.REASON_SYSTEM_CRITICAL),
        (703, "dwm.exe", 200, False, 9001, 5555, True,
         "skip", memory_trim.REASON_SYSTEM_CRITICAL),
        (704, "MsMpEng.exe", 500, False, 9001, 5555, True,
         "skip", memory_trim.REASON_SYSTEM_CRITICAL),
        # 6) 内核伪进程（无扩展名 / pid ≤ 4）
        (4, "System", 100, False, 9001, 5555, True,
         "skip", memory_trim.REASON_SYSTEM_CRITICAL),
        (0, "Idle", 0, False, 9001, 5555, True,
         "skip", memory_trim.REASON_SYSTEM_CRITICAL),
        # 7) 小于阈值：对几百个小进程做无用功
        (8000, "tiny.exe", 49, False, 9001, 5555, True,
         "skip", memory_trim.REASON_TOO_SMALL),
        # 8) 边界：恰好等于阈值算达标（含端点）
        (8001, "edge.exe", 50, False, 9001, 5555, True, "trim", memory_trim.REASON_OK),
        # 9) 普通进程：回收
        (8002, "wechat.exe", 300, False, 9001, 5555, True, "trim", memory_trim.REASON_OK),
        # 10) 读不出映像名：认不出关键名单（``_is_critical_name("")`` 恒 False），
        #     按关键保护处理——绝不盲调（与"读不出工作集"同口径）
        (8009, "", 300, False, 9001, 5555, True,
         "skip", memory_trim.REASON_NAME_FAILED),
        (8010, "   ", 300, False, 9001, 5555, True,
         "skip", memory_trim.REASON_NAME_FAILED),
    ],
)
def test_decide_target_truth_table(pid, name, size_mb, child, own_pid, foreground,
                                   skip_foreground, expect_action, expect_reason):
    decision = memory_trim.decide_target(
        _entry(pid, name, size_mb, child=child),
        own_pid=own_pid,
        foreground_pid=foreground,
        min_size_mb=50.0,
        skip_foreground=skip_foreground,
    )

    assert (decision.action, decision.reason) == (expect_action, expect_reason)
    assert (decision.pid, decision.name) == (pid, name)


def test_min_size_threshold_is_editable_and_never_negative():
    entry = _entry(8003, "medium.exe", 30)

    assert memory_trim.decide_target(
        entry, own_pid=1, foreground_pid=None, min_size_mb=10.0, skip_foreground=True,
    ).action == "trim"
    # 脏配置（负数 / 非数值）不得把阈值算成"连 0 字节进程也要折腾"
    assert memory_trim.normalize_min_size_mb(-5) == 0.0
    assert memory_trim.normalize_min_size_mb("abc") == memory_trim.DEFAULT_MIN_SIZE_MB
    assert memory_trim.normalize_min_size_mb(None) == memory_trim.DEFAULT_MIN_SIZE_MB
    assert memory_trim.normalize_min_size_mb(1e9) == memory_trim.MIN_SIZE_MB_MAX


def test_no_foreground_window_never_blocks_trimming():
    """取不到前台窗口（None）时不能把所有进程当成"前台"跳过。"""
    decision = memory_trim.decide_target(
        _entry(8004, "editor.exe", 300),
        own_pid=9001, foreground_pid=None, min_size_mb=50.0, skip_foreground=True,
    )

    assert decision.action == "trim"


# ---------------------------------------------------------------- 编排 + 句柄回收
def _mixed_api(**kwargs):
    processes = [
        _entry(os.getpid(), "pythonw.exe", 400),        # 自己
        _entry(5555, "chrome.exe", 800),                # 前台
        _entry(3333, "ffmpeg.exe", 64, child=True),     # 自己的子进程
        _entry(700, "csrss.exe", 300),                  # 系统关键
        _entry(8000, "tiny.exe", 12),                   # 太小
        _entry(8002, "wechat.exe", 300),                # 正常 → 回收
        _entry(8003, "slack.exe", 500),                 # 正常 → 回收
        _entry(8004, "locked.exe", 700),                # 正常但权限不足
    ]
    return FakeApi(processes, denied={8004}, children={3333}, foreground=5555, **kwargs)


def test_trim_working_sets_only_touches_allowed_processes_and_closes_every_handle():
    api = _mixed_api()

    report = memory_trim.trim_working_sets(api, own_pid=os.getpid(), skip_foreground=True)

    assert api.trimmed == [8002, 8003], "只允许回收：非自己/非前台/非子进程/非关键/够大"
    assert report.trimmed_count == 2
    assert report.skipped_count == 5
    assert report.failed_count == 1
    assert {decision.reason for decision in report.skipped} == {
        memory_trim.REASON_SELF,
        memory_trim.REASON_FOREGROUND,
        memory_trim.REASON_OWN_CHILD,
        memory_trim.REASON_SYSTEM_CRITICAL,
        memory_trim.REASON_TOO_SMALL,
    }
    assert [decision.reason for decision in report.failed] == [
        memory_trim.REASON_OPEN_FAILED
    ]
    # 句柄泄漏检查：每次成功 OpenProcess 都恰好 CloseHandle 一次（含失败/跳过路径）
    assert len(api.closed) == len(api.opened) == 6  # 7 个候选里 1 个打不开
    assert sorted(api.closed) == sorted(api.handle_for(pid) for pid in api.opened)


def test_kernel_pseudo_processes_are_skipped_without_opening():
    """pid 4（System）不是"权限不足"，是内核伪进程：连 OpenProcess 都不做。"""
    processes = [_entry(4, "System", 100), _entry(8002, "wechat.exe", 300)]
    api = FakeApi(processes, children=set())

    report = memory_trim.trim_working_sets(api, own_pid=os.getpid())

    assert api.opened == [8002]
    assert [decision.reason for decision in report.skipped] == [
        memory_trim.REASON_SYSTEM_CRITICAL
    ]
    assert report.failed_count == 0


def test_trim_working_sets_reports_exact_wording():
    report = memory_trim.trim_working_sets(
        _mixed_api(), own_pid=os.getpid(), skip_foreground=True
    )

    assert report.text == "已回收：2 个进程，跳过 5，无权限 1"


def test_foreground_is_detected_when_not_passed_in():
    api = _mixed_api()

    report = memory_trim.trim_working_sets(api, own_pid=os.getpid())

    assert [decision.pid for decision in report.skipped if
            decision.reason == memory_trim.REASON_FOREGROUND] == [5555]


def test_disabling_foreground_skip_trims_the_foreground_process():
    api = _mixed_api()

    report = memory_trim.trim_working_sets(
        api, own_pid=os.getpid(), skip_foreground=False,
    )

    assert api.trimmed == [5555, 8002, 8003]
    assert report.skipped_count == 4


def test_unreadable_working_set_is_counted_as_failure_and_never_trimmed():
    """大小读不出来 → 阈值判据不成立，绝不盲调 EmptyWorkingSet。"""
    processes = [_entry(8002, "wechat.exe", 300), _entry(8005, "ghost.exe", 300)]
    api = FakeApi(processes, size_fail={8005}, denied=set(), children=set())

    report = memory_trim.trim_working_sets(api, own_pid=os.getpid())

    assert api.trimmed == [8002]
    assert [decision.pid for decision in report.failed] == [8005]
    assert report.failed[0].reason == memory_trim.REASON_QUERY_FAILED


def test_unreadable_image_name_is_skipped_and_never_trimmed():
    """名字读不出来 → 认不出关键名单，按关键保护跳过（绝不盲调）。"""
    processes = [_entry(8002, "wechat.exe", 300), _entry(8009, "", 300)]
    api = FakeApi(processes, children=set())

    report = memory_trim.trim_working_sets(api, own_pid=os.getpid())

    assert api.trimmed == [8002]
    assert 8009 not in api.trimmed, "名字读不出的进程不许被回收"
    assert [decision.pid for decision in report.skipped
            if decision.reason == memory_trim.REASON_NAME_FAILED] == [8009]


def test_trim_failure_lands_in_failed_count_without_raising():
    processes = [_entry(8002, "wechat.exe", 300), _entry(8006, "word.exe", 400)]
    api = FakeApi(processes, trim_fail={8006}, denied=set(), children=set())

    report = memory_trim.trim_working_sets(api, own_pid=os.getpid())

    assert api.trimmed == [8002]
    assert [decision.pid for decision in report.failed] == [8006]
    assert report.text == "已回收：1 个进程，跳过 0，无权限 1"


def test_permission_denied_processes_are_silent_not_fatal():
    """权限不足只计数，不抛异常、不弹错误气泡（安静跳过）。"""
    api = FakeApi([_entry(8007, "other-session.exe", 300)],
                  denied={8007}, children=set())

    report = memory_trim.trim_working_sets(api, own_pid=os.getpid())

    assert report.trimmed_count == 0
    assert report.failed_count == 1
    assert api.closed == [], "打不开就没有句柄，不许凭空 CloseHandle"


def test_unsupported_platform_report_has_no_handle_side_effects():
    api = FakeApi([_entry(8002, "wechat.exe", 300)])
    api.supported = False

    report = memory_trim.trim_working_sets(api, own_pid=os.getpid())

    assert report.supported is False
    assert api.opened == []
    assert "Windows" in report.text


def test_is_supported_only_on_windows():
    assert memory_trim.is_supported("win32") is True
    assert memory_trim.is_supported("darwin") is False
    assert memory_trim.is_supported("linux") is False
    assert memory_trim.is_supported() == (sys.platform == "win32")


# ---------------------------------------------------------------- 真 ctypes 薄封装
class _FakeKernel32:
    def __init__(self, *, image_paths, open_fail=(), query_name_fail=()):
        self.image_paths = dict(image_paths)
        self.open_fail = set(open_fail)
        self.query_name_fail = set(query_name_fail)
        self.open_calls: list[tuple] = []
        self.closed: list[int] = []
        self.query_calls: list[int] = []
        self.next_handle = 0x2000
        self.handles: dict[int, int] = {}
        self.toolhelp_snapshot_calls: list[tuple] = []
        self.snapshot_handle = 0x3000
        self.snapshot_entries: list[tuple[int, int]] = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        self.open_calls.append((access, inherit, pid))
        if pid in self.open_fail:
            return 0
        handle = self.handles.get(pid)
        if handle is None:
            self.next_handle += 1
            handle = self.next_handle
            self.handles[pid] = handle
        return handle

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)
        return 1

    def QueryFullProcessImageNameW(self, handle, flags, buffer, size):  # noqa: N802
        self.query_calls.append(handle)
        pid = next((p for p, h in self.handles.items() if h == handle), None)
        if pid in self.query_name_fail or pid not in self.image_paths:
            return 0
        buffer.value = self.image_paths[pid]
        size._obj.value = len(buffer.value)
        return 1

    def CreateToolhelp32Snapshot(self, flags, pid):  # noqa: N802
        self.toolhelp_snapshot_calls.append((flags, pid))
        return self.snapshot_handle

    def Process32FirstW(self, handle, entry):  # noqa: N802
        self._snapshot_index = 0
        return self._fill_snapshot_entry(entry)

    def Process32NextW(self, handle, entry):  # noqa: N802
        return self._fill_snapshot_entry(entry)

    def _fill_snapshot_entry(self, entry):
        entries = self.snapshot_entries
        index = getattr(self, "_snapshot_index", 0)
        if index >= len(entries):
            return 0
        pid, parent = entries[index]
        target = entry._obj
        target.dwSize = ctypes.sizeof(memory_trim._ProcessEntry32W)
        target.th32ProcessID = pid
        target.th32ParentProcessID = parent
        target.szExeFile = f"proc-{pid}.exe"
        self._snapshot_index = index + 1
        return 1


class _FakePsapi:
    def __init__(self, pids=(), sizes=None, *, fail_empty=()):
        self.pids = list(pids)
        self.sizes = dict(sizes or {})
        self.fail_empty = set(fail_empty)
        self.empty_calls: list[int] = []
        self._handles: dict[int, int] = {}

    def EnumProcesses(self, buffer, size, needed):  # noqa: N802
        count = min(len(self.pids), len(buffer))
        for index in range(count):
            buffer[index] = self.pids[index]
        needed._obj.value = count * ctypes.sizeof(ctypes.c_uint32)
        return 1

    def GetProcessMemoryInfo(self, handle, counters, size):  # noqa: N802
        pid = next((p for p, h in self._handles.items() if h == handle), None)
        value = self.sizes.get(pid)
        if value is None:
            return 0
        counters._obj.WorkingSetSize = value
        return 1

    def EmptyWorkingSet(self, handle):  # noqa: N802
        pid = next((p for p, h in self._handles.items() if h == handle), None)
        if pid in self.fail_empty:
            return 0
        self.empty_calls.append(pid)
        return 1


class _FakeUser32:
    def __init__(self, foreground_hwnd=0, window_pid=0):
        self.foreground_hwnd = foreground_hwnd
        self.window_pid = window_pid

    def GetForegroundWindow(self):  # noqa: N802
        return self.foreground_hwnd

    def GetWindowThreadProcessId(self, hwnd, out):  # noqa: N802
        out._obj.value = self.window_pid
        return 1


def _real_api(kernel32, psapi, user32=None):
    return memory_trim.Win32ProcessApi(
        kernel32=kernel32, psapi=psapi, user32=user32 or _FakeUser32(),
    )


def _wire_handles(kernel32, psapi):
    psapi._handles = kernel32.handles


def test_win32_api_uses_minimal_access_rights_and_closes_every_handle():
    kernel32 = _FakeKernel32(
        image_paths={11: r"C:\a\chrome.exe", 22: r"C:\b\locked.exe"},
        open_fail={22},
    )
    psapi = _FakePsapi([11, 22], sizes={11: 300 * MB})
    _wire_handles(kernel32, psapi)
    api = _real_api(kernel32, psapi)

    report = memory_trim.trim_working_sets(api, own_pid=999, foreground_pid=None)

    assert [call[0] for call in kernel32.open_calls] == [
        memory_trim.TRIM_ACCESS, memory_trim.TRIM_ACCESS,
    ]
    assert memory_trim.TRIM_ACCESS == (
        memory_trim.PROCESS_QUERY_LIMITED_INFORMATION | memory_trim.PROCESS_SET_QUOTA
    ), "EmptyWorkingSet 要求 QUERY_* + SET_QUOTA，不许升级成 PROCESS_ALL_ACCESS"
    assert kernel32.closed == [kernel32.snapshot_handle, kernel32.handles[11]], (
        "进程树快照句柄与进程句柄都必须收尾（失败路径不例外）"
    )
    assert psapi.empty_calls == [11]
    assert report.text == "已回收：1 个进程，跳过 0，无权限 1"


def test_win32_api_parses_enumeration_names_and_working_set():
    kernel32 = _FakeKernel32(
        image_paths={11: r"C:\Program Files\Google\Chrome\chrome.exe", 22: r"C:\b\wechat.exe"},
    )
    psapi = _FakePsapi([0, 11, 22], sizes={11: 300 * MB, 22: 800 * MB})
    _wire_handles(kernel32, psapi)
    api = _real_api(kernel32, psapi)

    assert api.list_pids() == [11, 22], "0（Idle/数组填充槽）丢弃，4（System）交给过滤层"
    handle = api.open_process(11)
    assert handle == kernel32.handles[11]
    assert api.image_name(handle) == "chrome.exe", "关键进程名单按可执行名比对，取 basename"
    assert api.working_set_bytes(handle) == 300 * MB
    api.close_handle(handle)


def test_win32_api_reports_unknown_size_for_unqueryable_process():
    kernel32 = _FakeKernel32(image_paths={})
    psapi = _FakePsapi([11], sizes={})
    _wire_handles(kernel32, psapi)
    api = _real_api(kernel32, psapi)

    handle = api.open_process(11)

    assert api.image_name(handle) == ""
    assert api.working_set_bytes(handle) < 0, "读不到大小必须给负值标记（不得当成 0 字节）"


def test_win32_api_foreground_pid_reads_user32():
    kernel32 = _FakeKernel32(image_paths={11: r"C:\a\chrome.exe"})
    psapi = _FakePsapi([11], sizes={11: 300 * MB})
    _wire_handles(kernel32, psapi)
    api = _real_api(kernel32, psapi, _FakeUser32(foreground_hwnd=0x1234, window_pid=11))

    assert api.foreground_pid() == 11
    assert _real_api(kernel32, psapi, _FakeUser32(foreground_hwnd=0)).foreground_pid() is None


def test_own_child_pids_walks_the_process_tree():
    kernel32 = _FakeKernel32(image_paths={1: r"C:\a\pythonw.exe"})
    kernel32.snapshot_entries = [(1, 0), (2, 1), (3, 2), (4, 1), (5, 99)]
    api = _real_api(kernel32, _FakePsapi([1]))

    assert api.own_child_pids(1) == frozenset({2, 3, 4}), "只认自己的后代，不认别人的子进程"
    assert kernel32.toolhelp_snapshot_calls == [(memory_trim.TH32CS_SNAPPROCESS, 0)]


def test_own_child_pids_is_empty_when_snapshot_fails():
    kernel32 = _FakeKernel32(image_paths={})
    kernel32.snapshot_handle = memory_trim._INVALID_HANDLE
    api = _real_api(kernel32, _FakePsapi([1]))

    assert api.own_child_pids(1) == frozenset(), "快照失败只失去 extra 保护，不得抛"


# --------------------------------------------------- 句柄按指针语义判定（m6 回归）
class _SigFunction:
    """可声明 ``argtypes`` / ``restype`` 的假 Win32 函数（真 ctypes 函数支持这两项）。

    只有把函数做成**实例属性**（而非绑定方法）才能设这两个属性——绑定方法会抛
    AttributeError，而 ``_configure_signatures`` 遇到它就会放弃剩余声明，于是
    "签名没补齐"这件事在旧替身上根本测不出来。
    """

    def __init__(self, impl) -> None:
        self._impl = impl
        self.argtypes = None
        self.restype = None
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self._impl(*args)


class _SigLibrary:
    def __init__(self, **functions) -> None:
        for name, impl in functions.items():
            setattr(self, name, _SigFunction(impl))

    def fn(self, name: str) -> _SigFunction:
        return getattr(self, name)


def _signature_tracking_api(snapshot_raw=0x3000):
    """三库全套替身（每个函数都能记录签名声明），快照返回值可注入。"""
    kernel32 = _SigLibrary(
        OpenProcess=lambda access, inherit, pid: 0x1234,
        CloseHandle=lambda handle: 1,
        QueryFullProcessImageNameW=lambda handle, flags, buffer, size: 0,
        CreateToolhelp32Snapshot=lambda flags, pid: snapshot_raw,
        Process32FirstW=lambda handle, entry: 0,
        Process32NextW=lambda handle, entry: 0,
    )
    psapi = _SigLibrary(
        EnumProcesses=lambda buffer, size, needed: 0,
        GetProcessMemoryInfo=lambda handle, counters, size: 0,
        EmptyWorkingSet=lambda handle: 0,
    )
    user32 = _SigLibrary(
        GetForegroundWindow=lambda: 0,
        GetWindowThreadProcessId=lambda hwnd, out: 0,
    )
    api = memory_trim.Win32ProcessApi(
        kernel32=kernel32, psapi=psapi, user32=user32,
    )
    return api, kernel32, psapi, user32


def test_snapshot_functions_declare_pointer_sized_signatures():
    """进程树快照三件套必须显式声明签名（否则句柄按 32 位 c_long 截断）。

    ``CreateToolhelp32Snapshot`` 返回句柄/``INVALID_HANDLE_VALUE``，默认 restype
    （``c_int``）会把 64 位值截断：高位句柄丢失、``-1`` 可能读成正数，于是
    "快照失败"被当成有效句柄——快照句柄泄漏 + 后续 Process32* 拿着垃圾句柄。
    """
    _api, kernel32, _psapi, _user32 = _signature_tracking_api()

    snapshot = kernel32.fn("CreateToolhelp32Snapshot")
    assert snapshot.restype is ctypes.c_void_p, "句柄必须按指针接收（同 OpenProcess）"
    assert snapshot.argtypes == [ctypes.c_uint32, ctypes.c_uint32]
    assert ctypes.sizeof(snapshot.restype) == ctypes.sizeof(ctypes.c_void_p) > 4

    for name in ("Process32FirstW", "Process32NextW"):
        spec = kernel32.fn(name)
        assert spec.restype is ctypes.c_int, "BOOL 返回值"
        assert spec.argtypes == [
            ctypes.c_void_p, ctypes.POINTER(memory_trim._ProcessEntry32W),
        ], "快照句柄 + LPPROCESSENTRY32W 必须显式声明"


@pytest.mark.parametrize(
    "raw", [-1, memory_trim._INVALID_HANDLE, 0xFFFFFFFF, None, 0],
)
def test_invalid_snapshot_returns_empty_without_closing_anything(raw):
    """``INVALID_HANDLE_VALUE`` 的判定不许赌"当前机器恰好返回 -1"。

    符号位随平台/转换路径变化（``c_int`` 截断给 -1、指针语义给 0xFFFF...FFFF），
    两种表示都必须判为失效：返回空集合，且**绝不**对失效句柄 CloseHandle。
    """
    api, kernel32, _psapi, _user32 = _signature_tracking_api(snapshot_raw=raw)

    assert api.own_child_pids(1) == frozenset()
    assert kernel32.fn("CloseHandle").calls == [], "失效句柄不得进 CloseHandle"
    assert kernel32.fn("Process32FirstW").calls == [], "失效句柄不得进 Process32FirstW"


def test_high_bit_snapshot_handle_is_still_valid():
    """反过来也要成立：合法的高位句柄不得被"一律当失效"误杀。"""
    high_bit = 0x7FF0000000001000
    api, kernel32, _psapi, _user32 = _signature_tracking_api(snapshot_raw=high_bit)
    first, next_ = _snapshot_walker([(1, 0), (2, 1)])
    kernel32.Process32FirstW._impl = first
    kernel32.Process32NextW._impl = next_

    assert api.own_child_pids(1) == frozenset({2})
    assert kernel32.fn("CloseHandle").calls == [(high_bit,)], "合法句柄必须收尾"


def _snapshot_walker(entries):
    """``First``/``Next`` 共用一个游标（真 Win32 的进程枚举就是一个游标）。

    两个独立游标是错的替身：First 会把整张表走完、Next 立刻返回 0，等于把
    "快照里只有第一个进程"喂给被测代码。
    """
    state = {"index": 0}

    def _fill(entry):
        if state["index"] >= len(entries):
            return 0
        pid, parent = entries[state["index"]]
        state["index"] += 1
        target = entry._obj
        target.dwSize = ctypes.sizeof(memory_trim._ProcessEntry32W)
        target.th32ProcessID = pid
        target.th32ParentProcessID = parent
        return 1

    def first(handle, entry):
        state["index"] = 0
        return _fill(entry)

    def next_(handle, entry):
        return _fill(entry)

    return first, next_


def test_invalid_handle_judgment_covers_both_representations():
    """判定函数本身：NULL / -1 / 无符号全 1 / 32 位全 1 失效，合法高位句柄有效。"""
    invalid = memory_trim._invalid_handle

    assert invalid(0) is True
    assert invalid(None) is True
    assert invalid(-1) is True
    assert invalid(memory_trim._INVALID_HANDLE) is True
    assert invalid(0xFFFFFFFF) is True, "32 位全 1（截断形态）同样是失效句柄"
    assert invalid(0x7FF0000000001000) is False
    assert invalid(0x1234) is False


def test_process_memory_counters_layout_matches_win32_header():
    """cb 会交给 API 校验，结构大小错一位就是垃圾数据/调用失败。"""
    assert ctypes.sizeof(memory_trim._ProcessMemoryCounters) == (
        8 + 8 * ctypes.sizeof(ctypes.c_size_t)
    )
    assert memory_trim._ProcessMemoryCounters._fields_[0][0] == "cb"


def test_min_size_bound_matches_config_clamp():
    """执行层上限与 config 归一化上限必须同值（两处字面量，靠本断言防漂移）。

    归一化行为本身在 ``tests/test_config_schema.py``（脏值族）里逐条钉住。
    """
    assert memory_trim.MIN_SIZE_MB_MAX == 8192.0


# ---------------------------------------------------------------- 后台线程
def test_run_in_background_trims_off_the_calling_thread():
    seen: dict = {}
    done = threading.Event()
    api = _mixed_api()

    def on_report(report):
        seen["report"] = report
        seen["thread"] = threading.current_thread().name
        done.set()

    api_thread = os.getpid()
    thread = memory_trim.run_in_background(
        on_report, own_pid=api_thread, min_size_mb=50.0, skip_foreground=True,
        api_factory=lambda: api,
    )

    assert thread.daemon is True
    assert thread.name == "memory-trim"
    assert done.wait(10.0), "worker 未在预算内收工"
    assert seen["thread"] == "memory-trim"
    assert seen["thread"] != threading.main_thread().name
    assert seen["report"].trimmed_count == 2
    assert api.trimmed == [8002, 8003]


def test_run_in_background_turns_unexpected_errors_into_a_report():
    done = threading.Event()
    seen: dict = {}

    def explode():
        raise OSError("psapi.dll 加载失败")

    def on_report(report):
        seen["report"] = report
        done.set()

    memory_trim.run_in_background(on_report, api_factory=explode)

    assert done.wait(10.0)
    assert seen["report"].error
    assert seen["report"].text.startswith("回收内存失败")


def test_run_in_background_survives_a_broken_callback():
    """回调里抛异常不许让 worker 线程以未捕获异常收尾（daemon 线程炸了没人看见）。"""
    done = threading.Event()

    def on_report(report):
        done.set()
        raise RuntimeError("宿主窗口已销毁")

    thread = memory_trim.run_in_background(on_report, api_factory=lambda: FakeApi())

    assert done.wait(10.0)
    thread.join(10.0)
    assert not thread.is_alive()


# ---------------------------------------------------------------- 菜单接线
class _Cfg:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save(self):
        return None


class _Pet:
    """菜单建造器的最小宿主（气泡替身记录汇报文案）。

    ``context_menus`` 的两套建造器要读的宿主面很宽（动画清单 / 速率 / 尺寸 /
    各窗口能力回调），这里只显式给出**必须是真的值**的那几个（清单、数值、
    布尔），其余经 ``__getattr__`` 兜成 no-op 可调用对象——与被测行为无关的
    面保持"存在即可"。
    """

    def __init__(self, values=None):
        self.cfg = _Cfg(values)
        self.bubbles: list[str] = []
        self.idles = ["待机"]
        self.turns: list[str] = []
        self.moves: list[str] = []
        self.clicks: list[str] = []
        self.acts: list[str] = []
        self.playback_speed = 1.0
        self.scale = 1.0
        self.drag_physics = False
        self.no_move = False

    def show_bubble(self, text, duration_ms=3200, **_kwargs):
        self.bubbles.append(text)

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def test_registry_registers_memory_trim_with_a_windows_gate(monkeypatch):
    assert "memory_trim" in registry_mod.MENU_ACTIONS.ids
    assert registry_mod.ACTION_LABELS["memory_trim"] == "回收内存"
    pet = _Pet()

    monkeypatch.setattr(sys, "platform", "win32")
    assert "memory_trim" in registry_mod.MENU_ACTIONS.available_ids(pet)

    monkeypatch.setattr(sys, "platform", "darwin")
    assert "memory_trim" not in registry_mod.MENU_ACTIONS.available_ids(pet)


def test_modern_menu_offers_memory_trim_on_windows_only(app, monkeypatch):
    from pet.context_menu import populate_context_menu

    pet = _Pet({"context_menu_layout": None, "context_menu_appearance": {}})
    monkeypatch.setattr(shared_mod.autostart_mod, "is_enabled", lambda: False)

    monkeypatch.setattr(sys, "platform", "win32")
    menu = QMenu()
    populate_context_menu(menu, pet)
    windows_labels = [action.text() for action in menu.actions()]
    menu.close()

    monkeypatch.setattr(sys, "platform", "darwin")
    other_menu = QMenu()
    populate_context_menu(other_menu, pet)
    other_labels = [action.text() for action in other_menu.actions()]
    other_menu.close()

    assert "回收内存" in windows_labels
    assert "回收内存" not in other_labels
    app.processEvents()


def test_shared_builder_offers_memory_trim_on_windows_only(app, monkeypatch):
    """平台门在**共享建造器**里（供任何直调它的入口复用），不只是注册表判定。"""
    from pet.context_menus.shared import add_memory_trim

    pet = _Pet()
    monkeypatch.setattr(shared_mod.autostart_mod, "is_enabled", lambda: False)

    monkeypatch.setattr(sys, "platform", "win32")
    menu = QMenu()
    add_memory_trim(menu, pet)
    windows_labels = [action.text() for action in menu.actions()]
    menu.close()

    monkeypatch.setattr(sys, "platform", "darwin")
    other_menu = QMenu()
    add_memory_trim(other_menu, pet)
    other_labels = [action.text() for action in other_menu.actions()]
    other_menu.close()

    assert "回收内存" in windows_labels
    assert "回收内存" not in other_labels
    app.processEvents()


def test_menu_click_passes_config_to_the_background_worker(app, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        memory_trim, "run_in_background",
        lambda on_report, **kwargs: calls.append(kwargs),
    )

    pet = _Pet({"mem_min_size_mb": 120, "mem_skip_foreground": False})
    menu = QMenu()
    action = shared_mod.add_memory_trim(menu, pet)
    action.trigger()

    assert calls == [{"min_size_mb": 120, "skip_foreground": False}]
    menu.close()
    app.processEvents()


def test_menu_click_uses_documented_defaults_without_config(app, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        memory_trim, "run_in_background",
        lambda on_report, **kwargs: calls.append(kwargs),
    )

    pet = _Pet()
    menu = QMenu()
    shared_mod.add_memory_trim(menu, pet).trigger()

    assert calls == [{"min_size_mb": memory_trim.DEFAULT_MIN_SIZE_MB,
                      "skip_foreground": True}]
    menu.close()
    app.processEvents()


def test_finished_report_reaches_the_pet_bubble(app, monkeypatch):
    """汇报走既有气泡通道（show_bubble），文案即 report.text。"""
    monkeypatch.setattr(sys, "platform", "win32")

    def fake_run(on_report, **_kwargs):
        on_report(memory_trim.TrimReport(
            trimmed=(memory_trim.TrimDecision(1, "a.exe", 1, "trim", "ok"),),
            skipped=(memory_trim.TrimDecision(2, "b.exe", 1, "skip", "self"),),
            failed=(),
        ))

    monkeypatch.setattr(memory_trim, "run_in_background", fake_run)

    pet = _Pet()
    menu = QMenu()
    shared_mod.add_memory_trim(menu, pet).trigger()
    app.processEvents()

    assert pet.bubbles == ["已回收：1 个进程，跳过 1，无权限 0"]
    menu.close()
    app.processEvents()


def test_unsupported_platform_click_reports_instead_of_crashing(app, monkeypatch):
    """菜单项在非 Windows 已被隐藏；即便被外部调用也只提示不支持。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    # 真机（Windows）上 Win32ProcessApi.supported 是导入期常量；这里按"非 Windows
    # 平台"的语义改掉它，避免用例真的对本机进程下手（红线：单测不碰真实进程）。
    monkeypatch.setattr(memory_trim.Win32ProcessApi, "supported", False)

    pet = _Pet()
    menu = QMenu()
    action = shared_mod.add_memory_trim(menu, pet)
    assert action is None

    shared_mod.trigger_memory_trim(pet)
    # 这条走**真实** run_in_background：报告在 worker 线程产生、经队列信号回 GUI
    # 线程，故等气泡而不是 processEvents 一次就断言。
    assert _wait_until(app, lambda: bool(pet.bubbles)), "不支持平台也该有可见反馈"
    assert "Windows" in pet.bubbles[0]
    menu.close()
    app.processEvents()


def test_config_less_host_still_gets_documented_defaults(app, monkeypatch):
    """宿主连 cfg 都没有（最小外壳/测试替身）时不许在点击路径上抛异常。"""
    calls: list[dict] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        memory_trim, "run_in_background",
        lambda on_report, **kwargs: calls.append(kwargs),
    )

    class BareHost:
        bubbles: list[str] = []

        def show_bubble(self, text, duration_ms=3200, **_kwargs):
            self.bubbles.append(text)

    host = BareHost()
    menu = QMenu()
    shared_mod.add_memory_trim(menu, host).trigger()

    assert calls == [{"min_size_mb": memory_trim.DEFAULT_MIN_SIZE_MB,
                      "skip_foreground": True}]
    menu.close()
    app.processEvents()


class _FakeOverlayShell:
    """overlay 侧最小壳：facade 需要的只有配置视图 + show_bubble。"""

    def __init__(self, values=None):
        self._config = _Cfg(values)
        self.bubbles: list[str] = []

    def _sprite_config(self, _sprite):
        return self._config

    def show_bubble(self, text, duration_ms=3200, **_kwargs):
        self.bubbles.append(text)


def test_overlay_facade_host_reads_its_config_and_reports_to_the_shell(app, monkeypatch):
    """overlay 路径：菜单宿主是 SpriteMenuFacade，配置与气泡都要落到壳上。

    两条菜单路径（窗口 PetWindow / overlay facade）共用同一个建造器，这里钉住
    facade 这条链：宿主 cfg 是壳的配置视图、汇报气泡经 facade.show_bubble 落到
    壳的 show_bubble（与 ``_MusicLaunchBridge`` 同一条既有通道）。
    """
    from pet.sprite_menu_facade import SpriteMenuFacade

    calls: list[dict] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        memory_trim, "run_in_background",
        lambda on_report, **kwargs: (calls.append(kwargs), on_report(memory_trim.TrimReport())),
    )

    shell = _FakeOverlayShell({"mem_min_size_mb": 77, "mem_skip_foreground": False})
    facade = SpriteMenuFacade(shell, sprite=object())
    menu = QMenu()
    shared_mod.add_memory_trim(menu, facade).trigger()
    app.processEvents()

    assert calls == [{"min_size_mb": 77, "skip_foreground": False}]
    assert shell.bubbles == ["已回收：0 个进程，跳过 0，无权限 0"]
    menu.close()
    app.processEvents()
