# -*- coding: utf-8 -*-
"""内存归因探针（诊断专用）：直接以 python -m pet 的语义启动产品进程，旁路采样。

设计约束（对齐 docs/DEV-HANDOVER.md 的证据纪律）：

- **不改产品代码**：本文件是启动器，先起采样器再 ``from pet.app import main``，
  产品侧零 hook、零 env 判定分支。
- **两种模式，口径分开**：
  - ``PET_MEM_TRACE_MODE=ws``：只采样 WorkingSet / PrivateBytes + Qt 对象普查。
    tracemalloc **不开**（它自己的 traceback 记账会抬高进程 WS），用于
    权威 before/after 数字。
  - ``PET_MEM_TRACE_MODE=trace``：额外开 tracemalloc（25 帧）+ 定期快照落盘，
    用于"哪段代码分配了多少 Python 堆"。此模式下 WS 偏大，只作归因、不作数字。
- **进程外与进程内同源**：WS 由进程内 ctypes GetProcessMemoryInfo 读（无第三方
  依赖，避免 psutil import 自身的内存污染），每 ``SAMPLE_S`` 秒 append 一行 CSV，
  进程被强杀也留着已采到的曲线。
- **Qt 对象普查在 GUI 线程做**：QPixmap 只能在其线程使用；普查经
  ``QMetaObject.invokeMethod`` 排队到主线程执行，采样线程只搬数据。

环境变量：
  PET_MEM_TRACE_DIR      产物目录（必填，否则落在 .scratch/mem-probe/adhoc）
  PET_MEM_TRACE_MODE     ws | trace      （默认 ws）
  PET_MEM_TRACE_LABEL    运行标签（写进 summary）
  PET_MEM_TRACE_SECONDS  自动退出时长秒（默认 200）
  PET_MEM_TRACE_SAMPLE_S 采样间隔秒（默认 2）
  PET_MEM_TRACE_SNAP_S   tracemalloc 快照间隔秒（默认 20）
"""

from __future__ import annotations

import csv
import ctypes
import gc
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = Path(os.environ.get('PET_MEM_TRACE_DIR') or (ROOT / '.scratch' / 'mem-probe' / 'adhoc'))
MODE = (os.environ.get('PET_MEM_TRACE_MODE') or 'ws').strip().lower()
LABEL = os.environ.get('PET_MEM_TRACE_LABEL') or 'run'
SECONDS = float(os.environ.get('PET_MEM_TRACE_SECONDS') or 200)
SAMPLE_S = float(os.environ.get('PET_MEM_TRACE_SAMPLE_S') or 2)
SNAP_S = float(os.environ.get('PET_MEM_TRACE_SNAP_S') or 20)

OUT_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH = OUT_DIR / 'ws.csv'
SUMMARY_PATH = OUT_DIR / 'summary.json'
SNAP_DIR = OUT_DIR / 'snap'

CSV_FIELDS = [
    't', 'ws_mb', 'peak_ws_mb', 'private_mb', 'handles', 'threads', 'children',
    'tm_current_mb', 'tm_peak_mb', 'py_blocks',
    'qimage_n', 'qimage_mb', 'qpixmap_n', 'qwidget_n', 'clip_n',
    'clip_queue_frames', 'clip_queue_mb', 'clip_first_mb', 'clip_curr_mb',
    'clip_listed_frames', 'live_readers',
]


# ------------------------------------------------------------------ 进程内存
class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('cb', ctypes.c_uint32),
        ('PageFaultCount', ctypes.c_uint32),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
    ]


def _make_mem_reader():
    """返回 () -> dict；非 Windows 退化为 resource（口径不同，报告里注明）。"""
    if sys.platform == 'win32':
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), ctypes.c_uint32,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int

        def _read():
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            handle = k32.GetCurrentProcess()
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                raise OSError('GetProcessMemoryInfo failed')
            return {
                'ws_mb': counters.WorkingSetSize / 1048576.0,
                'peak_ws_mb': counters.PeakWorkingSetSize / 1048576.0,
                'private_mb': counters.PagefileUsage / 1048576.0,
            }

        return _read

    import resource

    def _read_posix():
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mb = usage.ru_maxrss / 1024.0 if sys.platform == 'darwin' else usage.ru_maxrss / 1024.0
        return {'ws_mb': mb, 'peak_ws_mb': mb, 'private_mb': 0.0}

    return _read_posix


_read_mem = _make_mem_reader()


def _handle_thread_counts() -> tuple[int, int]:
    """(句柄数, 线程数)；Windows 走 GetProcessHandleCount；失败返回 0。"""
    handles = 0
    try:
        if sys.platform == 'win32':
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            count = ctypes.c_uint32(0)
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            if k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(count)):
                handles = int(count.value)
    except Exception:
        handles = 0
    try:
        threads = threading.active_count()
    except Exception:
        threads = 0
    return handles, threads


def _child_count() -> int:
    """当前进程的子孙进程数（ffmpeg 风暴观测）；用 psutil 会污染内存，故用快照 API。"""
    if sys.platform != 'win32':
        return 0
    try:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snapshot = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snapshot in (None, ctypes.c_void_p(-1).value):
            return 0
        parent = os.getpid()
        found = 0
        # 只数"直接子进程"：PROCESSENTRY32 的 th32ParentProcessID 即为直接父。
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ('dwSize', ctypes.c_uint32), ('cntUsage', ctypes.c_uint32),
                ('th32ProcessID', ctypes.c_uint32),
                ('th32DefaultHeapID', ctypes.c_void_p),
                ('th32ModuleID', ctypes.c_uint32), ('cntThreads', ctypes.c_uint32),
                ('th32ParentProcessID', ctypes.c_uint32),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', ctypes.c_uint32),
                ('szExeFile', ctypes.c_char * 260),
            ]

        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        if k32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32ParentProcessID == parent:
                    found += 1
                if not k32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        k32.CloseHandle(snapshot)
        return found
    except Exception:
        return 0


# ------------------------------------------------------------------ Qt 普查
def _qt_census() -> dict:
    """GUI 线程内执行：Qt/剪贴簿对象普查。任何异常都不许拖垮采样。"""
    data: dict = {}
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            tops = []
            for widget in app.topLevelWidgets():
                try:
                    tops.append({
                        'cls': type(widget).__name__,
                        'name': widget.objectName(),
                        'w': widget.width(), 'h': widget.height(),
                        'visible': bool(widget.isVisible()),
                    })
                except Exception:
                    continue
            data['top_level_widgets'] = tops
    except Exception:
        pass
    try:
        from PySide6.QtGui import QPixmapCache
        data['qpixmap_cache_limit'] = int(QPixmapCache.cacheLimit())
    except Exception:
        pass
    return data


def _gc_census() -> dict:
    """gc 普查（可在任意线程做：只计数，不调 QPixmap 方法）。

    QImage.sizeInBytes() 是 const + 隐式共享，跨线程读取只读尺寸字段，安全；
    QPixmap 只计数（读它的尺寸必须在 GUI 线程，采样线程不碰）。

    另附 clip 级结算（诊断重点）：每个 WebMClip/FrameSeqClip 的
    - 帧队列条目数与字节数（reader 已解码但尚未消费的 RGBA 帧）
    - 首帧缓存 / 当前显示帧字节数
    - 是否还有存活 reader 线程
    这三项是"106 个播放器对象"到底驻留了多少原生/字节内存的直接证据。

    另附 ``clip_listed_frames``：帧表**物化**的 Path 条目数（M1 帧表去物化后
    FrameSeqClip 常态 0——路径按编号现推，只有 meta 缺失/非法时的 glob 兜底才真
    持有列表）。这是"爬稳态 vs 泄漏"的判别量：健康进程里它不随运行时长增长。
    """
    data: dict = {}
    n_img = 0
    img_bytes = 0
    n_pix = 0
    class_hist: dict[str, int] = {}
    queue_frames = 0
    queue_bytes = 0
    first_bytes = 0
    current_bytes = 0
    live_readers = 0
    retired = 0
    listed_frames = 0
    try:
        for obj in gc.get_objects():
            try:
                name = type(obj).__name__
            except Exception:
                continue
            if name in ('QImage', 'QPixmap', 'QIcon', 'QMovie', 'WebMClip', 'FrameSeqClip',
                        'GifClip', 'PetSprite', 'OverlayWindow', 'PetWindow', 'QWidget',
                        'QTimer', 'QThread', 'QObject', 'DynamicIsland', 'QMenu', 'QAction',
                        'AgentLinkManager', 'SharedAgentLinkManager', 'SharedSubsystems',
                        'MultiWindowProxy', 'SharedProactiveWatcher', 'SharedFullscreenWatcher',
                        'ProactiveScreenWatcher', 'DshStateConverger', 'DecodeFanoutHub',
                        'TodoReminderService', 'VoiceChimeService', 'FestivalReminderService',
                        'IslandChatBubble', 'PetSpeechBubble', 'QSystemTrayIcon',
                        'DirGlobTailer', 'ByteOffsetTailer', 'DshMonitor', 'OpenCodeMonitor',
                        'QNetworkAccessManager', 'AppShell', 'PetInstance'):
                class_hist[name] = class_hist.get(name, 0) + 1
            if name == 'QImage':
                try:
                    if not obj.isNull():
                        n_img += 1
                        img_bytes += int(obj.sizeInBytes())
                except Exception:
                    continue
            elif name == 'QPixmap':
                n_pix += 1
            elif name in ('WebMClip', 'FrameSeqClip'):
                try:
                    queue = getattr(obj, '_queue', None)
                    if queue is not None:
                        items = list(getattr(queue, 'queue', ()))
                        queue_frames += len(items)
                        for item in items:
                            try:
                                queue_bytes += len(item[0])
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    first = getattr(obj, '_first_image', None)
                    if first is not None and not first.isNull():
                        first_bytes += int(first.sizeInBytes())
                except Exception:
                    pass
                try:
                    current = getattr(obj, '_current_image', None)
                    if current is not None and not current.isNull():
                        current_bytes += int(current.sizeInBytes())
                except Exception:
                    pass
                try:
                    thread = getattr(obj, '_thread', None)
                    if thread is not None and thread.is_alive():
                        live_readers += 1
                except Exception:
                    pass
                try:
                    retired += len(getattr(obj, '_retired', ()) or ())
                except Exception:
                    pass
                try:
                    # 帧表物化的 Path 数（None 安全）：FrameSeqClip 的 _frames 是
                    # 现推路径的序列，只有 glob 兜底才真持有列表（属性 listed）；
                    # 其它形态（旧列表）按其长度计。
                    table = getattr(obj, '_frames', None)
                    if table is not None:
                        listed_frames += len(getattr(table, 'listed', table))
                except Exception:
                    pass
        data['class_hist'] = class_hist
        data['qwidget_n'] = class_hist.get('QWidget', 0)
    except Exception:
        pass
    data['qimage_n'] = n_img
    data['qimage_mb'] = round(img_bytes / 1048576.0, 2)
    data['qpixmap_n'] = n_pix
    data['clip_queue_frames'] = queue_frames
    data['clip_queue_mb'] = round(queue_bytes / 1048576.0, 2)
    data['clip_first_frame_mb'] = round(first_bytes / 1048576.0, 2)
    data['clip_current_frame_mb'] = round(current_bytes / 1048576.0, 2)
    data['clip_listed_frames'] = listed_frames
    data['clip_live_readers'] = live_readers
    data['clip_retired_readers'] = retired
    return data


_CENSUS: dict = {}
_CENSUS_LOCK = threading.Lock()


def _census_on_gui() -> None:
    """GUI 线程：补 top-level widget 清单（QWidget 几何只能在本线程读）。"""
    try:
        result = _qt_census()
    except Exception:
        return
    with _CENSUS_LOCK:
        _CENSUS.update(result)


def _census() -> dict:
    with _CENSUS_LOCK:
        return dict(_CENSUS)


# ------------------------------------------------------------------ tracemalloc 归因
def _bucket_outer(traceback) -> str:
    """归因口径 A：traceback 中**第一个我方帧**（谁发起的这段分配）。"""
    root = str(ROOT).lower()
    frames = list(traceback)
    for frame in frames:
        filename = str(frame.filename)
        if filename.lower().startswith(root):
            try:
                rel = Path(filename).resolve().relative_to(ROOT).as_posix()
            except Exception:
                rel = filename
            return f'{rel}:{frame.lineno}'
    if frames:
        last = frames[-1]
        return f'<ext> {Path(str(last.filename)).name}:{last.lineno}'
    return '<unknown>'


def _bucket_inner(traceback) -> str:
    """归因口径 B：traceback 中**最后一个我方帧**（真正的分配点）。"""
    root = str(ROOT).lower()
    frames = list(traceback)
    for frame in reversed(frames):
        filename = str(frame.filename)
        if filename.lower().startswith(root):
            try:
                rel = Path(filename).resolve().relative_to(ROOT).as_posix()
            except Exception:
                rel = filename
            return f'{rel}:{frame.lineno}'
    if frames:
        last = frames[-1]
        return f'<ext> {Path(str(last.filename)).name}:{last.lineno}'
    return '<unknown>'


def _table(snapshot, key_fn, limit: int = 45) -> list[str]:
    groups: dict[str, list[int]] = {}
    for stat in snapshot.statistics('traceback'):
        key = key_fn(stat.traceback)
        slot = groups.setdefault(key, [0, 0])
        slot[0] += stat.count
        slot[1] += stat.size
    ordered = sorted(groups.items(), key=lambda kv: kv[1][1], reverse=True)
    lines = [f'{"MB":>9}  {"blocks":>9}  bucket']
    for key, (count, size) in ordered[:limit]:
        lines.append(f'{size / 1048576.0:9.3f}  {count:9d}  {key}')
    return lines


def _write_snapshot(tag: str) -> None:
    import tracemalloc

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = tracemalloc.take_snapshot()
    total = sum(stat.size for stat in snapshot.statistics('filename'))
    lines = [f'# snapshot {tag}  (mode={MODE}, label={LABEL})', '',
             f'total traced = {total / 1048576.0:.2f} MB', '',
             '## 口径 A：最外层我方帧（谁发起）', '']
    lines += _table(snapshot, _bucket_outer)
    lines += ['', '## 口径 B：最内层我方帧（分配点）', '']
    lines += _table(snapshot, _bucket_inner)
    (SNAP_DIR / f'snap-{tag}.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


# ------------------------------------------------------------------ 采样主循环
class _GuiBridge:
    """把"要在 GUI 线程做的读"排队过去（PySide6 6.11 只接受字符串成员名）。

    采样线程创建本对象后 moveToThread 到 GUI 线程；之后 emit 即队列投递到 GUI
    线程执行，采样线程绝不直接触碰 QWidget。
    """

    def __init__(self, app) -> None:
        from PySide6.QtCore import QObject, Qt, Signal, Slot

        class _Bridge(QObject):
            requested = Signal()

            @Slot()
            def _on_requested(self) -> None:  # pragma: no cover - 运行时探针
                _census_on_gui()

        self._bridge = _Bridge()
        self._bridge.requested.connect(
            self._bridge._on_requested, Qt.ConnectionType.QueuedConnection)
        self._bridge.moveToThread(app.thread())

    def request(self) -> None:
        try:
            self._bridge.requested.emit()
        except Exception:
            pass


def _quit_app(app) -> None:
    from PySide6.QtCore import QMetaObject, Qt

    try:
        QMetaObject.invokeMethod(app, 'quit', Qt.ConnectionType.QueuedConnection)
        return
    except Exception:
        pass
    try:
        app.quit()
    except Exception:
        pass


def _sampler() -> None:
    started = time.monotonic()
    last_snap = -1e9
    last_census = -1e9
    quit_sent = False
    bridge = None
    with CSV_PATH.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        handle.flush()
        while True:
            now = time.monotonic()
            elapsed = now - started
            app = None
            try:
                from PySide6.QtWidgets import QApplication
                app = QApplication.instance()
            except Exception:
                app = None
            if app is not None and bridge is None:
                try:
                    bridge = _GuiBridge(app)
                except Exception:
                    bridge = None
            if app is not None and elapsed - last_census >= 10.0:
                last_census = elapsed
                if bridge is not None:
                    bridge.request()
                try:
                    gc_census = _gc_census()
                except Exception:
                    gc_census = {}
                with _CENSUS_LOCK:
                    _CENSUS.update(gc_census)
            row = {'t': round(elapsed, 2)}
            try:
                row.update(_read_mem())
                row['ws_mb'] = round(row['ws_mb'], 2)
                row['peak_ws_mb'] = round(row['peak_ws_mb'], 2)
                row['private_mb'] = round(row['private_mb'], 2)
            except Exception:
                pass
            handles, threads = _handle_thread_counts()
            row['handles'] = handles
            row['threads'] = threads
            row['children'] = _child_count()
            if MODE == 'trace':
                try:
                    import tracemalloc
                    current, peak = tracemalloc.get_traced_memory()
                    row['tm_current_mb'] = round(current / 1048576.0, 2)
                    row['tm_peak_mb'] = round(peak / 1048576.0, 2)
                except Exception:
                    pass
            try:
                row['py_blocks'] = sys.getallocatedblocks()
            except Exception:
                pass
            census = _census()
            row['qimage_n'] = census.get('qimage_n', '')
            row['qimage_mb'] = census.get('qimage_mb', '')
            row['qpixmap_n'] = census.get('qpixmap_n', '')
            row['qwidget_n'] = census.get('qwidget_n', '')
            row['clip_n'] = census.get('class_hist', {}).get('WebMClip', 0) + \
                census.get('class_hist', {}).get('FrameSeqClip', 0)
            row['clip_queue_frames'] = census.get('clip_queue_frames', '')
            row['clip_queue_mb'] = census.get('clip_queue_mb', '')
            row['clip_first_mb'] = census.get('clip_first_frame_mb', '')
            row['clip_curr_mb'] = census.get('clip_current_frame_mb', '')
            row['clip_listed_frames'] = census.get('clip_listed_frames', '')
            row['live_readers'] = census.get('clip_live_readers', '')
            writer.writerow(row)
            handle.flush()
            if MODE == 'trace' and elapsed - last_snap >= SNAP_S:
                last_snap = elapsed
                try:
                    _write_snapshot(f'{int(elapsed):04d}s')
                except Exception:
                    pass
            if elapsed >= SECONDS and not quit_sent:
                quit_sent = True
                if MODE == 'trace':
                    try:
                        _write_snapshot('final')
                    except Exception:
                        pass
                # 退出前再采一次 GUI 普查（拿最终 top-level widget 清单）
                if bridge is not None:
                    bridge.request()
                time.sleep(4.0)
                if app is not None:
                    _quit_app(app)
                time.sleep(20.0)
                # 应用没退（或退出很慢）：写摘要后强退，避免挂死。
                _write_summary()
                os._exit(0)
            time.sleep(SAMPLE_S)


def _write_summary() -> None:
    rows = []
    try:
        with CSV_PATH.open('r', encoding='utf-8') as handle:
            for row in csv.DictReader(handle):
                rows.append(row)
    except Exception:
        rows = []

    def _f(row, key):
        try:
            return float(row.get(key) or 0.0)
        except Exception:
            return 0.0

    summary = {
        'label': LABEL,
        'mode': MODE,
        'seconds': SECONDS,
        'appdata': os.environ.get('APPDATA', ''),
        'topology_env': os.environ.get('PET_RENDER_TOPOLOGY', ''),
        'qt_platform': os.environ.get('QT_QPA_PLATFORM', ''),
        'probe_env': {k: v for k, v in sorted(os.environ.items())
                      if k.startswith('PET_') or k.startswith('DSH_')},
        'samples': len(rows),
        'census': _census(),
    }
    if rows:
        mark = max(0.0, SECONDS - 60.0)
        early = [r for r in rows if _f(r, 't') <= 15.0]
        mid = [r for r in rows if 100.0 <= _f(r, 't') <= 180.0]
        late = [r for r in rows if _f(r, 't') >= mark]
        peak_all = max(rows, key=lambda r: _f(r, 'ws_mb'))
        summary.update({
            'ws_start_mb': _f(rows[0], 'ws_mb'),
            'ws_t15_mb': _f(early[-1], 'ws_mb') if early else None,
            'ws_t150_mb': _f(rows[-1], 'ws_mb'),
            'ws_peak_mb': _f(peak_all, 'ws_mb'),
            'ws_peak_t': _f(peak_all, 't'),
            'ws_mid_avg_mb': round(sum(_f(r, 'ws_mb') for r in mid) / len(mid), 2) if mid else None,
            'ws_late_avg_mb': round(sum(_f(r, 'ws_mb') for r in late) / len(late), 2) if late else None,
            'ws_late_max_mb': round(max(_f(r, 'ws_mb') for r in late), 2) if late else None,
            'private_late_avg_mb': round(sum(_f(r, 'private_mb') for r in late) / len(late), 2) if late else None,
            'children_max': max(int(_f(r, 'children')) for r in rows),
            'handles_late': int(_f(rows[-1], 'handles')),
            'threads_late': int(_f(rows[-1], 'threads')),
            'qimage_late_n': rows[-1].get('qimage_n'),
            'qimage_late_mb': rows[-1].get('qimage_mb'),
            'qpixmap_late_n': rows[-1].get('qpixmap_n'),
            'clip_late_n': rows[-1].get('clip_n'),
            'clip_listed_frames_late': rows[-1].get('clip_listed_frames'),
        })
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> int:
    if MODE == 'trace':
        import tracemalloc
        tracemalloc.start(25)
    thread = threading.Thread(target=_sampler, daemon=True, name='mem-probe-sampler')
    thread.start()
    try:
        from pet.app import main as app_main
        code = app_main([sys.argv[0]])
    finally:
        _write_summary()
    return int(code or 0)


if __name__ == '__main__':
    sys.exit(main())
