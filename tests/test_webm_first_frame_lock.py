# -*- coding: utf-8 -*-
"""N4：WebMClip 首帧解码原子认领（每实例锁）的回归测试。

GUI 线程永不同步解码首帧（实测定案：冷路径 100-337ms 冻结）——jumpToFrame
冷路径只 kick 后台 warm（tests/test_first_frame_no_gui_decode.py 钉硬不变量）；
前台同步解码/逃生口机制已删除。本文件锁定其余语义：
1. 同一时间只有一个首帧解码执行者：并发 warm_first_frame 认领失败的立即放弃；
2. 首帧进程登记与取消的竞态窗口闭合：登记回调在锁内复查代次/cleanup，
   迟到登记（取消后）的进程必须自终止且不得进 _first_frame_procs；
3. 取消（cancel_first_frame_warm/cleanup）换代：在飞解码结果作废不污染缓存；
4. 取消超时的未确认进程由孤儿注册表 sweep 跟踪/补杀/有限重试放弃。

全部用事件/锁同步，不用 sleep 猜时序。
"""
from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

import pet.webm_clip as webm_clip_mod
from pet.webm_clip import WebMClip


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


class _FakeDecodeProc:
    """模拟首帧解码拉起的 ffmpeg 进程句柄：terminate 不退出、kill 才退出。"""

    def __init__(self):
        self._dead = False
        self.terminated = False
        self.killed = False
        self.pid = id(self)

    def poll(self):
        return None if not self._dead else 1

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self._dead = True

    def wait(self, timeout=None):
        if self._dead:
            return 1
        raise subprocess.TimeoutExpired(self, timeout)


class BlockingDecodeClip(WebMClip):
    """每次 _decode_first_qimage 都阻塞直到放行，记录解码调用次数。

    返回有效 QImage：解码完成后可被前台同步路径复用为缓存。
    """

    def __init__(self, path, parent=None):
        super().__init__(path, parent)
        self.decode_entered = threading.Event()
        self.decode_release = threading.Event()
        self.decode_count = 0
        self._counter_lock = threading.Lock()

    def _decode_first_qimage(self, gen=None):
        with self._counter_lock:
            self.decode_count += 1
        self.decode_entered.set()
        self.decode_release.wait(5.0)
        return QImage(2, 2, QImage.Format.Format_RGBA8888)


def test_warm_first_frame_single_executor_atomic_claim(app):
    """同一时间只能有一个首帧解码执行者：第二个并发 warm 认领失败立即放弃。"""
    clip = BlockingDecodeClip("dummy.webm")
    t1 = threading.Thread(target=clip.warm_first_frame, daemon=True)
    t1.start()
    assert clip.decode_entered.wait(5.0), "第一个执行者必须已认领并进入解码"

    clip.warm_first_frame()  # 第二个并发 warm：认领失败 → 不进入解码、不等待
    assert clip.decode_count == 1, "并发首帧解码必须原子认领，不得双执行"

    clip.decode_release.set()
    t1.join(5.0)
    assert clip.decode_count == 1
    assert clip._first_image is not None, "后台解码完成应写入首帧缓存"
    clip.cleanup()
    app.processEvents()


class _WarmBlockingClip(WebMClip):
    """每次 _decode_first_qimage 都阻塞直到放行（模拟在飞预热解码）。"""

    def __init__(self, path, parent=None):
        super().__init__(path, parent)
        self.decode_entered = threading.Event()
        self.decode_release = threading.Event()
        self.decode_count = 0
        self._counter_lock = threading.Lock()

    def _decode_first_qimage(self, gen=None):
        with self._counter_lock:
            self.decode_count += 1
        self.decode_entered.set()
        self.decode_release.wait(5.0)
        return QImage(2, 2, QImage.Format.Format_RGBA8888)


def test_cancel_first_frame_warm_terminates_procs_and_bumps_generation(app):
    """P1-2：cancel_first_frame_warm 必须 terminate 登记的首帧 ffmpeg 进程并换代。"""
    clip = WebMClip("dummy.webm")
    procs = [_FakeDecodeProc(), _FakeDecodeProc()]
    with clip._reader_lock:
        clip._first_frame_procs.update(procs)
    gen_before = clip._first_frame_gen

    clip.cancel_first_frame_warm()

    assert clip._first_frame_gen == gen_before + 1, "取消必须换代使在飞结果作废"
    assert all(p.terminated for p in procs), "取消必须 terminate 在飞首帧 ffmpeg 进程"
    assert all(p.poll() is not None for p in procs), "terminate+kill 后进程必须退出"
    assert clip._first_frame_procs == set(), "取消后登记集合必须清空"
    clip.cleanup()
    app.processEvents()


def test_cleanup_cancels_inflight_warm_and_discards_result(app):
    """P1-2：cleanup 取消在飞首帧预热；被取消的预热结果不得写入缓存。"""
    clip = _WarmBlockingClip("dummy.webm")
    t = threading.Thread(target=clip.warm_first_frame, daemon=True)
    t.start()
    assert clip.decode_entered.wait(5.0), "预热必须已进入解码（持有锁）"

    clip.cleanup()  # 取消在飞预热（换代 + terminate）
    clip.decode_release.set()  # 放行解码：结果须被代次检查丢弃
    t.join(5.0)

    assert clip._first_image is None, "被取消的预热结果不得提交缓存"
    assert clip._cleaned is True
    app.processEvents()


class _FakePopenCapture:
    """替换 _PopenCapture：单例实例，捕获 on_process 登记回调，测试可精确
    控制「Popen 已创建但登记尚未完成」的竞态窗口。"""

    _instance = None
    current = None

    def __new__(cls, on_process=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.entered = threading.Event()
            cls._instance.proceed = threading.Event()
        return cls._instance

    def __init__(self, on_process=None):
        self.on_process = on_process
        _FakePopenCapture.current = self
        self.entered.clear()
        self.proceed.clear()

    def __enter__(self):
        self.entered.set()
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _fake_read_frames(proc, frame_bytes, meta, clip, observed=None):
    """模拟 imageio_ffmpeg.read_frames：经 capture 回调拉起「解码进程」登记，
    等待 proceed 放行（测试控制登记时机）；observed 记录登记发生时进程
    是否已在 _first_frame_procs 中。"""

    def _read_frames(*args, **kwargs):
        cap = _FakePopenCapture.current
        cap.proceed.wait(5.0)
        cap.on_process(proc, ["ffmpeg", "-i", "dummy.webm"])
        if observed is not None:
            observed.append(proc in clip._first_frame_procs)
        yield meta
        yield frame_bytes

    return _read_frames


def _install_fake_decode(monkeypatch, clip, proc, observed=None):
    """安装假 capture/read_frames，返回 capture 实例；frame 数据用真实尺寸。"""
    frame_bytes = bytes(clip._w * clip._h * clip._bpp)
    meta = {"fps": 24.0, "duration": 1.0}
    cap = _FakePopenCapture()
    fake = _fake_read_frames(proc, frame_bytes, meta, clip, observed)
    monkeypatch.setattr(webm_clip_mod, "_PopenCapture", _FakePopenCapture)
    monkeypatch.setattr(webm_clip_mod.imageio_ffmpeg, "read_frames", fake)
    return cap


def test_first_frame_proc_registered_then_unregistered(app, monkeypatch):
    """P1-2/R2：首帧解码进程经 capture 登记进 _first_frame_procs（供取消
    主动 terminate）；解码结束（finally）即从集合移除。"""
    clip = WebMClip("dummy.webm")
    gen = clip._first_frame_gen
    proc = _FakeDecodeProc()
    observed = []
    cap = _install_fake_decode(monkeypatch, clip, proc, observed)
    result = {}

    def _decode():
        result["img"] = clip._decode_first_qimage(gen=gen)

    t = threading.Thread(target=_decode, daemon=True)
    t.start()
    assert cap.entered.wait(5.0), "capture 必须已进入"
    cap.proceed.set()  # 放行登记回调
    t.join(5.0)

    assert result["img"] is not None, "正常解码应成功"
    assert observed == [True], "解码期间进程必须已登记进 _first_frame_procs"
    assert proc not in clip._first_frame_procs, "解码结束后进程必须从集合移除"
    assert proc.terminated is False and proc.killed is False, "正常解码进程不得被终止"
    clip.cleanup()
    app.processEvents()


def test_first_frame_cancel_before_register_terminates_stale_proc(app, monkeypatch):
    """P1-2/R2：取消发生在「Popen 已创建、登记尚未完成」窗口内时，迟到的
    登记必须在锁内复查代次并自终止进程——已取消的进程绝不漏进集合。"""
    clip = WebMClip("dummy.webm")
    gen = clip._first_frame_gen
    proc = _FakeDecodeProc()
    cap = _install_fake_decode(monkeypatch, clip, proc)
    result = {}

    def _decode():
        result["img"] = clip._decode_first_qimage(gen=gen)

    t = threading.Thread(target=_decode, daemon=True)
    t.start()
    assert cap.entered.wait(5.0), "capture 必须已进入"
    # 竞态窗口：Popen 已创建、_register 尚未执行 → 此刻取消（换代 + 清集合）
    clip.cancel_first_frame_warm()
    cap.proceed.set()  # 放行 → 迟到登记执行
    t.join(5.0)

    assert proc.terminated or proc.killed, "迟到的登记必须自终止已取消进程"
    assert proc.poll() is not None, "自终止必须确认进程退出"
    assert proc not in clip._first_frame_procs, "已取消进程不得登记进集合"
    assert result["img"] is None, "取消后解码结果作废"
    clip.cleanup()
    app.processEvents()


def test_cancel_timeout_skip_registers_unconfirmed_and_sweep_kills(app, monkeypatch):
    """批 6-8b 收尾 P1：cancel_first_frame_warm 的 try-acquire 超时跳过不再是
    「无条件安全」——跳过时进程必须登记进 _unconfirmed_procs 重试机制，孤儿
    注册表 sweep 在 owner 释放 _ff_proc_lock 后确认/补杀（进程最终退出）。"""
    clip = WebMClip("dummy.webm")
    proc = _FakeDecodeProc()
    with clip._reader_lock:
        clip._first_frame_procs.add(proc)
    monkeypatch.setattr(webm_clip_mod, "_PROC_LOCK_ACQUIRE_TIMEOUT", 0.05)

    with clip._ff_proc_lock:  # 模拟解码线程正在 finally 的 g.close()（持锁）
        clip.cancel_first_frame_warm()  # 超时跳过 → 登记 unconfirmed

    assert proc.terminated is False, "持锁期间取消不得操作 Popen"
    assert clip._unconfirmed_procs, "超时跳过的进程必须登记进重试机制"
    assert proc in [e[0] for e in clip._unconfirmed_procs], "登记必须携带进程句柄"

    # owner 释放锁后：sweep 补杀确认
    webm_clip_mod._ORPHAN_REGISTRY.reap()
    assert proc.terminated or proc.killed, "sweep 必须补杀未确认退出的进程"
    assert proc.poll() is not None, "补杀必须确认进程退出"
    assert clip._unconfirmed_procs == [], "确认后登记列表必须清空"
    clip.cleanup()
    app.processEvents()


class _UnkillableDecodeProc:
    """模拟首帧解码进程：terminate/kill 全部执行但进程永不退出（kill 无效）。"""

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.waits = 0
        self.pid = id(self)

    def poll(self):
        return None  # 永远存活

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits += 1
        raise subprocess.TimeoutExpired(self, timeout)


class _PollRaisingDecodeProc:
    """模拟 poll 抛异常的进程句柄（Windows 句柄失效等病态）。"""

    def __init__(self):
        self.pid = id(self)
        self.poll_calls = 0

    def poll(self):
        self.poll_calls += 1
        raise OSError("handle invalid")

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 1


def test_sweep_unconfirmed_keeps_tracking_when_proc_unkillable(app, monkeypatch):
    """R2 复审 P1 闭合：拿到 _ff_proc_lock 后补杀失败（kill 后仍存活）不得
    一次即丢条目——保留追踪并累计 attempts，后续 sweep 可再次补杀。"""
    clip = WebMClip("dummy.webm")
    proc = _UnkillableDecodeProc()
    with clip._reader_lock:
        clip._unconfirmed_procs.append([proc, 0, False])
    webm_clip_mod._register_orphan(clip)
    monkeypatch.setattr(webm_clip_mod, "_PROC_LOCK_ACQUIRE_TIMEOUT", 0.05)

    webm_clip_mod._ORPHAN_REGISTRY.reap()

    entries = list(clip._unconfirmed_procs)
    assert len(entries) == 1, "补杀失败必须保留条目（不得移出追踪）"
    assert entries[0][0] is proc
    assert entries[0][1] == 1, "失败必须累计 attempts"
    assert entries[0][2] is False, "未达上限不得标注放弃"
    assert proc.terminated is True and proc.killed is True, "补杀必须已执行"
    assert proc.poll() is None, "进程仍存活（未被误判为已退出）"

    # 第二次 sweep 仍可再次补杀（追踪不丢）
    webm_clip_mod._ORPHAN_REGISTRY.reap()
    entries = list(clip._unconfirmed_procs)
    assert entries[0][1] == 2

    clip.cleanup()
    clip._unconfirmed_procs = []
    webm_clip_mod._ORPHAN_REGISTRY._clips.discard(clip)
    app.processEvents()


def test_sweep_unconfirmed_keeps_tracking_when_poll_raises(app, monkeypatch):
    """R2 复审 P1 闭合：poll 异常（无法确认退出）时条目必须保留追踪并累计
    attempts（绝不一次即丢）。"""
    clip = WebMClip("dummy.webm")
    proc = _PollRaisingDecodeProc()
    with clip._reader_lock:
        clip._unconfirmed_procs.append([proc, 0, False])
    webm_clip_mod._register_orphan(clip)
    monkeypatch.setattr(webm_clip_mod, "_PROC_LOCK_ACQUIRE_TIMEOUT", 0.05)

    webm_clip_mod._ORPHAN_REGISTRY.reap()

    entries = list(clip._unconfirmed_procs)
    assert len(entries) == 1, "poll 异常：必须保留条目"
    assert entries[0][1] == 1
    assert entries[0][2] is False
    assert proc.poll_calls >= 1

    clip.cleanup()
    clip._unconfirmed_procs = []
    webm_clip_mod._ORPHAN_REGISTRY._clips.discard(clip)
    app.processEvents()


def test_sweep_unconfirmed_abandons_after_retry_limit(app, monkeypatch):
    """R2 复审 P1 闭合：未确认退出达到重试上限时告警并标注 abandoned，条目先
    保留一轮（不再重试），下一次 sweep 做终局处置（缺陷 21）。

    「绝不静默丢弃句柄」的不变量由两段组成：达到上限前反复补杀 + 留痕；达到
    上限后条目**不永久钉住**（否则 ``_has_unconfirmed_procs()`` 恒真 → clip
    永远进不了注册表的 discard 分支，显示槽帧被强引用钉住）。
    """
    clip = WebMClip("dummy.webm")
    proc = _UnkillableDecodeProc()
    with clip._reader_lock:
        clip._unconfirmed_procs.append([proc, 0, False])
    webm_clip_mod._register_orphan(clip)
    monkeypatch.setattr(webm_clip_mod, "_PROC_LOCK_ACQUIRE_TIMEOUT", 0.01)

    limit = webm_clip_mod._UNCONFIRMED_KILL_MAX
    for _ in range(limit):
        webm_clip_mod._ORPHAN_REGISTRY.reap()

    entries = list(clip._unconfirmed_procs)
    assert len(entries) == 1, "标注放弃的当下条目仍在追踪中（终局处置在其后一轮）"
    assert entries[0][1] >= limit
    assert entries[0][2] is True, "达到上限必须标注 abandoned"
    assert proc.poll() is None, "病态进程仍未退出"

    # 标注放弃后的下一次 sweep：终局处置（不再重试、清出追踪 + 审计日志）
    webm_clip_mod._ORPHAN_REGISTRY.reap()
    assert clip._unconfirmed_procs == [], "标注放弃后必须终局清出追踪（缺陷 21）"
    assert clip._has_unconfirmed_procs() is False, "abandoned 条目不得再钉住 clip"
    assert clip not in webm_clip_mod._ORPHAN_REGISTRY.holders(), "clip 必须能回到 discard 分支"

    clip.cleanup()
    clip._unconfirmed_procs = []
    webm_clip_mod._ORPHAN_REGISTRY._clips.discard(clip)
    app.processEvents()


def test_cancel_registers_unconfirmed_when_terminate_not_confirmed(app, monkeypatch):
    """缺陷 20：try-acquire 成功但 ``_terminate_proc`` 未确认退出（返回 False）时
    句柄绝不静默丢弃——必须进 ``_unconfirmed_procs`` 交孤儿 sweep 重试/补杀。

    修前该分支丢弃返回值：进程没确认退出却既不登记也不孤儿化，与同文件
    「绝不静默丢弃句柄」的不变量矛盾（首帧进程成为无人追踪的残留）。
    """
    clip = WebMClip("dummy.webm")
    proc = _UnkillableDecodeProc()
    with clip._reader_lock:
        clip._first_frame_procs.add(proc)
    real_terminate = webm_clip_mod.WebMClip._terminate_proc

    def terminate_but_unconfirmed(target, timeout=None):
        real_terminate(target, 0.01)   # 真的试过一次（fake 进程 kill 后仍"存活"）
        return False

    monkeypatch.setattr(webm_clip_mod.WebMClip, "_terminate_proc",
                        staticmethod(terminate_but_unconfirmed))

    clip.cancel_first_frame_warm()

    assert proc.terminated is True, "取消失败前仍须真的尝试 terminate"
    entries = list(clip._unconfirmed_procs)
    assert [e[0] for e in entries] == [proc], "未确认退出的句柄必须登记进未确认追踪"
    assert entries[0][2] is False, "首次登记不得直接标注放弃"
    assert clip in webm_clip_mod._ORPHAN_REGISTRY.holders(), \
        "必须孤儿化，sweep 才有机会补杀确认"

    clip.cleanup()
    clip._unconfirmed_procs = []
    webm_clip_mod._ORPHAN_REGISTRY._clips.discard(clip)
    app.processEvents()


def test_leak_warning_is_logged_once_per_crossing(app, monkeypatch, caplog):
    """缺陷 21：越过泄漏阈值的告警只打一条，不再每 500ms sweep 重复刷屏。

    病态 reader 一直不退时，``_orphan_reap_count`` 每轮 sweep 都递增；用
    ``>=`` 判定会让同一条 warning 永久重复（日志被淹、真信号被稀释）。
    """
    clip = WebMClip("dummy.webm")
    proc = _UnkillableDecodeProc()
    with clip._reader_lock:
        clip._unconfirmed_procs.append([proc, 0, False])
    webm_clip_mod._register_orphan(clip)
    monkeypatch.setattr(webm_clip_mod, "_PROC_LOCK_ACQUIRE_TIMEOUT", 0.01)
    monkeypatch.setattr(webm_clip_mod._OrphanClipRegistry, "_LEAK_ATTEMPTS", 2)
    # 补杀上限放宽：条目持续待重试 → clip 一直留在注册表（正是刷屏场景）
    monkeypatch.setattr(webm_clip_mod, "_UNCONFIRMED_KILL_MAX", 1000)

    with caplog.at_level(logging.WARNING):
        for _ in range(6):
            webm_clip_mod._ORPHAN_REGISTRY.reap()

    leak_lines = [r for r in caplog.records if "多次回收仍存活" in r.getMessage()]
    assert len(leak_lines) == 1, f"越阈告警必须只打一条，实际 {len(leak_lines)} 条"
    assert clip._orphan_reap_count >= 2, "前提：确实越过了阈值（计数继续递增）"

    clip.cleanup()
    clip._unconfirmed_procs = []
    webm_clip_mod._ORPHAN_REGISTRY._clips.discard(clip)
    app.processEvents()


def test_abandoned_entry_terminal_disposal_logs_path_and_duration(app, monkeypatch, caplog):
    """缺陷 21：abandoned 条目的终局处置——清出追踪 + 一条含 clip 路径与存活
    时长的审计日志（进程已 terminate 过、确认杀不掉，不再钉住 clip）。"""
    clip = WebMClip("dummy.webm")
    proc = _UnkillableDecodeProc()
    with clip._reader_lock:
        clip._unconfirmed_procs.append([proc, 0, False])
    webm_clip_mod._register_orphan(clip)
    monkeypatch.setattr(webm_clip_mod, "_PROC_LOCK_ACQUIRE_TIMEOUT", 0.01)

    limit = webm_clip_mod._UNCONFIRMED_KILL_MAX
    for _ in range(limit):
        webm_clip_mod._ORPHAN_REGISTRY.reap()
    assert clip._unconfirmed_procs[0][2] is True, "前提：已标注 abandoned"

    with caplog.at_level(logging.WARNING):
        webm_clip_mod._ORPHAN_REGISTRY.reap()

    assert clip._unconfirmed_procs == [], "终局处置必须清出追踪"
    audit = [r for r in caplog.records if "首帧进程终局处置" in r.getMessage()]
    assert len(audit) == 1, "终局处置必须留一条审计日志"
    text = audit[0].getMessage()
    assert str(clip.path) in text, "审计日志必须含 clip 路径"
    assert "追踪时长" in text, "审计日志必须含句柄存活/追踪时长"
    assert clip not in webm_clip_mod._ORPHAN_REGISTRY.holders(), \
        "终局处置后 clip 必须可被 discard（显示槽帧不再被强引用钉住）"

    clip.cleanup()
    webm_clip_mod._ORPHAN_REGISTRY._clips.discard(clip)
    app.processEvents()


# ---------------------------------------------------------------------------
# #201 首帧校验静默：两个失败出口都必须可诊断
# ---------------------------------------------------------------------------

def _make_meta_clip(tmp_path, width: int = 2, height: int = 2) -> WebMClip:
    """元数据直接给足的最小 clip：_decode_first_qimage 不触真实 ffmpeg 探测。"""
    clip = WebMClip(str(tmp_path / "x.webm"))
    clip._w = width
    clip._h = height
    clip._fps = 24.0
    clip._duration = 0.1
    clip._frame_count = 3
    clip._frame_count_exact = True
    return clip


def _install_frames(monkeypatch, meta, frame):
    """把 read_frames 换成"返回 meta + 一帧"的替身（与真实契约一致：生成器）。"""

    def fake_read_frames(*args, **kwargs):
        yield meta
        yield frame

    monkeypatch.setattr(webm_clip_mod.imageio_ffmpeg, "read_frames", fake_read_frames)


def test_first_frame_length_mismatch_is_logged(tmp_path, monkeypatch, caplog):
    """#201：首帧尺寸不符必须留 warning（对齐播放侧 :3004 的可诊断口径）。

    旧实现直接 ``return None``，既不记日志也不计数——"素材尺寸与画布不符"这类
    问题在首帧路径上完全不可诊断（播放路径靠同款 warning 一眼定位过）。
    """
    clip = _make_meta_clip(tmp_path)
    expect = clip._w * clip._h * clip._bpp
    _install_frames(monkeypatch, {"fps": 24.0, "duration": 0.1}, b"\x00" * (expect - 4))

    with caplog.at_level(logging.WARNING):
        assert clip._decode_first_qimage() is None, "尺寸不符仍须按 None 降级（行为不变）"

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    hit = [m for m in msgs if "首帧长度异常" in m]
    assert hit, f"尺寸不符必须留 warning，实得 {msgs}"
    assert str(clip.path) in hit[0], f"warning 必须含素材路径：{hit[0]}"
    assert f"got={expect - 4}" in hit[0] and f"expect={expect}" in hit[0], hit[0]


def test_first_frame_null_image_is_logged(tmp_path, monkeypatch, caplog):
    """#201：QImage 构造为空（画布尺寸为 0）同样不得静默 return None。"""
    clip = _make_meta_clip(tmp_path, width=0, height=0)
    _install_frames(monkeypatch, {"fps": 24.0, "duration": 0.1}, b"")

    with caplog.at_level(logging.WARNING):
        assert clip._decode_first_qimage() is None

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    hit = [m for m in msgs if "首帧图像为空" in m]
    assert hit, f"空图像出口必须留 warning，实得 {msgs}"
    assert str(clip.path) in hit[0], f"warning 必须含素材路径：{hit[0]}"


def test_first_frame_success_path_stays_silent(tmp_path, monkeypatch, caplog):
    """对照组：尺寸正确时不得出现任何 warning（新日志不能变成噪声）。"""
    clip = _make_meta_clip(tmp_path)
    expect = clip._w * clip._h * clip._bpp
    _install_frames(monkeypatch, {"fps": 24.0, "duration": 0.1}, b"\x11" * expect)

    with caplog.at_level(logging.WARNING):
        img = clip._decode_first_qimage()

    assert img is not None and not img.isNull(), "正确尺寸必须照常返回 QImage"
    assert img.width() == clip._w and img.height() == clip._h
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert msgs == [], f"成功路径不得打日志：{msgs}"
