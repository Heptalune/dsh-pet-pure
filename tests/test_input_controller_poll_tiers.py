# -*- coding: utf-8 -*-
"""Windows 逐像素穿透轮询的「包围盒两段式」回归（DS 审查 M12b）。

背景：``WindowsPerPixelInputController`` 原为恒定 10ms 轮询（拖拽 100ms）。
光标在窗口包围盒外时逐像素判定必然「无命中 → 穿透」，10ms 一次的
QCursor.pos()/mapFromGlobal/GetWindowLong 调用纯属空转（overlay 铺满整屏时
命中判定也要每 10ms 走一遍全部 sprite 的 alpha 查询）。

要求：光标在包围盒内 = 10ms（逐像素跟手），盒外 = 50ms（低频），拖拽仍
100ms 且优先级最高；松手恢复时按当前位置重新定档。另（O2）：目标穿透态
未变时不再读窗口样式（缓存 + 低频校正），state 变化/句柄变化/`stop`+`resume`
必须立即重放。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication

from pet import platform_win
from pet.platform_win import WindowsPerPixelInputController as Ctl

app = QApplication.instance() or QApplication([])

WIN_LEFT, WIN_TOP, WIN_W, WIN_H = 100, 200, 400, 300


class _FakeCursor:
    """可设定的假光标（QCursor.pos 静态方法替身）。"""

    position = QPoint(1000, 1000)

    @classmethod
    def pos(cls):
        return QPoint(cls.position)


class _FakeWindow:
    def __init__(self):
        self.mouse_through = False
        self._press_global = None
        self.visible = True

    def winId(self):
        return getattr(self, "hwnd", 4242)

    def mapFromGlobal(self, point):
        return QPoint(point.x() - WIN_LEFT, point.y() - WIN_TOP)

    def _is_transparent_at(self, local):
        return True

    def isVisible(self):
        return self.visible

    def width(self):
        return WIN_W

    def height(self):
        return WIN_H


class _FakeUser32:
    """user32 替身：数 GetWindowLongW/SetWindowLongW，不碰真实 Win32。"""

    def __init__(self):
        self.style = 0
        self.reads = 0
        self.writes = 0

    def GetWindowLongW(self, hwnd, index):
        self.reads += 1
        return self.style

    def SetWindowLongW(self, hwnd, index, value):
        self.writes += 1
        self.style = value
        return 0


_WS_EX_TRANSPARENT = 0x00000020


def _make_controller(monkeypatch):
    """绕过 __init__（不碰真实 Win32 样式），拼出与生产同形的控制器。"""
    monkeypatch.setattr(platform_win, "QCursor", _FakeCursor)
    clicks: list[bool] = []
    monkeypatch.setattr(platform_win, "_set_windows_click_through",
                        lambda hwnd, enabled: clicks.append(bool(enabled)))
    controller = object.__new__(Ctl)
    controller._window = _FakeWindow()
    controller._timer = QTimer()
    controller._timer.setInterval(Ctl.NORMAL_POLL_INTERVAL_MS)
    return controller, clicks


def _make_counting_controller(monkeypatch):
    """真控制器 + 记账 user32：refresh 内部的 GetWindowLongW 次数可数。"""
    monkeypatch.setattr(platform_win, "QCursor", _FakeCursor)
    user32 = _FakeUser32()
    real = platform_win._set_windows_click_through
    monkeypatch.setattr(
        platform_win, "_set_windows_click_through",
        lambda hwnd, enabled: real(hwnd, enabled, user32))
    controller = object.__new__(Ctl)
    controller._window = _FakeWindow()
    controller._timer = QTimer()
    controller._timer.setInterval(Ctl.NORMAL_POLL_INTERVAL_MS)
    return controller, user32


def test_poll_tiers_constants():
    assert Ctl.NORMAL_POLL_INTERVAL_MS == 10
    assert Ctl.IDLE_POLL_INTERVAL_MS == 50
    assert Ctl.DRAG_POLL_INTERVAL_MS == 100
    assert Ctl.IDLE_POLL_INTERVAL_MS > Ctl.NORMAL_POLL_INTERVAL_MS


def test_refresh_switches_tier_by_bounding_box(monkeypatch):
    """盒内 10ms / 盒外 50ms：同一 refresh 入口按光标位置定档。"""
    controller, clicks = _make_controller(monkeypatch)

    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)   # 盒内
    controller.refresh()
    assert controller._timer.interval() == Ctl.NORMAL_POLL_INTERVAL_MS
    assert clicks == [True]                                      # 盒内空处：穿透

    _FakeCursor.position = QPoint(WIN_LEFT - 50, WIN_TOP - 50)   # 盒外
    controller.refresh()
    assert controller._timer.interval() == Ctl.IDLE_POLL_INTERVAL_MS

    _FakeCursor.position = QPoint(WIN_LEFT + WIN_W + 5, WIN_TOP + 5)  # 右缘外
    controller.refresh()
    assert controller._timer.interval() == Ctl.IDLE_POLL_INTERVAL_MS

    _FakeCursor.position = QPoint(WIN_LEFT + WIN_W - 1, WIN_TOP + WIN_H - 1)
    controller.refresh()
    assert controller._timer.interval() == Ctl.NORMAL_POLL_INTERVAL_MS


def test_drag_tier_wins_over_cursor_position(monkeypatch):
    """拖拽中 refresh 不得把 100ms 打回按位置定的档（事件必须持续送达）。"""
    controller, _clicks = _make_controller(monkeypatch)
    controller._window._press_global = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller._timer.setInterval(Ctl.DRAG_POLL_INTERVAL_MS)

    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)   # 盒内
    controller.refresh()
    assert controller._timer.interval() == Ctl.DRAG_POLL_INTERVAL_MS

    _FakeCursor.position = QPoint(0, 0)                          # 盒外
    controller.refresh()
    assert controller._timer.interval() == Ctl.DRAG_POLL_INTERVAL_MS


def test_release_resumes_position_tier(monkeypatch):
    """松手：先按当前位置重新定档并强制刷新一次穿透状态。"""
    controller, _clicks = _make_controller(monkeypatch)
    controller._window._press_global = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller._timer.setInterval(Ctl.DRAG_POLL_INTERVAL_MS)

    _FakeCursor.position = QPoint(0, 0)                          # 盒外
    controller._window._press_global = None
    controller.set_drag_active(False)
    assert controller._timer.interval() == Ctl.IDLE_POLL_INTERVAL_MS

    controller._timer.setInterval(Ctl.DRAG_POLL_INTERVAL_MS)
    controller._window._press_global = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    _FakeCursor.position = QPoint(WIN_LEFT + 20, WIN_TOP + 20)   # 盒内
    controller._window._press_global = None
    controller.set_drag_active(False)
    assert controller._timer.interval() == Ctl.NORMAL_POLL_INTERVAL_MS


# ---------------------------------------------------------------- 穿透态缓存（O2）
def test_static_state_skips_win32_style_read(monkeypatch):
    """目标穿透态未变时不再读窗口样式：1s 静止轮询从 100 次读降到 ≤2 次。

    低频校正（每 ``RECHECK_EVERY_N_REFRESHES`` 次 refresh 实读一次）兜住
    未知的外部样式改写；这里只跑 60 次，落在校正窗口内。
    """
    controller, user32 = _make_counting_controller(monkeypatch)
    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)   # 盒内：10ms 档
    for _ in range(60):
        controller.refresh()
    assert user32.reads <= 2, "静止期不得每次 refresh 都读样式"
    assert user32.writes <= 1
    assert user32.style & _WS_EX_TRANSPARENT                     # 首次已写入穿透


def test_target_state_change_reapplies_through(monkeypatch):
    """目标态变化（盒内空处 → 盒外）必须立即重写样式。"""
    controller, user32 = _make_counting_controller(monkeypatch)
    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller.refresh()
    assert user32.style & _WS_EX_TRANSPARENT
    _FakeCursor.position = QPoint(0, 0)                          # 盒外：不穿透
    controller.refresh()
    assert not (user32.style & _WS_EX_TRANSPARENT)
    reads_before = user32.reads
    controller.refresh()                                         # 静止：跳过
    assert user32.reads == reads_before


def test_window_handle_change_reapplies_through(monkeypatch):
    """原生窗口重建（setWindowFlag）后句柄变化：必须重放样式，不能信任旧缓存。"""
    controller, user32 = _make_counting_controller(monkeypatch)
    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller.refresh()
    reads_before = user32.reads
    controller._window.hwnd = 9999                               # 新窗口：样式从零开始
    user32.style = 0
    controller.refresh()
    assert user32.reads > reads_before, "句柄变化必须重读样式"
    assert user32.style & _WS_EX_TRANSPARENT, "新窗口必须重新写入穿透态"


def test_mouse_through_flag_change_reapplies_through(monkeypatch):
    """``mouse_through`` 变了（外部样式写入口必先改它）→ 缓存立即作废重放。"""
    controller, user32 = _make_counting_controller(monkeypatch)
    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller.refresh()
    user32.style = 0                                              # 模拟外部改样式
    controller._window.mouse_through = True
    controller.refresh()
    assert user32.style & _WS_EX_TRANSPARENT


def test_low_frequency_recheck_corrects_external_style_write(monkeypatch):
    """未知的外部样式改写：低频校正窗口内必须自愈（旧实现靠每次读样式兜住）。"""
    controller, user32 = _make_counting_controller(monkeypatch)
    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller.refresh()
    user32.style = 0                                              # 外部清掉穿透位
    for _ in range(Ctl.RECHECK_EVERY_N_REFRESHES):
        controller.refresh()
    assert user32.style & _WS_EX_TRANSPARENT, "低频校正必须把样式收敛回目标态"


def test_stop_resets_cache_and_resume_reapplies(monkeypatch):
    """stop 复位缓存（停表期间样式可能被外部改写）；resume 立即重写穿透态。"""
    controller, user32 = _make_counting_controller(monkeypatch)
    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller.refresh()
    assert user32.style & _WS_EX_TRANSPARENT
    controller.stop()
    reads_after_stop = user32.reads
    controller.resume()
    assert controller._timer.isActive()
    assert user32.reads > reads_after_stop, "缓存已复位 → resume 必须重读样式"
    assert user32.style & _WS_EX_TRANSPARENT, "resume 必须立即写回穿透态"
