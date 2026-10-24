# -*- coding: utf-8 -*-
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pet import catalog
from pet import webm_clip as webm_clip_module
from pet.webm_clip import WebMClip


def test_rapid_start_stop_no_leaked_running_threads():
    app = QApplication.instance() or QApplication([])

    sample_webm = Path("assets/characters/shenshen/videos/idle/待机呼吸休闲.webm")
    assert sample_webm.exists(), f"WebM test file not found: {sample_webm}"

    clip = WebMClip(sample_webm)

    initial_threads = {t for t in threading.enumerate() if t.is_alive()}

    # 连续 start/stop 10 次
    for _ in range(10):
        clip.start()
        app.processEvents()
        clip.stop()
        app.processEvents()

    # 销毁/清理 clip，等待所有 retired 线程回收
    clip.cleanup()
    app.processEvents()

    # 断言 clip._retired 中的 reader（线程+进程句柄记录）已全部结束
    for r in clip._retired:
        assert not r.thread.is_alive()

    # 断言无残留运行中的 reader 线程（或整体 threading 运行线程无残留）。
    # 线程退出是异步的，给一点宽限时间再断言，避免 CI 偶发“线程尚未完全回收”。
    deadline = time.monotonic() + 5.0
    while True:
        alive_threads = {t for t in threading.enumerate() if t.is_alive()}
        new_alive = [t for t in alive_threads if t not in initial_threads]
        if not new_alive or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
        app.processEvents()
    assert len(new_alive) == 0, f"Remaining unexpected threads: {new_alive}"


def test_reader_cancelled_before_ffmpeg_process_start(monkeypatch):
    clip = WebMClip(__file__)
    stop_evt = threading.Event()
    stop_evt.set()
    calls = []
    monkeypatch.setattr(
        "pet.webm_clip.imageio_ffmpeg.read_frames",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    clip._reader(stop_evt, generation=clip._generation)

    assert calls == []
    clip.cleanup()


def test_reader_cancelled_during_metadata_handshake_does_not_request_a_frame(monkeypatch):
    clip = WebMClip(__file__)
    stop_evt = threading.Event()
    metadata_started = threading.Event()
    release_metadata = threading.Event()
    frame_requests = []

    def controlled_frames(*_args, **_kwargs):
        metadata_started.set()
        assert release_metadata.wait(1.0)
        yield {"fps": 24.0, "duration": 1.0}
        frame_requests.append(True)
        yield b""

    monkeypatch.setattr("pet.webm_clip.imageio_ffmpeg.read_frames", controlled_frames)
    reader = threading.Thread(
        target=clip._reader,
        args=(stop_evt, clip._generation),
        daemon=True,
    )
    reader.start()
    assert metadata_started.wait(1.0)

    stop_evt.set()
    release_metadata.set()
    reader.join(timeout=1.0)

    assert not reader.is_alive()
    assert frame_requests == []
    clip.cleanup()


def test_stale_generation_reader_writes_dropped(monkeypatch):
    app = QApplication.instance() or QApplication([])

    sample_webm = Path("assets/characters/shenshen/videos/idle/待机呼吸休闲.webm")
    assert sample_webm.exists()

    clip = WebMClip(sample_webm)
    clip._ensure_meta()

    # 构造并启动一个 generation 为 1 的 reader 线程逻辑
    clip._generation = 1
    stop_evt = threading.Event()
    q = queue.Queue(maxsize=8)
    clip._queue = q

    # 模拟把 generation 提高为 2（表示新动画已启动），旧 generation 1 的 reader 尝试写入
    clip._generation = 2

    # 验证旧 reader 在 generation 不匹配时不会写入 self._queue 或 self._fps/duration
    old_fps = clip._fps
    clip._fps = 999.0

    # 运行 reader，由于 generation (1) != clip._generation (2)，reader 会迅速退出并不向队列或元数据写入
    clip._reader(stop_evt, generation=1)

    assert q.empty(), "Stale reader should not put items into queue"
    assert clip._fps == 999.0, "Stale reader should not overwrite metadata"

    clip.cleanup()
    app.processEvents()


def test_timer_is_precise_and_frame_interval_is_42ms():
    app = QApplication.instance() or QApplication([])

    assert catalog.FRAME_MS == 42

    sample_webm = Path("assets/characters/shenshen/videos/idle/待机呼吸休闲.webm")
    clip = WebMClip(sample_webm) if sample_webm.exists() else WebMClip(__file__)

    assert clip._timer.timerType() == Qt.TimerType.PreciseTimer
    clip._fps = 24.0
    clip.playback_speed = 1.0
    assert clip._timer_interval() == 42

    clip.cleanup()
    app.processEvents()


# --------------------------------- 暂停期背压（O4：暂停 ≠ 持续解码丢帧，2026-10-03）
_FRAME_BYTES = b"\x20\x40\x80\xff" * (4 * 4)   # 4×4 RGBA（_process_frame 的长度校验）


class _ControlledFrames:
    """``read_frames`` 替身：头部 meta + 无限帧流，记录"已解码帧数"。

    只替掉 ffmpeg 这个进程边界（生产 reader/时间线/背压逻辑全真）。前
    ``free_frames`` 帧自由交付，之后每帧都要等测试放行（``gate``）——这样
    "暂停发生在哪一帧的入队决策之前"由测试放行时刻决定，而不是赌时序：
    闸门关着时 reader 卡在取帧上，不在任何入队判定里。
    """

    def __init__(self, free_frames: int = 8) -> None:
        self.produced = 0
        self.free_frames = free_frames
        self.gate = threading.Event()

    def __call__(self, *_args, **_kwargs):
        yield {"fps": 24.0, "duration": 1.0}
        while True:
            if self.produced >= self.free_frames:
                self.gate.wait()
            self.produced += 1
            yield _FRAME_BYTES


def _clip_with_controlled_frames(tmp_path, monkeypatch):
    """真 WebMClip + 受控帧源（元数据预置 → start() 不跑 ffprobe 探测）。"""
    frames = _ControlledFrames()
    clip = WebMClip(str(tmp_path / "x.webm"))
    clip._duration = 1.0          # _ensure_meta 早退：测试不拉起探测进程
    clip._frame_count = 0         # 非精确帧数 → 不进程内循环（线性路径）
    clip._fps = 24.0
    clip._w, clip._h, clip._bpp = 4, 4, 4
    monkeypatch.setattr("pet.webm_clip.imageio_ffmpeg.read_frames", frames)
    return clip, frames


def _drops() -> int:
    """reader 侧"队列满丢帧"计数（产品打点 webm.queue_drop）。"""
    from pet import perfstats

    return perfstats.snapshot().get("webm.queue_drop", {}).get("count", 0)


def _wait_until(cond, timeout_s=5.0) -> bool:
    """有界轮询状态（宽预算，不赌固定 sleep；不泵事件：消费端由测试手动驱动）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


def _wait_produced_settled(frames, quiet_s=0.5, timeout_s=3.0) -> bool:
    """"已解码帧数"静止（连续 quiet_s 不变）。

    改前的丢帧路径每 ~0.2s 就会再解一帧丢掉，永远静不下来；改后 reader
    停在背压上，边界那一帧落地后计数立即静止。
    """
    last, changed_at = frames.produced, time.monotonic()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if frames.produced != last:
            last, changed_at = frames.produced, time.monotonic()
        elif time.monotonic() - changed_at >= quiet_s:
            return True
        time.sleep(0.005)
    return False


def _fill_queue_and_park_reader(clip, frames, timeout_s=5.0) -> bool:
    """把 reader 停在"队列写满 + 卡在下一帧取帧上"（入队判定之前）。"""
    if not _wait_until(lambda: clip._queue.qsize() >= clip._queue.maxsize,
                       timeout_s):
        return False
    # 前 free_frames 帧自由交付 → 此刻 reader 正卡在闸门后的取帧上
    return _wait_until(lambda: frames.produced >= frames.free_frames, timeout_s)


def test_pause_backpressures_reader_without_dropping_frames(tmp_path, monkeypatch):
    """暂停只停消费端 → reader 必须走阻塞背压，绝不丢帧/虚推进源帧号。

    改前 reader 的非节流分支队列满即丢帧且源帧号照推：暂停 = 队列写满 =
    持续解码持续丢帧——暂停位置丢失（恢复后跳帧）且隐藏期 CPU 白烧，与
    pause() docstring 承诺的"队列写满即背压、从暂停处续播"直接矛盾。
    """
    from pet import perfstats

    app = QApplication.instance() or QApplication([])
    clip, frames = _clip_with_controlled_frames(tmp_path, monkeypatch)
    seen = []
    clip.frameChanged.connect(seen.append)
    perfstats.reset()
    perfstats.enable()
    try:
        assert clip.start() is True
        assert _fill_queue_and_park_reader(clip, frames), \
            "reader 未就位（队列写满 + 卡在取帧闸门上）"

        clip.pause()
        assert clip._timer.isActive() is False
        assert _drops() == 0

        # 放行边界帧：它必须走阻塞背压（暂停已生效），而不是被丢掉
        frames.gate.set()
        assert _wait_until(lambda: frames.produced > frames.free_frames), \
            "放行后 reader 没取这一帧"
        snapshot = frames.produced

        # 暂停窗口：reader 必须停在背压上（不再解码新帧）。
        # 改前丢帧路径每 ~0.2s 就解一帧丢掉，produced 必定增长。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert frames.produced == snapshot, "暂停期 reader 仍在解码（丢帧路径）"
        assert _drops() == 0, "暂停期不得丢帧"

        # 恢复：消费端手动驱动（定时器真实走时会引入不必要的时序抖动），
        # 从暂停位置逐帧续播——无丢帧则源帧号必须连续
        clip.resume()
        assert clip._timer.isActive() is True
        clip._timer.stop()
        deadline = time.monotonic() + 5.0
        while len(seen) < 12 and time.monotonic() < deadline:
            clip._poll()
            time.sleep(0.002)
        assert len(seen) >= 12, f"恢复后消费不足：{seen}"
        assert seen == list(range(len(seen))), f"暂停边界出现跳帧：{seen}"
        assert _drops() == 0, "整段暂停/恢复过程不得丢帧"
    finally:
        frames.gate.set()
        perfstats.disable()
        perfstats.reset()
        clip.cleanup()
        app.processEvents()


def _wait_readers_retired(clip, timeout_s=5.0) -> bool:
    """退役 reader 全部退出（宽预算轮询，不赌固定 sleep）。"""
    return _wait_until(
        lambda: (clip._thread is None and clip._reader_proc is None
                 and all(not r.thread.is_alive() for r in clip._retired)),
        timeout_s)


@pytest.mark.parametrize("interrupt", ["stop", "jump_to_frame"])
def test_pause_then_interrupt_recycles_blocked_reader(
        tmp_path, monkeypatch, interrupt):
    """边界：暂停期 reader 阻塞在背压里，stop()/jumpToFrame(0) 仍能把它收回来。

    背压路径的退出判定链是 is_stopped()（stop_evt 或换代）：阻塞重试每轮
    复查，置位后立即退出、由 finally 收尾——不会出现"没人消费就永远醒不来"
    的 reader（那才是拿背压换丢帧的代价）。
    """
    from pet import perfstats

    app = QApplication.instance() or QApplication([])
    clip, frames = _clip_with_controlled_frames(tmp_path, monkeypatch)
    perfstats.reset()
    perfstats.enable()
    try:
        assert clip.start() is True
        assert _fill_queue_and_park_reader(clip, frames)
        clip.pause()
        frames.gate.set()
        assert _wait_until(lambda: frames.produced > frames.free_frames)
        assert _wait_produced_settled(frames), \
            "暂停期 reader 未停在背压上（仍在解码丢帧）"
        assert _drops() == 0

        if interrupt == "stop":
            clip.stop()
        else:
            assert clip.jumpToFrame(0) is True

        assert _wait_readers_retired(clip), "阻塞中的 reader 未被及时回收"
    finally:
        frames.gate.set()
        perfstats.disable()
        perfstats.reset()
        clip.cleanup()
        app.processEvents()


# ---------------------------------------------------------------- 缺陷 19：start 失败收口
class _FailingStartTimer:
    """播放节拍 QTimer 替身：start() 必抛（模拟 C++ 定时器半销毁）。

    其余接口按原 QTimer 转发，start() 之前的间隔设置/停止路径逐位不变——
    只把"定时器起不来"这一个失败点摆出来。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.start_calls = 0

    def start(self, *args, **kwargs):
        self.start_calls += 1
        raise RuntimeError("QTimer 已随 clip 销毁")

    def stop(self) -> None:
        self._inner.stop()

    def isActive(self) -> bool:
        return self._inner.isActive()

    def interval(self) -> int:
        return self._inner.interval()

    def setInterval(self, value: int) -> None:
        self._inner.setInterval(value)


class _ParkedReaderClip(WebMClip):
    """reader 入口替换为"等停止信号即退"的替身。

    保证 ``start()`` 在 ``thread.start()`` 之后失败时，那条 reader 线程确实
    活着（真实实现里它还会拉起 ffmpeg）——正是缺陷 19 要收口的状态。
    """

    def __init__(self, path) -> None:
        super().__init__(path)
        self.reader_entered = threading.Event()
        self.reader_stopped = threading.Event()

    def _reader(self, stop_evt, generation, ready_evt=None) -> None:
        self.reader_entered.set()
        stop_evt.wait(10.0)
        if stop_evt.is_set():
            self.reader_stopped.set()


def test_start_failure_leaves_clip_retryable_and_reaps_reader():
    """缺陷 19：start() 中途失败不得留下 "_running=True 却无 reader"。

    修前 ``_running = True`` 早于 thread.start()/_timer.start()：定时器抛错的
    clip 此后每次 start() 都在开头假成功（冻在旧帧），而 reader 线程（真实
    实现里连同它拉起的 ffmpeg）无人消费地活着。修后失败走既有硬停收口
    （送达停止信号 + 退役登记），_running 保持假，再次 start() 真能起。
    """
    app = QApplication.instance() or QApplication([])
    sample = Path("assets/characters/shenshen/videos/idle/待机呼吸休闲.webm")
    assert sample.exists(), f"WebM test file not found: {sample}"

    clip = _ParkedReaderClip(sample)
    real_timer = clip._timer
    clip._timer = _FailingStartTimer(real_timer)
    try:
        with pytest.raises(RuntimeError):
            clip.start()

        assert clip.reader_entered.wait(5.0), "前提：reader 线程已起来，失败点在其后"
        assert clip._running is False, \
            "失败后 _running 必须回假（否则后续 start() 开头假成功、冻在旧帧）"
        assert clip._thread is None, "失败后 reader 必须被摘出（不留无主活线程）"
        assert clip.reader_stopped.wait(5.0), \
            "失败必须把停止信号送达 reader（否则线程与它拉起的 ffmpeg 一起泄漏）"
        assert _wait_until(lambda: all(not r.thread.is_alive() for r in clip._retired)), \
            "退役 reader 必须退出（失败路径同样收口）"

        # 定时器恢复可用（真实环境=新窗口/新 timer）：失败不是终态
        clip._timer = real_timer
        assert clip.start() is True, "再次 start() 必须真的能起（不得假成功）"
        assert clip._running is True
        assert clip._thread is not None and clip._thread.is_alive()
    finally:
        clip.stop()
        clip.cleanup()
        app.processEvents()
