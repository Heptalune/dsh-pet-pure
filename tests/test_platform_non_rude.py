# -*- coding: utf-8 -*-
"""issue #296：铺满整屏的 overlay 合成窗让任务栏掉出置顶层。

背景（MS 文档 ``ITaskbarList2::MarkFullscreenWindow`` Remarks）：Shell 的
"rude window manager" 按窗口几何判定全屏应用——铺满整块显示器的窗口会让
任务栏被移到 z 序底部（用户观感「任务栏被所有悬浮窗挡住」）。4.3.0 起
overlay 是默认拓扑，每屏一张铺满 ``screen.geometry()`` 的透明置顶窗，正好
命中该判定；4.2.1 的 legacy 小窗不构成「铺满整屏」，所以没有这个问题。

官方豁免：``SetProp(hwnd, L"NonRudeHWND", HANDLE(TRUE))``，且必须在窗口
**显示之前**设置（本机探针确认 ``showEvent`` 时 ``IsWindowVisible=False``，
满足该前置条件）。属性挂在 HWND 上：``setWindowFlag`` 重建原生窗后丢失，
必须靠下一次显示重放——与 ``_set_windows_no_activate`` 同一套先例。

本文件只钉两件事：

1. 原语调用形状（属性名 / 值 / 返回值），用假 user32，不碰真实 Shell；
2. ``OverlayWindow.showEvent`` 真的接线，且每次显示都会重放（覆盖原生窗
   重建路径）。

「任务栏是否真的恢复置顶」是 Shell 行为，mock 没有意义，只能实机验证
（见交付报告的实机记录）。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import pet.overlay_window as overlay_window
from pet.overlay_window import OverlayWindow
from pet.platform_win import _set_windows_non_rude_hwnd

needs_win32 = pytest.mark.skipif(
    sys.platform != "win32", reason="NonRudeHWND 是 Windows Shell 专属属性")


class _FakeUser32:
    """只记录调用的假 user32：不加载真实 DLL，跨平台可跑。"""

    def __init__(self, result=1):
        self.result = result
        self.calls = []

    def SetPropW(self, hwnd, name, value):
        self.calls.append((hwnd, name, value))
        return self.result


def _prop_value(value):
    """``HANDLE(TRUE)`` 在真实调用里是 c_void_p；假 user32 原样收到。"""
    return getattr(value, "value", value)


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


# ------------------------------------------------------------ 原语调用形状

def test_marks_hwnd_as_non_rude():
    fake = _FakeUser32()
    assert _set_windows_non_rude_hwnd(0x1234, user32=fake) is True
    assert len(fake.calls) == 1
    hwnd, name, value = fake.calls[0]
    assert hwnd == 0x1234
    assert name == "NonRudeHWND", "属性名是 Shell 约定的固定字符串"
    assert _prop_value(value) == 1, "文档要求 HANDLE(TRUE)，不是 0/False"


def test_reports_failure():
    fake = _FakeUser32(result=0)
    assert _set_windows_non_rude_hwnd(0x1234, user32=fake) is False


# ------------------------------------------------------------ showEvent 接线

@needs_win32
def test_show_event_marks_native_window(monkeypatch):
    app = _qapp()
    calls = []
    monkeypatch.setattr(overlay_window, "_set_windows_non_rude_hwnd",
                        lambda hwnd: calls.append(hwnd), raising=False)
    overlay = OverlayWindow()
    try:
        overlay.show()
        app.processEvents()
        assert calls == [int(overlay.winId())]
    finally:
        overlay.close()
        overlay.deleteLater()
        app.processEvents()


@needs_win32
def test_show_event_replays_after_native_recreation(monkeypatch):
    """on_top 的 ``setWindowFlag`` 会重建原生窗，旧 HWND 上的属性随之消失。"""
    app = _qapp()
    calls = []
    monkeypatch.setattr(overlay_window, "_set_windows_non_rude_hwnd",
                        lambda hwnd: calls.append(hwnd), raising=False)
    overlay = OverlayWindow()
    try:
        overlay.show()
        app.processEvents()
        assert len(calls) == 1

        overlay.hide()
        app.processEvents()
        overlay.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        overlay.show()
        app.processEvents()
        assert len(calls) >= 2, "原生窗重建后必须重设 NonRudeHWND"
    finally:
        overlay.close()
        overlay.deleteLater()
        app.processEvents()
