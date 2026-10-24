# -*- coding: utf-8 -*-
"""overlay 右键菜单最小集（单合成窗架构 Phase 3a）。

架构背景见 .scratch/single-overlay-window/spec.md「Phase 3 设计」：sprite 不
拥有应用级状态，本阶段菜单只做"窗级动作"——戳一下（播点击动画）/ 大小
（sprite.scale）/ 鼠标穿透（overlay.mouse_through）/ 退出（关进程）。
不接 context_menus 的注册表/模板体系（id→spec 那套绑定 PetWindow API，
属 Phase 4 收口范围）；样式用默认 QMenu。

菜单动作全部走 duck-typing：overlay 只需暴露 mouse_through 字段，sprite
只需暴露 scale 属性（帧签名含 scale，改值即自动重建帧，见 pet_sprite.py），
behavior 只需有 on_sprite_clicked(sprite)。offscreen 测试可直接
trigger() 各 action，无需 exec。
"""

from __future__ import annotations

from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QApplication, QMenu, QWidget

# 大小档位（sprite.scale 预设值；默认档 0.72 = catalog.DEFAULT_SCALE）
SCALE_PRESETS = (0.5, 0.72, 1.0, 1.3)


def _scale_label(value: float) -> str:
    return f"{round(value * 100)}%"


def build_sprite_menu(overlay, sprite, *, behavior=None) -> QMenu:
    """构建 sprite 右键菜单（默认 QMenu 样式，不归调用方管生命周期）。

    - 「戳一下」：behavior.on_sprite_clicked(sprite)；behavior 为 None 时禁用；
    - 「大小」子菜单：SCALE_PRESETS 四档单选，勾选当前档，选中即改 sprite.scale；
    - 「鼠标穿透」勾选项 ↔ overlay.mouse_through（勾选 = 全 overlay 恒穿透，
      Windows 穿透轮询读这个字段，见 overlay_window.showEvent）；
    - 「退出」：QApplication.quit()。
    """
    menu = QMenu(overlay if isinstance(overlay, QWidget) else None)

    poke = menu.addAction("戳一下")
    poke.setEnabled(behavior is not None)
    if behavior is not None:
        poke.triggered.connect(lambda: behavior.on_sprite_clicked(sprite))

    size_menu = menu.addMenu("大小")
    group = QActionGroup(size_menu)
    group.setExclusive(True)
    for value in SCALE_PRESETS:
        action = size_menu.addAction(_scale_label(value))
        action.setCheckable(True)
        action.setData(value)
        action.setChecked(abs(float(sprite.scale) - value) < 1e-6)
        action.triggered.connect(lambda _checked=False, v=value: setattr(sprite, "scale", v))
        group.addAction(action)

    menu.addSeparator()

    through = menu.addAction("鼠标穿透")
    through.setCheckable(True)
    through.setChecked(bool(overlay.mouse_through))
    # 有统一直写点就走它（shell 收编为用户+自动穿透复合语义），demo 直接写字段
    through.toggled.connect(
        lambda checked: overlay.set_mouse_through(bool(checked))
        if hasattr(overlay, "set_mouse_through")
        else setattr(overlay, "mouse_through", bool(checked)))

    menu.addSeparator()

    quit_action = menu.addAction("退出")
    # 调用点查表而非连接时绑定：测试可 monkeypatch QApplication.quit 记录调用
    quit_action.triggered.connect(lambda: QApplication.quit())

    return menu
