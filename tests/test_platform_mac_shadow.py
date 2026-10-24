# -*- coding: utf-8 -*-
"""macOS 关闭窗口系统阴影（issue #229）回归。

现象：macOS 桌宠窗口外圈有一圈系统阴影黑边，普通模式与直播捕获模式都有。
要求：两个模式都关掉，且不引入任何非 macOS 平台的行为变化。

落点（纯 Qt，无 objc ctypes）：Qt 的 cocoa 插件把窗口标志
``Qt::NoDropShadowWindowHint`` 直接翻成 ``[NSWindow setHasShadow:NO]``
（qtbase 6.8 ``QCocoaWindow::setWindowFlags``，qcocoawindow.mm:726；同文件的
窗口创建路径也会按同一标志重新下发）。标志跟着窗口 flags 走，Qt 每次创建原生
窗口（``on_top`` 开关、捕获模式开关都会重建）都会重新应用，所以注入点必须在
**flags 构造处**而不是「显示后补写」——``setWindowFlag`` 在可见窗口上是原生
窗口重建（频闪），没必要。

平台分派放在 ``pet/platform_mac.py::_mac_no_window_shadow_flag``：非 darwin 返回
空标志（``flags |= 0`` 天然 no-op），调用点无需自行判平台。本文件钉四件事：
标志的 darwin 分派、overlay 主窗 flags、legacy/捕获模式窗 flags、原生窗口重建后
标志仍在。

未在本机验证（诚实登记）：本机是 Windows，macOS 上阴影真的消失无法在这里执行；
断言到「标志在窗口 flags 里」这一层，由 CI macOS 与实机验收（见交付报告
「未实测项」）。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pet import platform_mac
from pet.config import Config
from pet.overlay_window import OverlayWindow
from pet.window import PetWindow, build_window_flags
from tests.test_window_pause import FakeLibrary

app = QApplication.instance() or QApplication([])

NO_SHADOW = Qt.WindowType.NoDropShadowWindowHint


# ---------------------------------------------------------------- 平台分派（纯函数）
@pytest.mark.parametrize("platform,expected", [
    ("darwin", True),
    ("win32", False),
    ("linux", False),
])
def test_no_window_shadow_flag_dispatch(monkeypatch, platform, expected):
    """只有 darwin 拿到标志；其它平台返回空标志（可直接 ``|=``，天然 no-op）。"""
    monkeypatch.setattr(sys, "platform", platform)
    flags = platform_mac._mac_no_window_shadow_flag()
    if expected:
        assert flags & NO_SHADOW
    else:
        assert flags == Qt.WindowType(0)


# ---------------------------------------------------------------- overlay 主窗
def test_overlay_window_has_no_shadow_flag_on_darwin(monkeypatch):
    """overlay 主窗（新架构默认拓扑 = 普通模式）在 darwin 带关阴影标志。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    overlay = OverlayWindow()
    try:
        assert overlay.windowFlags() & NO_SHADOW
    finally:
        overlay.close()


def test_overlay_window_keeps_shadow_flag_off_darwin(monkeypatch):
    """非 darwin 不加标志：不给 Windows/X11 引入无谓的窗口行为变化。"""
    monkeypatch.setattr(sys, "platform", "win32")
    overlay = OverlayWindow()
    try:
        assert not overlay.windowFlags() & NO_SHADOW
    finally:
        overlay.close()


def test_overlay_shadow_flag_survives_on_top_toggle(monkeypatch):
    """on_top 开关（运行时改 flags，Qt 会重建原生窗口）后标志仍在。

    这正是注入点选在 flags 构造处、而不是「显示后补写」的理由：标志随窗口 flags
    走，Qt 每次创建原生窗口都按它重新下发 ``hasShadow = NO``。运行时唯一会碰
    overlay flags 的调用点是 ``ShellOverlayWindow`` 的 on_top 开关，用的是位修改
    版 ``setWindowFlag``（pet/overlay_shell.py:3225），其余标志原样保留。
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    overlay = OverlayWindow()
    try:
        overlay.show()
        app.processEvents()
        assert overlay.windowFlags() & NO_SHADOW

        overlay.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        overlay.show()
        app.processEvents()
        assert overlay.windowFlags() & NO_SHADOW, "改单个标志后必须仍关阴影"
        assert overlay.windowHandle().flags() & NO_SHADOW

        overlay.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        overlay.show()
        app.processEvents()
        assert overlay.windowHandle().flags() & NO_SHADOW
    finally:
        overlay.close()


# ---------------------------------------------------------------- legacy / 直播捕获模式窗
def test_build_window_flags_covers_capture_mode_on_darwin(monkeypatch):
    """flags 工厂：普通模式与直播捕获模式都要带关阴影标志（issue #229 两颗都要）。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    config = {"on_top": True, "mouse_through": False}
    for capture_mode in (False, True):
        flags = build_window_flags(config, False, capture_mode)
        assert flags & NO_SHADOW, f"stream_capture_mode={capture_mode} 必须关阴影"


def test_build_window_flags_untouched_off_darwin(monkeypatch):
    """非 darwin：flags 构造结果逐位不变（不加任何新标志）。"""
    config = {"on_top": True, "mouse_through": False}
    for platform in ("win32", "linux"):
        monkeypatch.setattr(sys, "platform", platform)
        for capture_mode in (False, True):
            assert not build_window_flags(config, False, capture_mode) & NO_SHADOW


def test_petwindow_covers_normal_and_capture_mode_on_darwin(monkeypatch, tmp_path):
    """真窗口：legacy 主窗普通模式与切到直播捕获模式后都带关阴影标志。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    win = PetWindow(FakeLibrary(), Config(base=tmp_path))
    try:
        assert win.windowFlags() & NO_SHADOW
        win.set_stream_capture_mode(True)          # 重建原生窗口 + 重新算 flags
        assert win.windowFlags() & NO_SHADOW, "直播捕获模式必须同样关阴影"
        win.set_stream_capture_mode(False)
        assert win.windowFlags() & NO_SHADOW
    finally:
        win.close()
        app.processEvents()
