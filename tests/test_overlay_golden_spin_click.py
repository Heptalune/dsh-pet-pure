# -*- coding: utf-8 -*-
"""点击触发黄金回旋（``golden_spin_on_click``）overlay 接线回归（DS 审查 M5e）。

旧架构落点：``window.py:3356-3357`` 的 ``_effects_route_click_golden_spin`` +
``window_optional_services.py:242-265`` 的直连/armed 两模式。overlay 侧此前
只把该键接在设置页与 legacy 窗口上——点击链路完全没读它（死开关）。

- 直连模式（``golden_spin_direct`` 或角色无点击素材）：点击立即回旋并累计圈数；
- armed 模式（有点击素材且未开直连）：点击动画播完后接续旋转；
- 探头激活时不叠加（旧机同纪律）。

纪律：offscreen；同步直调事件处理器/接续回调，不启动真实 QTimer、不 sleep。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from tests.test_overlay_dead_switches import (
    _hit_pos,
    _make_shell,
    _mouse_event,
)

app = QApplication.instance() or QApplication([])


def test_golden_spin_on_click_direct_accumulates(tmp_path):
    """直连模式：点击被效果层消费（不播点击动画），旋转中再点累计圈数。"""
    shell, _lib = _make_shell(
        tmp_path, {"golden_spin_on_click": True, "golden_spin_direct": True})
    try:
        sprite = shell.sprite
        sprite.bind_clip("idle1")
        sprite._rebuild_pixmap()
        clicked: list = []
        shell.behavior.on_sprite_clicked = (
            lambda target: (clicked.append(target), True)[1])

        pos = _hit_pos(sprite)
        shell.overlay.mousePressEvent(
            _mouse_event(QEvent.Type.MouseButtonPress, pos))
        shell.overlay.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, pos))

        spin = shell._golden_spin
        assert spin is not None and spin.active
        assert spin.queued_turns == 1
        assert clicked == [], "直连模式点击由效果层消费，不再播点击反应"

        # 旋转中再点一次：累计下一圈，不打断当前旋转
        shell.overlay.mousePressEvent(
            _mouse_event(QEvent.Type.MouseButtonPress, pos))
        shell.overlay.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, pos))
        assert spin.queued_turns == 2
    finally:
        shell._delete_runtime_marker()


def test_golden_spin_on_click_armed_after_click_anim(tmp_path):
    """armed 模式（有点击素材且未开直连）：点击动画播完后再接续旋转。"""
    shell, _lib = _make_shell(tmp_path, {"golden_spin_on_click": True})
    try:
        sprite = shell.sprite
        sprite.bind_clip("idle1")
        sprite._rebuild_pixmap()
        pos = _hit_pos(sprite)
        shell.overlay.mousePressEvent(
            _mouse_event(QEvent.Type.MouseButtonPress, pos))
        shell.overlay.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, pos))

        spin = shell._golden_spin
        assert spin is not None and spin.pending_after_click is True
        assert not spin.active
        # 点击动画播完（一次性状态下降沿）→ 接续旋转
        shell._on_click_anim_finished()
        assert spin.active
    finally:
        shell._delete_runtime_marker()


def test_golden_spin_off_by_default_keeps_click_path(tmp_path):
    """开关默认关：点击链路逐位不变（不建控制器、不旋转）。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        sprite = shell.sprite
        sprite.bind_clip("idle1")
        sprite._rebuild_pixmap()
        clicked: list = []
        shell.behavior.on_sprite_clicked = (
            lambda target: (clicked.append(target), True)[1])
        pos = _hit_pos(sprite)
        shell.overlay.mousePressEvent(
            _mouse_event(QEvent.Type.MouseButtonPress, pos))
        shell.overlay.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, pos))
        assert clicked == [sprite]
        assert shell._golden_spin is None or not shell._golden_spin.active
    finally:
        shell._delete_runtime_marker()
