# -*- coding: utf-8 -*-
"""overlay 拓扑「死开关」接线回归（DS 全量审查 M5 a-c）。

背景：``shift_drag`` / ``lock_position`` / ``pet_opacity`` 三个持久化设置在旧
架构（``pet/window.py``）生效，但 overlay 侧零引用——设置页改了没反应、
重启即丢。M5d/e/f 见 test_sprite_animation_gap.py /
test_overlay_golden_spin_click.py / test_overlay_music_sing.py。

每条对应一条「旧语义 → overlay 落点」：
- a) ``shift_drag``：未按 SHIFT 的按下不进拖拽 grab（window.py:3172-3181）；
- b) ``lock_position``：锁定后不可拖动，点击互动保留（window.py:3126-3133）；
- c) ``pet_opacity``：整 overlay 不透明度（window.py:3853-3857/4134-4146）。

纪律：offscreen；同步直调事件处理器与 handler，不起真实 QTimer、不 sleep
赌时序（AGENTS.md 时序测试纪律）。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

import tests.test_sprite_menu_facade as fac
from pet.pet_sprite import INTERACTION_DRAG, INTERACTION_NORMAL

app = QApplication.instance() or QApplication([])


def _make_shell(tmp_path, values=None):
    """真实 PetSprite + 真 overlay 的壳（复用 facade 的测试装配）。"""
    shell, lib = fac._make_shell(tmp_path, values)
    # 点击音效在接线测试里只会引入无关副作用：显式摘掉
    shell.overlay.click_feedback = None
    return shell, lib


def _mouse_event(etype, pos, *, modifiers=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(etype, QPointF(*pos), QPointF(*pos),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                       modifiers)


def _hit_pos(sprite):
    """sprite 内部一个必然命中的 overlay 局部坐标（帧全不透明）。"""
    rect = sprite.rect()
    return (rect.center().x(), rect.center().y())


# ---------------------------------------------------------------- M5a/M5b：拖拽闸门
def test_lock_position_press_is_click_only_and_still_clicks(tmp_path):
    """锁定位：命中按下不进拖拽 grab（不 on_press/不升级），松手仍按点击处理。"""
    shell, _lib = _make_shell(tmp_path, {"lock_position": True})
    try:
        sprite = shell.sprite
        sprite.bind_clip("idle1")
        sprite._rebuild_pixmap()
        overlay = shell.overlay
        assert overlay.lock_position is True

        clicked: list = []
        shell.behavior.on_sprite_clicked = (
            lambda target: (clicked.append(target), True)[1])

        pos = _hit_pos(sprite)
        press = _mouse_event(QEvent.Type.MouseButtonPress, pos)
        overlay.mousePressEvent(press)
        assert press.isAccepted()
        assert overlay._press_click_only is True
        assert overlay._press_global is None          # 无拖拽锚点
        assert sprite.interaction_state == INTERACTION_NORMAL

        # 大位移也不升级为拖拽（锁定语义）
        move = _mouse_event(QEvent.Type.MouseMove, (pos[0] + 300, pos[1]))
        overlay.mouseMoveEvent(move)
        assert overlay._drag_committed is False
        assert sprite.interaction_state == INTERACTION_NORMAL
        assert not sprite.dragging

        release = _mouse_event(QEvent.Type.MouseButtonRelease,
                               (pos[0] + 300, pos[1]))
        overlay.mouseReleaseEvent(release)
        assert clicked == [sprite], "锁定后点击互动必须保留"
    finally:
        shell._delete_runtime_marker()


def test_shift_drag_requires_shift_and_falls_back_to_click(tmp_path):
    """SHIFT 门：未按 SHIFT 的按下不进拖拽 grab（大位移也不升级），按 SHIFT 恢复。"""
    shell, _lib = _make_shell(tmp_path, {"shift_drag": True})
    try:
        sprite = shell.sprite
        sprite.bind_clip("idle1")
        sprite._rebuild_pixmap()
        overlay = shell.overlay
        assert overlay.shift_drag_required is True

        pos = _hit_pos(sprite)
        overlay.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, pos))
        assert overlay._press_click_only is True
        assert overlay._press_global is None
        overlay.mouseMoveEvent(
            _mouse_event(QEvent.Type.MouseMove, (pos[0] + 300, pos[1])))
        assert overlay._drag_committed is False
        assert sprite.interaction_state == INTERACTION_NORMAL
        overlay.mouseReleaseEvent(_mouse_event(
            QEvent.Type.MouseButtonRelease, (pos[0] + 300, pos[1])))
        # 首次松手走了点击分支（绑 click clip → 命中图作废）：按真实 tick 语义
        # 重建命中图，再验证 SHIFT 门放行
        sprite._rebuild_pixmap()

        # 按住 SHIFT：恢复既有拖拽语义（过阈值升级真拖拽）
        press = _mouse_event(QEvent.Type.MouseButtonPress, pos,
                             modifiers=Qt.KeyboardModifier.ShiftModifier)
        overlay.mousePressEvent(press)
        assert overlay._press_click_only is False
        assert overlay._press_global is not None
        overlay.mouseMoveEvent(_mouse_event(
            QEvent.Type.MouseMove, (pos[0] + 300, pos[1]),
            modifiers=Qt.KeyboardModifier.ShiftModifier))
        assert sprite.interaction_state == INTERACTION_DRAG
        overlay.mouseReleaseEvent(_mouse_event(
            QEvent.Type.MouseButtonRelease, (pos[0] + 300, pos[1]),
            modifiers=Qt.KeyboardModifier.ShiftModifier))
    finally:
        shell._delete_runtime_marker()


def test_drag_gates_follow_config_hot_change(tmp_path):
    """开关经 _apply_window_capabilities 推到 overlay，refresh_settings 即改即生效。"""
    shell, _lib = _make_shell(tmp_path, {"lock_position": True, "shift_drag": False})
    try:
        assert shell.overlay.lock_position is True
        assert shell.overlay.shift_drag_required is False
        shell._config.set("lock_position", False)
        shell._config.set("shift_drag", True)
        shell.refresh_settings()
        assert shell.overlay.lock_position is False
        assert shell.overlay.shift_drag_required is True
    finally:
        shell._delete_runtime_marker()


def test_ungated_press_unchanged(tmp_path):
    """两开关都关（默认）：按下即 grab 锚点（既有语义逐位不变）。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        sprite = shell.sprite
        sprite.bind_clip("idle1")
        sprite._rebuild_pixmap()
        pos = _hit_pos(sprite)
        press = _mouse_event(QEvent.Type.MouseButtonPress, pos)
        shell.overlay.mousePressEvent(press)
        assert shell.overlay._press_click_only is False
        assert shell.overlay._press_global is not None
        shell.overlay.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, pos))
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- M5c：不透明度
def test_pet_opacity_applied_on_build_and_refresh(tmp_path):
    """pet_opacity 启动即应用，改了 refresh_settings 热生效（整 overlay 单窗）。"""
    shell, _lib = _make_shell(tmp_path, {"pet_opacity": 40})
    try:
        assert abs(shell.overlay.windowOpacity() - 0.40) < 0.01
        shell._config.set("pet_opacity", 80)
        shell.refresh_settings()
        assert abs(shell.overlay.windowOpacity() - 0.80) < 0.01
    finally:
        shell._delete_runtime_marker()


def test_pet_opacity_clamped_to_legacy_range():
    """10-100 钳制（window.py:3853-3857 口径）。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    overlay.apply_opacity(3)
    assert abs(overlay.windowOpacity() - 0.10) < 0.01
    overlay.apply_opacity(999)
    assert abs(overlay.windowOpacity() - 1.0) < 0.01
