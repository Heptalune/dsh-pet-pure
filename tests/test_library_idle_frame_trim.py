# -*- coding: utf-8 -*-
"""素材池「非播放中 clip 不留像素」不变量回归（内存瘦身第一刀）。

实测背景（.scratch/mem-probe/base-overlay-trace，tracemalloc 口径 B）：
稳态下 50.1MB Python 堆落在 ``pet/webm_clip.py:2545``（``frame = next(it)``，
即 reader 解码出来的 RGBA 帧），共 57 块 × 0.879MB —— 恰好是"若干被切走
且不再播放的 clip 各攥着一整条 8 帧队列"。

根因位置在渲染层的 stop 路径（已停播的 clip 只清显示槽、不排空帧队列，见
webm_clip._hard_stop），而 MovieLibrary 是素材池的持有者：本刀在池这一层把
不变量补上——**不在播、且 reader 线程已退出的 clip，不得再持有解码帧与
显示槽像素**。

功能等价论证（为什么不会回退任何功能）：
- 队列只可能被该 clip 自己的 QTimer(_poll) 消费；clip 不在播 = 定时器已停，
  这些帧在物理上不可能再被任何路径读取；
- webm_clip.start() 每次都会 ``self._queue = queue.Queue(maxsize=8)`` 重建
  队列，下一次播放拿到的是全新队列，绝不依赖旧队列里的任何一帧；
- 软停驻留（``_soft_parked``，等 re-arm 续圈）的 reader 仍存活，被"reader
  已退出"判据排除，续圈语义零影响；
- 显示槽由 start()/jumpToFrame() 重写，且桌宠显示的是 PetSprite 自己那份
  pixmap，清空已停播 clip 的显示槽不会造成任何可见变化
  （webm_clip._hard_stop 本来就会这么做）。
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

import pet.library as library_mod


FRAME_BYTES = 1024


class FakeReaderClip:
    """WebMClip 的最小替身：暴露池级回收真正要读/要写的那几个面。"""

    def __init__(self, path, parent=None):
        self.path = Path(path)
        self._queue = queue.Queue(maxsize=8)
        self._running = False
        self._thread = None
        self._soft_parked = False
        self.display_cleared = 0
        self.warmed_meta = 0
        self.warmed_frame = 0

    # 预热面
    def warm_meta(self):
        self.warmed_meta += 1

    def warm_first_frame(self):
        self.warmed_frame += 1

    # 池级回收面
    def clear_display_frame(self):
        self.display_cleared += 1

    def fill_queue(self, frames: int = 3) -> int:
        for index in range(frames):
            self._queue.put((b'x' * FRAME_BYTES, index))
        return frames * FRAME_BYTES


def _live_thread() -> threading.Thread:
    """一个真正存活的后台线程（用于"reader 未退出"判据）。"""
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    thread._test_release = release  # type: ignore[attr-defined]
    return thread


def _make_lib(tmp_path, monkeypatch):
    monkeypatch.setattr(library_mod, 'WebMClip', FakeReaderClip)
    videos = tmp_path / 'videos'
    folders = {
        'idle': ['待机呼吸休闲.webm'],
        'click': ['点击回应-开心跃动.webm', '点击回应-害羞惊讶.webm'],
        'random': ['吃白饭.webm', '写代码.webm'],
    }
    for folder, files in folders.items():
        directory = videos / folder
        directory.mkdir(parents=True, exist_ok=True)
        for name in files:
            (directory / name).write_bytes(b'fake')
    return library_mod.MovieLibrary(asset_dir=videos, prewarm_policy='balanced')


def _clips(lib):
    return list(lib.movies().values())


# --------------------------------------------------------------- 回收生效
def test_release_idle_frames_drains_stopped_clip(tmp_path, monkeypatch):
    lib = _make_lib(tmp_path, monkeypatch)
    clip = _clips(lib)[0]
    clip.display_cleared = 0  # 计数归零：构造期的 movie() 回收也会清槽
    expected = clip.fill_queue(3)

    freed = lib.release_idle_frames()

    assert clip._queue.empty(), '已停播且 reader 已退出的 clip 不得再攥着解码帧'
    assert freed >= expected, f'应报告回收字节数，实际 {freed} < {expected}'
    assert clip.display_cleared == 1, '显示槽像素也属于同一笔残留'


def test_release_idle_frames_keeps_playing_clip(tmp_path, monkeypatch):
    lib = _make_lib(tmp_path, monkeypatch)
    clip = _clips(lib)[0]
    clip.display_cleared = 0
    clip.fill_queue(2)
    clip._running = True

    lib.release_idle_frames()

    assert clip._queue.qsize() == 2, '正在播放的 clip 队列是活数据，绝不能动'
    assert clip.display_cleared == 0


def test_release_idle_frames_keeps_live_reader_clip(tmp_path, monkeypatch):
    """圈末软停驻留（_soft_parked）的 reader 仍存活：续圈要靠这条队列。"""
    lib = _make_lib(tmp_path, monkeypatch)
    clip = _clips(lib)[0]
    clip.display_cleared = 0
    clip.fill_queue(2)
    clip._soft_parked = True
    clip._thread = _live_thread()
    try:
        lib.release_idle_frames()
        assert clip._queue.qsize() == 2
        assert clip.display_cleared == 0
    finally:
        clip._thread._test_release.set()  # type: ignore[attr-defined]


def test_release_idle_frames_reclaims_parked_clip_whose_reader_died(
        tmp_path, monkeypatch):
    """驻留宽限期满后 reader 自行退出：那时队列同样不可能再被读到。"""
    lib = _make_lib(tmp_path, monkeypatch)
    clip = _clips(lib)[0]
    clip.fill_queue(2)
    clip._soft_parked = True
    clip._thread = None  # 宽限期满，reader 已退出

    lib.release_idle_frames()

    assert clip._queue.empty()


def test_release_idle_frames_skips_alive_thread_even_when_not_running(
        tmp_path, monkeypatch):
    lib = _make_lib(tmp_path, monkeypatch)
    clip = _clips(lib)[0]
    clip.fill_queue(1)
    clip._thread = _live_thread()
    try:
        lib.release_idle_frames()
        assert clip._queue.qsize() == 1
    finally:
        clip._thread._test_release.set()  # type: ignore[attr-defined]


def test_release_idle_frames_tolerates_broken_clip(tmp_path, monkeypatch):
    """单个素材的对象面坏掉（C++ 侧半销毁等）不得拖垮整池回收。"""
    lib = _make_lib(tmp_path, monkeypatch)
    clips = _clips(lib)
    broken, healthy = clips[0], clips[1]
    healthy.fill_queue(2)

    def _boom():
        raise RuntimeError('C++ object already deleted')

    broken.clear_display_frame = _boom  # type: ignore[method-assign]
    broken._queue = None

    lib.release_idle_frames()

    assert healthy._queue.empty(), '一个坏素材不能让其余素材的回收被跳过'


# --------------------------------------------------------------- 触发时机
def test_movie_creation_trims_idle_siblings(tmp_path, monkeypatch):
    """切换动画（movie() 新建 clip）时顺手回收兄弟 clip 的残留帧。

    这是事件驱动的触发点：切走旧 clip 的那一刻就是残留产生的时刻。
    """
    lib = _make_lib(tmp_path, monkeypatch)
    monkeypatch.setattr(lib, '_priority_names', lambda: ([], ['吃白饭']))
    lib.movie('吃白饭')  # 建出该 clip
    idle = lib.movie('写代码')

    stale = lib.movie('点击回应-开心跃动')  # 高优先级 clip：构造期已建
    stale.fill_queue(3)
    assert stale._queue.qsize() == 3

    # 新建第三个 clip（模拟切换到新动画）时回收已停播的兄弟
    monkeypatch.setattr(lib, '_priority_names', lambda: ([], ['吃白饭', '写代码']))
    lib._movies.pop('写代码', None)
    lib.movie('写代码')

    assert stale._queue.empty()
    assert idle is not None


def test_idle_trim_timer_lifecycle(tmp_path, monkeypatch):
    """低频兜底回收定时器：随低优先级预热排期开、随隐藏/关闭停。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    lib = _make_lib(tmp_path, monkeypatch)
    assert not lib._idle_trim_timer.isActive()

    lib.schedule_low_priority_warm()
    assert lib._idle_trim_timer.isActive()

    lib.pause_warm()
    assert not lib._idle_trim_timer.isActive()
    app.processEvents()


def test_idle_trim_timer_timeout_runs_sweep(tmp_path, monkeypatch):
    lib = _make_lib(tmp_path, monkeypatch)
    clip = _clips(lib)[0]
    clip.fill_queue(2)

    lib._on_idle_trim()

    assert clip._queue.empty()


def test_idle_trim_timer_restarts_on_resume_warm(tmp_path, monkeypatch):
    """隐藏→恢复（pause_warm/resume_warm）必须把池级回收定时器一并重启。

    改前 resume_warm 只重启低优先级预热定时器，而 _idle_trim_timer 的唯一
    启动点在 schedule_low_priority_warm——生产中只在建库/角色交接时调。
    首次隐藏后池级兜底回收终身停摆：3 宠库的残留帧（release_idle_frames
    的 10s 兜底）单调增长。
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    lib = _make_lib(tmp_path, monkeypatch)
    lib.schedule_low_priority_warm()
    try:
        assert lib._idle_trim_timer.isActive()

        # 两轮完整隐藏/恢复：低优先级批次跑完后 incomplete 为假，回收定时器
        # 照样得活过来（它不该依赖预热是否还有活干）
        for _ in range(2):
            lib.pause_warm()
            assert not lib._idle_trim_timer.isActive(), '隐藏即停（既有语义不变）'
            lib.resume_warm()
            assert lib._idle_trim_timer.isActive(), '恢复显示必须重新开表'

        # 幂等：已开表时重复恢复不得重开（周期不被推后）
        lib.pause_warm()
        lib.resume_warm()
        remaining = lib._idle_trim_timer.remainingTime()
        lib.resume_warm()
        lib.resume_warm()
        assert lib._idle_trim_timer.isActive()
        assert lib._idle_trim_timer.remainingTime() <= remaining, \
            '重复 resume_warm 不得重开定时器'
        app.processEvents()
    finally:
        lib.shutdown()


def test_idle_trim_timer_restarts_on_resume_warm_for_peer_library(
        tmp_path, monkeypatch):
    """兄弟库（预热责任不在本库）的池级回收同样随隐藏/恢复重启。

    三宠同角色 = 三份库：预热责任只由首个库持有，兄弟库不排低优先级预热，
    但**池级回收管的是本库自己的内存**（schedule_low_priority_warm 对兄弟库
    照旧开表）——所以恢复显示时的重启不能藏在"责任持有者"分支后面。
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    lib1 = _make_lib(tmp_path, monkeypatch)
    try:
        lib1.schedule_low_priority_warm()
        lib2 = library_mod.MovieLibrary(
            asset_dir=tmp_path / 'videos', prewarm_policy='balanced')
        try:
            lib2.schedule_low_priority_warm()
            assert lib2._warm_peer is True, '第二份库必须是兄弟库，否则本用例没意义'
            assert lib2._idle_trim_timer.isActive()

            lib2.pause_warm()
            assert not lib2._idle_trim_timer.isActive()
            lib2.resume_warm()
            assert lib2._idle_trim_timer.isActive(), '兄弟库的池级回收也必须重启'
            app.processEvents()
        finally:
            lib2.shutdown()
    finally:
        lib1.shutdown()
