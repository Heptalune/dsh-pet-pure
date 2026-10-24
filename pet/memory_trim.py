# -*- coding: utf-8 -*-
"""回收其它进程的工作集（issue #210 菜单动作「回收内存」，Windows 专属）。

**纯逻辑层：零 Qt**——本模块只依赖 ``ctypes`` / ``os`` / ``sys`` / ``threading``，
可在无 Qt 环境导入与单测。菜单入口、后台线程投递与气泡汇报在
``pet/context_menus/shared.py``（那边负责跨线程把结果送回 GUI 线程）。

它做什么
--------
``EmptyWorkingSet`` 把某个进程已不用的物理页交还系统（工作集被清后该进程会
按需回读）。对**别的**进程做这件事能立刻腾出内存，代价是被清理的进程下一次
访问那些页时付一次缺页。所以本模块的全部过滤逻辑只服务一句话：
**绝不抖正在被人用的东西**。

安全红线（在本模块内部守住，不依赖调用方）
------------------------------------------
1. **自己**（``own_pid``）永远不碰：抖桌宠自己的工作集 = 当场动画卡帧；
2. **当前前台**进程默认不碰（``skip_foreground``，默认 True）：用户正在用的
   窗口被抖出工作集就是可感知的卡顿；
3. **自己的子进程**不碰（``own_child_pids``）：桌宠的解码 ffmpeg 读者正是自己
   的子进程，抖它的工作集直接表现为掉帧——这条超出 issue 原文，但同属"不给
   自己制造缺页抖动"，是本节唯一一处主动加固；
4. **系统关键进程**不碰（``SYSTEM_CRITICAL_NAMES``，逐条依据见常量注释）；
   **读不出映像名**的进程按关键处理（认不出是不是名单里的进程就当成是，跳过）；
5. 每个成功 ``OpenProcess`` 的句柄都在 ``finally`` 里 ``CloseHandle``——失败与
   提前跳过的路径同样收尾（单测钉死"打开 N 次 = 关闭 N 次"）；
6. 权限不足的进程**安静跳过**并计入失败数，不抛异常、不打断整轮回收。

调用面
------
纯编排：``trim_working_sets(api)`` + ``decide_target(...)``（真值表在单测里）；
真机入口：``run_in_background(on_report, ...)``——后台线程跑一轮，结果经
``on_report`` 交回（在 worker 线程调用，调用方负责投递到 GUI 线程）。
"""
from __future__ import annotations

import ctypes
import logging
import ntpath
import os
import sys
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_MIN_SIZE_MB = 50.0
"""工作集小于该值的进程不折腾（默认 50MB）：几百个小进程逐个 OpenProcess 是
纯无用功，且它们的工作集本身没有可观回收量。"""

MIN_SIZE_MB_MAX = 8192.0
"""阈值上限：手改配置成 1e9 时不该让"全部进程都不够大"这种静默失效发生。"""

# EmptyWorkingSet 的最小必要权限：QUERY_LIMITED_INFORMATION（查询）+
# SET_QUOTA（改工作集）。刻意**不**用 PROCESS_ALL_ACCESS / QUERY_INFORMATION：
# 权限越大越容易在受保护进程上被拒绝，而 QUERY_INFORMATION 只在更少进程上可用。
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
TRIM_ACCESS = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_INVALID_HANDLE_MASK = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
_INVALID_HANDLE_VALUES = frozenset({
    _INVALID_HANDLE,  # 指针宽度全 1：64 位下 0xFFFF…FFFF
    0xFFFFFFFF,  # 32 位机器上的真身，也是 64 位下按 c_int/dword 截断后的样子
})


def _invalid_handle(handle) -> bool:
    """句柄失效判定：NULL 与 ``INVALID_HANDLE_VALUE`` 都算，按**指针语义**比较。

    ctypes 把 NULL 给成 ``None``、把非空指针给成 ``int``，而那个 int 的取值取决于
    转换路径（``restype=c_int`` 截断出 -1/0xFFFFFFFF，指针语义给出 0xFFFF…FFFF）
    ——所以先按指针宽度取模，再比全部已知的失效表示，不赌"当前机器恰好返回哪一个"。
    """
    if not handle:
        return True
    return (int(handle) & _INVALID_HANDLE_MASK) in _INVALID_HANDLE_VALUES


SYSTEM_CRITICAL_NAMES = frozenset({
    # 内核态伪进程：没有真实工作集，OpenProcess 无意义或直接失败
    "system", "idle", "registry", "memory compression", "secure system",
    # 会话/安全/登录/服务控制关键进程：抖动会波及全系统，且多为受保护进程
    "smss", "csrss", "wininit", "services", "lsass", "winlogon",
    # 桌面合成器：抖它 = 整个桌面（含桌宠自己）重绘卡顿
    "dwm",
    # 字体宿主：清空后每个新字形都要重读字库，全系统文本渲染抖动
    "fontdrvhost",
    # Defender 引擎：清掉的工作集会被立刻重扫驻留，纯抖动无收益
    "msmpeng", "nissrv",
})
"""按**可执行名**（casefold、去掉 ``.exe``）比对的跳过名单。

依据：这些进程要么是内核伪进程（工作集概念不成立），要么是关键子系统/安全
/桌面合成/字体/反恶意软件进程——对它们清工作集的收益接近零，而代价是全系统
范围内的重扫与重绘（多个 Windows 内存清理工具的公开实现同样把这一组列为
"不要碰"）。``svchost`` 刻意**不**在名单里：它是服务宿主，清了只付一次缺页，
在少数进程上跳过它等于放弃可观回收量；真正的关键服务由其自身保护机制拒绝。
"""

REASON_SELF = "self"
REASON_FOREGROUND = "foreground"
REASON_OWN_CHILD = "own-child"
REASON_SYSTEM_CRITICAL = "system-critical"
REASON_TOO_SMALL = "too-small"
REASON_OK = "ok"
REASON_OPEN_FAILED = "open-failed"
REASON_QUERY_FAILED = "query-failed"
REASON_NAME_FAILED = "name-failed"
REASON_TRIM_FAILED = "trim-failed"

ACTION_TRIM = "trim"
ACTION_SKIP = "skip"

_UNSET = object()


def is_supported(platform: str | None = None) -> bool:
    """平台分派（菜单可用性 / 编排水印共用同一判定）。"""
    return (sys.platform if platform is None else platform) == "win32"


def normalize_min_size_mb(value) -> float:
    """阈值归一化：脏配置（非数值/负数/超大）一律夹回可用区间。"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_MIN_SIZE_MB
    if number != number:  # NaN
        return DEFAULT_MIN_SIZE_MB
    return max(0.0, min(MIN_SIZE_MB_MAX, number))


def _is_critical_name(name: str) -> bool:
    key = str(name or "").strip().casefold()
    if key.endswith(".exe"):
        key = key[:-4]
    return key in SYSTEM_CRITICAL_NAMES


@dataclass(frozen=True)
class ProcessInfo:
    """一个候选进程（``is_own_child`` 由进程树快照标记）。"""

    pid: int
    name: str
    working_set_bytes: int
    is_own_child: bool = False


@dataclass(frozen=True)
class TrimDecision:
    """单个进程的处置结论（真值表就是本对象的 ``action`` + ``reason``）。"""

    pid: int
    name: str
    working_set_bytes: int
    action: str
    reason: str


@dataclass(frozen=True)
class TrimReport:
    """一轮回收的结果（气泡文案与统计口径的唯一来源）。"""

    trimmed: tuple[TrimDecision, ...] = ()
    skipped: tuple[TrimDecision, ...] = ()
    failed: tuple[TrimDecision, ...] = ()
    supported: bool = True
    error: str = ""

    @property
    def trimmed_count(self) -> int:
        return len(self.trimmed)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def text(self) -> str:
        """气泡文案（口径固定，单测逐字钉住）。"""
        if not self.supported:
            return "回收内存只支持 Windows 系统"
        if self.error:
            return f"回收内存失败：{self.error}"
        return (
            f"已回收：{self.trimmed_count} 个进程，"
            f"跳过 {self.skipped_count}，无权限 {self.failed_count}"
        )


def decide_target(
    entry: ProcessInfo,
    *,
    own_pid: int,
    foreground_pid: int | None,
    min_size_mb: float,
    skip_foreground: bool,
) -> TrimDecision:
    """过滤真值表（顺序即优先级：安全类判定永远先于"够不够大"）。

    先判自己、再判"是不是别人的进程"（子进程/前台）、再判系统关键，最后才
    按大小筛——顺序错了就会出现"小进程因为太小而绕过安全跳过"这类洞。
    名字读不出来（空串）同样归安全类：认不出它是不是关键进程，就按关键处理。
    """
    def _skip(reason: str) -> TrimDecision:
        return TrimDecision(entry.pid, entry.name, entry.working_set_bytes,
                            ACTION_SKIP, reason)

    if entry.pid == own_pid:
        return _skip(REASON_SELF)
    if entry.pid <= 4:  # 0=Idle、4=System：内核伪进程，没有可回收的工作集
        return _skip(REASON_SYSTEM_CRITICAL)
    if entry.is_own_child:
        return _skip(REASON_OWN_CHILD)
    if skip_foreground and foreground_pid is not None and entry.pid == foreground_pid:
        return _skip(REASON_FOREGROUND)
    if _is_critical_name(entry.name):
        return _skip(REASON_SYSTEM_CRITICAL)
    if not str(entry.name or "").strip():
        # 名字读不出来（``QueryFullProcessImageName`` 失败）：关键名单比对不了，
        # 而读不出名字的多是受保护/系统进程。按关键保护处理——绝不盲调
        # （与"读不出工作集"同口径：判据不成立就不动手）。
        return _skip(REASON_NAME_FAILED)
    if entry.working_set_bytes < normalize_min_size_mb(min_size_mb) * 1024 * 1024:
        return _skip(REASON_TOO_SMALL)
    return TrimDecision(entry.pid, entry.name, entry.working_set_bytes,
                        ACTION_TRIM, REASON_OK)


class _ProcessMemoryCounters(ctypes.Structure):
    """``PROCESS_MEMORY_COUNTERS``（psapi.h）逐字段对齐。

    字段宽度必须与头文件一致：``cb`` 与实际结构大小一起交给 API，错一位就是
    垃圾数据或调用失败。``cb`` 之外的字段顺序不可重排。
    """

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _ProcessEntry32W(ctypes.Structure):
    """``PROCESSENTRY32W``（tlhelp32.h）：只用到 pid / 父 pid，但字段必须齐全，
    否则 ``dwSize`` 与实际布局不符、``Process32FirstW`` 直接失败。"""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _load_library(name: str):
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("当前平台没有 WinDLL（回收内存仅支持 Windows）")
    return loader(name, use_last_error=True)


class Win32ProcessApi:
    """kernel32 / psapi / user32 的 ctypes 薄封装。

    单测用两种替身覆盖两层：①``FakeApi``（同名方法）替换整层；②假
    ``kernel32``/``psapi``/``user32`` 模块喂给本类，连同缓冲区解析与
    "打开必关闭"一起走真实代码路径。
    """

    #: 类级判定按**宿主平台**：``run_in_background`` 在构造之前就用它决定要不要开工。
    supported = sys.platform == "win32"

    def __init__(self, kernel32=None, psapi=None, user32=None) -> None:
        self._kernel32 = kernel32 if kernel32 is not None else _load_library("kernel32")
        self._psapi = psapi if psapi is not None else _load_library("psapi")
        self._user32 = user32 if user32 is not None else _load_library("user32")
        if kernel32 is not None and psapi is not None:
            # 显式注入了 kernel32 + psapi：这一份实例自带整套 Win32 面（替身走的就是这条
            # 路），而本类的职责——缓冲区解析、句柄按指针语义判定、"打开必关闭"——与宿主
            # 平台无关。类级常量是"这台机器能不能自己加载 Win32 库"，对注入了库的实例不成立。
            self.supported = True
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        """显式声明 restype：HANDLE 是 64 位指针，默认 ``c_int`` 返回值会把高位
        截断（截断后的"句柄"传回 Win32 是随机行为，不是无害的）。"""
        k32, psapi, user32 = self._kernel32, self._psapi, self._user32
        try:
            k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle.restype = ctypes.c_int
            k32.QueryFullProcessImageNameW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            k32.QueryFullProcessImageNameW.restype = ctypes.c_int
            # 进程树快照三件套：句柄一律走 c_void_p（同 OpenProcess/CloseHandle），
            # 默认 restype=c_int 会把 64 位句柄截断、并让 INVALID_HANDLE_VALUE 的
            # 判定依赖"当前机器恰好返回 -1"——那正是快照句柄泄漏/误判的来源。
            k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
            k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            k32.Process32FirstW.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W),
            ]
            k32.Process32FirstW.restype = ctypes.c_int
            k32.Process32NextW.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W),
            ]
            k32.Process32NextW.restype = ctypes.c_int
            psapi.EnumProcesses.argtypes = [
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            psapi.EnumProcesses.restype = ctypes.c_int
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_uint32,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
            psapi.EmptyWorkingSet.restype = ctypes.c_int
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            user32.GetWindowThreadProcessId.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
            ]
            user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        except AttributeError:  # pragma: no cover - 只可能出现在替身少写方法时
            log.debug("Win32 API 签名声明不完整，回退默认转换", exc_info=True)

    # ------------------------------------------------------------ 枚举/查询
    def list_pids(self) -> list[int]:
        capacity = 4096
        buffer = (ctypes.c_uint32 * capacity)()
        needed = ctypes.c_uint32()
        if not self._psapi.EnumProcesses(
            buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
        ):
            return []
        count = min(int(needed.value) // ctypes.sizeof(ctypes.c_uint32), capacity)
        return [int(pid) for pid in buffer[:count] if pid]

    def open_process(self, pid: int) -> int:
        return self._kernel32.OpenProcess(TRIM_ACCESS, 0, int(pid)) or 0

    def close_handle(self, handle) -> None:
        # 失效句柄不进 CloseHandle：CloseHandle(INVALID_HANDLE_VALUE) 是未定义行为
        # （调试器下会抛异常），NULL 也必须静默跳过。
        if not _invalid_handle(handle):
            self._kernel32.CloseHandle(handle)

    def image_name(self, handle) -> str:
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(len(buffer))
        if not self._kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        # 路径是 Win32 给的，分隔符**永远**是反斜杠：取 basename 必须用 ``ntpath``，用
        # ``os.path`` 在 POSIX 宿主上会把 ``C:\x\chrome.exe`` 整条原样返回（关键进程名单
        # 按可执行名比对，于是比对全部落空）。Windows 上两者等价，行为不变。
        return ntpath.basename(buffer.value or "")

    def working_set_bytes(self, handle) -> int:
        """读不到返回 ``-1``（**不是** 0）：0 会被当成"小于阈值"而掩盖查询失败。"""
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not self._psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        ):
            return -1
        return int(counters.WorkingSetSize)

    def empty_working_set(self, handle) -> bool:
        return bool(self._psapi.EmptyWorkingSet(handle))

    def foreground_pid(self) -> int | None:
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_uint32()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None

    # ------------------------------------------------------------ 自己的进程树
    def own_child_pids(self, own_pid: int) -> frozenset[int]:
        """自己进程的全部后代 pid（供"不许抖自己的解码子进程"用）。

        快照失败只意味着少一层额外保护——返回空集合，**不**升级成异常：宁可
        多清理几个进程，也不让整轮回收因为一次快照失败而中断。
        """
        handle = self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if _invalid_handle(handle):
            return frozenset()
        try:
            parents: dict[int, int] = {}
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            ok = self._kernel32.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = self._kernel32.Process32NextW(handle, ctypes.byref(entry))
        except Exception:  # pragma: no cover - 快照异常同样降级为"没有额外保护"
            log.debug("进程树快照解析失败", exc_info=True)
            return frozenset()
        finally:
            self.close_handle(handle)

        children: set[int] = set()
        frontier = [int(own_pid)]
        while frontier:
            current = frontier.pop()
            for pid, parent in parents.items():
                if parent == current and pid not in children and pid != own_pid:
                    children.add(pid)
                    frontier.append(pid)
        return frozenset(children)


def trim_working_sets(
    api,
    *,
    own_pid: int | None = None,
    foreground_pid=_UNSET,
    min_size_mb: float = DEFAULT_MIN_SIZE_MB,
    skip_foreground: bool = True,
) -> TrimReport:
    """枚举 → 过滤 → OpenProcess → EmptyWorkingSet → 统计（纯编排）。

    ``foreground_pid`` 省略（``_UNSET``）时向 ``api`` 查询当前前台窗口归属。
    逐进程的失败（打不开 / 读不到大小 / 清失败）一律计入 ``failed``，从不抛出
    ——一轮回收永远要给用户一个可汇报的结果。
    """
    if not getattr(api, "supported", True):
        return TrimReport(supported=False)

    own_pid = os.getpid() if own_pid is None else int(own_pid)
    if foreground_pid is _UNSET:
        try:
            foreground_pid = api.foreground_pid()
        except Exception:
            log.debug("取前台窗口失败，按无前台处理", exc_info=True)
            foreground_pid = None
    try:
        children = set(api.own_child_pids(own_pid))
    except Exception:
        log.debug("取自身子进程失败，跳过额外保护", exc_info=True)
        children = set()

    trimmed: list[TrimDecision] = []
    skipped: list[TrimDecision] = []
    failed: list[TrimDecision] = []

    for pid in api.list_pids():
        pid = int(pid)
        if pid == own_pid:
            # 自己：**连 OpenProcess 都不做**（比"打开后不调 EmptyWorkingSet"更硬的
            # 保证），所以这里在句柄逻辑之外单列一条。decide_target 里同样有自己
            # 的规则，两层是刻意的（直调 decide_target 的调用点也拿得到同一结论）。
            skipped.append(TrimDecision(pid, "", 0, ACTION_SKIP, REASON_SELF))
            continue
        if pid <= 4:
            # 0=Idle / 4=System：内核伪进程，工作集概念不成立，连打开都不做
            # （否则它们会以"无权限"混进失败数，掩盖真正的权限问题）。
            skipped.append(TrimDecision(pid, "", 0, ACTION_SKIP, REASON_SYSTEM_CRITICAL))
            continue
        handle = 0
        try:
            handle = api.open_process(pid)
        except Exception:
            handle = 0
        if not handle:
            # 权限不足（受保护进程 / 其它会话 / 需要提权）：安静跳过，只计数
            failed.append(TrimDecision(pid, "", 0, ACTION_TRIM, REASON_OPEN_FAILED))
            continue
        try:
            name = api.image_name(handle)
            size = api.working_set_bytes(handle)
            if size < 0:
                failed.append(TrimDecision(pid, name, -1, ACTION_TRIM, REASON_QUERY_FAILED))
                continue
            decision = decide_target(
                ProcessInfo(pid, name, size, is_own_child=pid in children),
                own_pid=own_pid,
                foreground_pid=foreground_pid,
                min_size_mb=min_size_mb,
                skip_foreground=skip_foreground,
            )
            if decision.action == ACTION_SKIP:
                skipped.append(decision)
                continue
            try:
                ok = bool(api.empty_working_set(handle))
            except Exception:
                log.debug("EmptyWorkingSet 调用异常 pid=%s", pid, exc_info=True)
                ok = False
            (trimmed if ok else failed).append(
                decision if ok
                else TrimDecision(pid, name, size, ACTION_TRIM, REASON_TRIM_FAILED)
            )
        finally:
            api.close_handle(handle)

    return TrimReport(tuple(trimmed), tuple(skipped), tuple(failed))


def run_in_background(
    on_report,
    *,
    min_size_mb: float = DEFAULT_MIN_SIZE_MB,
    skip_foreground: bool = True,
    own_pid: int | None = None,
    api_factory=None,
) -> threading.Thread:
    """后台线程跑一轮回收，收工后把 :class:`TrimReport` 交给 ``on_report``。

    枚举几百个进程 + 逐个 OpenProcess 是慢操作（毫秒级 × 进程数），**绝不能**
    在 GUI 线程跑——菜单点击路径必须立刻返回。``on_report`` 在 **worker 线程**
    被调用，调用方负责投递给 GUI 线程（Qt 信号队列，见
    ``pet/context_menus/shared.py::_MemoryTrimBridge``）。

    异常一律转成 ``error`` 报告：worker 里漏出去就是 daemon 线程的未捕获异常，
    对用户表现为"点了没反应"。
    """
    factory = api_factory or Win32ProcessApi

    def worker() -> None:
        report: TrimReport
        if not getattr(factory, "supported", True):
            report = TrimReport(supported=False)
        else:
            try:
                api = factory()
                report = trim_working_sets(
                    api,
                    own_pid=os.getpid() if own_pid is None else own_pid,
                    min_size_mb=min_size_mb,
                    skip_foreground=skip_foreground,
                )
            except Exception as exc:
                log.exception("回收内存失败")
                report = TrimReport(error=str(exc) or exc.__class__.__name__)
        try:
            on_report(report)
        except Exception:
            log.debug("回收内存汇报回调失败", exc_info=True)

    thread = threading.Thread(target=worker, name="memory-trim", daemon=True)
    thread.start()
    return thread
