# -*- coding: utf-8 -*-
"""Phase 3a offscreen 单测：overlay 右键菜单最小集 + 右键不进拖拽 grab。

纪律同 test_overlay_window.py：同步构造事件直调处理器，不启动真实事件循环、
不固定 sleep 赌时序；菜单不真 exec（monkeypatch QMenu.exec 记录接线）。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QMenu

from pet.overlay_window import OverlayWindow
from pet.sprite_menu import SCALE_PRESETS, build_sprite_menu
from tests.test_overlay_window import FakeSprite

app = QApplication.instance() or QApplication([])


class FakeBehavior:
    """记录 on_sprite_clicked 调用的行为控制器桩。"""

    def __init__(self):
        self.clicked: list = []

    def on_sprite_clicked(self, sprite):
        self.clicked.append(sprite)


def _make_sprite(scale=0.72):
    sprite = FakeSprite((0, 0), (100, 100))
    sprite.scale = scale
    return sprite


def _top_level(menu: QMenu):
    return [(a.text(), a.isSeparator()) for a in menu.actions()]


def _find(menu: QMenu, text: str):
    for action in menu.actions():
        if action.text() == text:
            return action
    raise AssertionError(f"菜单缺少动作: {text}")


def _mouse_event(etype, pos, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(etype, QPointF(*pos), QPointF(*pos),
                       button, button, Qt.KeyboardModifier.NoModifier)


# ---------------------------------------------------------------- 菜单构建
def test_menu_structure_and_check_states():
    overlay = OverlayWindow()
    sprite = _make_sprite(scale=0.72)
    menu = build_sprite_menu(overlay, sprite, behavior=FakeBehavior())

    top = _top_level(menu)
    texts = [t for t, is_sep in top if not is_sep]
    assert texts == ["戳一下", "大小", "鼠标穿透", "退出"]
    assert sum(1 for _, is_sep in top if is_sep) == 2

    size_action = _find(menu, "大小")
    size_menu = size_action.menu()
    assert size_menu is not None
    scale_actions = size_menu.actions()
    assert [a.data() for a in scale_actions] == list(SCALE_PRESETS)
    assert [a.text() for a in scale_actions] == ["50%", "72%", "100%", "130%"]
    # 勾选当前档（0.72），其余不勾；全部 checkable
    assert [a.isChecked() for a in scale_actions] == [False, True, False, False]
    assert all(a.isCheckable() for a in scale_actions)

    through = _find(menu, "鼠标穿透")
    assert through.isCheckable() and not through.isChecked()


def test_poke_action_enabled_and_calls_behavior():
    overlay = OverlayWindow()
    sprite = _make_sprite()
    behavior = FakeBehavior()
    menu = build_sprite_menu(overlay, sprite, behavior=behavior)
    poke = _find(menu, "戳一下")
    assert poke.isEnabled()
    poke.trigger()
    assert behavior.clicked == [sprite]


def test_poke_action_disabled_without_behavior():
    overlay = OverlayWindow()
    menu = build_sprite_menu(overlay, _make_sprite(), behavior=None)
    assert not _find(menu, "戳一下").isEnabled()


def test_scale_action_sets_sprite_scale_and_moves_check():
    overlay = OverlayWindow()
    sprite = _make_sprite(scale=0.72)
    menu = build_sprite_menu(overlay, sprite, behavior=None)
    scale_actions = _find(menu, "大小").menu().actions()

    scale_actions[3].trigger()  # 130%
    assert sprite.scale == 1.3
    assert [a.isChecked() for a in scale_actions] == [False, False, False, True]

    scale_actions[0].trigger()  # 50%
    assert sprite.scale == 0.5
    assert [a.isChecked() for a in scale_actions] == [True, False, False, False]


def test_mouse_through_toggle_writes_overlay():
    overlay = OverlayWindow()
    assert overlay.mouse_through is False
    menu = build_sprite_menu(overlay, _make_sprite(), behavior=None)
    through = _find(menu, "鼠标穿透")

    through.trigger()
    assert overlay.mouse_through is True
    through.trigger()
    assert overlay.mouse_through is False

    # 初始勾选态跟随 overlay 现状
    overlay.mouse_through = True
    menu2 = build_sprite_menu(overlay, _make_sprite(), behavior=None)
    assert _find(menu2, "鼠标穿透").isChecked()


def test_quit_action_calls_app_quit(monkeypatch):
    calls = []
    monkeypatch.setattr(QApplication, "quit", lambda: calls.append(1))
    overlay = OverlayWindow()
    menu = build_sprite_menu(overlay, _make_sprite(), behavior=None)
    _find(menu, "退出").trigger()
    assert calls == [1]


# ---------------------------------------------------------------- 鼠标路由：右键不进 grab
def test_right_button_press_does_not_grab():
    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (100, 100))
    overlay.add_sprite(sprite)

    press = _mouse_event(QEvent.Type.MouseButtonPress, (30, 30),
                         button=Qt.MouseButton.RightButton)
    overlay.mousePressEvent(press)
    assert overlay._mouse_grab is None          # 右键不进拖拽 grab
    assert overlay._press_global is None        # 穿透轮询不进入拖拽态
    assert not sprite.press_events              # sprite 不收到 press
    assert not press.isAccepted()

    # 左键语义不变：命中即 grab
    left = _mouse_event(QEvent.Type.MouseButtonPress, (30, 30))
    overlay.mousePressEvent(left)
    assert overlay._mouse_grab is sprite
    assert len(sprite.press_events) == 1


def test_context_menu_event_hit_execs_menu_miss_ignores(monkeypatch):
    overlay = OverlayWindow()
    overlay.behavior = FakeBehavior()  # 集成层挂载点（基类 getattr 读取）
    sprite = FakeSprite((0, 0), (100, 100))
    sprite.scale = 0.72
    overlay.add_sprite(sprite)

    # PySide6 的 QMenu.exec 类级 monkeypatch 不生效（shiboken 方法表绕过
    # Python 类型字典，实测类级 patch 后真 exec 仍跑模态）——包装工厂函数，
    # 给返回的 menu 装实例级 exec 桩（实例字典优先，生效）
    import pet.overlay_window as overlay_mod

    execs = []
    real_build = overlay_mod.build_sprite_menu

    def spy_build(overlay_, sprite_, *, behavior=None):
        menu = real_build(overlay_, sprite_, behavior=behavior)
        menu.exec = lambda *args: execs.append((menu, args)) or 0
        return menu

    monkeypatch.setattr(overlay_mod, "build_sprite_menu", spy_build)

    hit = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                            QPoint(30, 30), QPoint(300, 300))
    overlay.contextMenuEvent(hit)
    assert len(execs) == 1                        # 命中：弹出菜单
    menu, args = execs[0]
    assert args[0] == QPoint(300, 300)            # 在全局光标处弹出
    assert _find(menu, "戳一下").isEnabled()      # behavior 已接线
    assert hit.isAccepted()

    miss = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                             QPoint(400, 400), QPoint(700, 700))
    overlay.contextMenuEvent(miss)
    assert len(execs) == 1                        # 未命中：不弹菜单
    assert not miss.isAccepted()
