# -*- coding: utf-8 -*-
"""托盘图标首帧刷新（overlay 等价 legacy app.py:3132-3148 的占位+frame_ready）。

回归：托盘图标只在 _build_tray 建造时取一次，而建造跑在 overlay.show() 之前，
sprite 首帧未上屏、clip 未起播，icon_pixmap 只能回退系统占位图标——不刷新
就永远显示不出鱼（用户「托盘栏没有桌宠图标」；上游 b37a44c 只修了 legacy
PetWindow 路径，overlay 壳是另一条建造链）。
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

import tests.test_overlay_window_capabilities as cap
import tests.test_sprite_menu_facade as fac
from pet.overlay_shell import OverlayShell
from pet.pet_sprite import PetSprite

app = QApplication.instance() or QApplication([])


def _make_shell(tmp_path):
    config = cap.CapConfig(tmp_path, {})
    instance = cap.CapInstance(config)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    lib = fac.RichLibrary()
    shell.lib = lib
    shell.sprite.library = lib
    return shell


def _pixmap() -> QPixmap:
    pm = QPixmap(32, 32)
    pm.fill(QColor(200, 80, 80))
    return pm


def test_tray_icon_refreshes_when_first_frame_arrives(tmp_path):
    """建造时无帧（占位图标）→ 帧就绪后轮询把托盘图标换成角色头像并停表。"""
    shell = _make_shell(tmp_path)
    try:
        assert shell.tray is not None
        calls = {"n": 0}
        real_icon_pixmap = shell.icon_pixmap

        def fake_icon_pixmap(size=64):
            calls["n"] += 1
            if calls["n"] <= 3:
                return None  # 手动一拍 + arm 内一拍 + 首轮一拍：首帧未就绪
            return _pixmap()

        shell.icon_pixmap = fake_icon_pixmap

        assert shell._refresh_tray_icon_once() is False  # 无帧：不换
        shell._arm_tray_icon_refresh()  # 无帧 → 进入轮询
        assert shell._tray_icon_timer.isActive() is True

        shell._poll_tray_icon()  # 第 2 拍仍无帧：继续轮询
        assert shell._tray_icon_timer.isActive() is True

        shell._poll_tray_icon()  # 第 3 拍有帧：换头像 + 停表
        assert not shell.tray.icon().isNull()
        assert shell._tray_icon_timer.isActive() is False

        shell.icon_pixmap = real_icon_pixmap
        shell._delete_runtime_marker()
    finally:
        timer = getattr(shell, "_tray_icon_timer", None)
        if timer is not None:
            timer.stop()
        shell._delete_runtime_marker()


def test_tray_icon_immediate_when_frame_already_ready(tmp_path):
    """start 时首帧已就绪：同步换好，不起轮询表。"""
    shell = _make_shell(tmp_path)
    try:
        shell.icon_pixmap = lambda size=64: _pixmap()
        shell._arm_tray_icon_refresh()
        assert not shell.tray.icon().isNull()
        assert getattr(shell, "_tray_icon_timer", None) is None
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_tray_icon_fallback_is_pet_pawprint(tmp_path):
    """首帧未就绪时的兜底图标 = legacy 的宠物爪印（不是系统「电脑」图标）。

    legacy ``app.py:3127`` 的 ``_tray_placeholder_icon`` 是
    ``vector_menu_icon(QWidget(), "pet", 64)``；overlay 此前回退
    ``SP_ComputerIcon``——首帧未就绪期间用户在托盘里看到的是电脑图标，
    观感等同「托盘栏里没有桌宠图标」。
    """
    from PySide6.QtWidgets import QWidget

    from pet.context_menus.icons import vector_menu_icon

    shell = _make_shell(tmp_path)
    try:
        shell.icon_pixmap = lambda size=64: None       # 帧未就绪
        expected = vector_menu_icon(QWidget(), "pet", 64)
        got = shell._tray_icon()
        assert got.pixmap(64).toImage() == expected.pixmap(64).toImage(), \
            "兜底图标必须与 legacy 占位图标同图（爪印），不是系统图标"
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_tray_icon_poll_keeps_retrying_at_low_rate_after_budget(tmp_path):
    """拿不到帧也不放弃：超预算后降频（5s）继续等，只有拿到帧才停表。

    旧断言把「20 拍（10s）后永久放弃」锁成期望——首帧晚于 10s（首跑帧序列
    转码、慢启动、内存压力）时托盘**整个进程生命期**停在兜底图标；legacy 用的是
    无时限的 ``frame_ready``（app.py:3185-3192 + window.py:347）。降频是为了不
    常驻 500ms 高频表。
    """
    shell = _make_shell(tmp_path)
    try:
        shell.icon_pixmap = lambda size=64: None
        shell._arm_tray_icon_refresh()
        assert shell._tray_icon_timer.interval() == 500
        for _ in range(20):
            shell._poll_tray_icon()
        assert shell._tray_icon_timer.isActive() is True, "超预算不得停表（要能自愈）"
        assert shell._tray_icon_timer.interval() == 5000, "超预算后必须降频"

        # 帧终于就绪：换头像并停表（此时才允许停）
        shell.icon_pixmap = lambda size=64: _pixmap()
        shell._poll_tray_icon()
        img = shell.tray.icon().pixmap(32).toImage()
        assert img.pixelColor(16, 16) == QColor(200, 80, 80), "拿到帧必须换上头像"
        assert shell._tray_icon_timer.isActive() is False
        shell._delete_runtime_marker()
    finally:
        timer = getattr(shell, "_tray_icon_timer", None)
        if timer is not None:
            timer.stop()
        shell._delete_runtime_marker()
