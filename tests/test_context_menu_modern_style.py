# -*- coding: utf-8 -*-
"""modern 菜单样式的平台窗口标志回归。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMenu

app = QApplication.instance() or QApplication([])


def test_modern_menu_has_frameless_hint_on_windows():
    """Windows 下 modern 菜单必须带 FramelessWindowHint——否则 QPA 不挂
    WS_EX_LAYERED，圆角外的透明像素被当成不透明黑（四角黑边，PR #341 根因）。
    非 Windows 不置位（各平台原生弹窗行为不动）。"""
    from pet.context_menus.menu_styles.modern import apply_modern_menu_style

    menu = QMenu()
    apply_modern_menu_style(menu)
    has_flag = bool(menu.windowFlags() & Qt.WindowType.FramelessWindowHint)
    assert has_flag is (sys.platform == "win32")
    menu.deleteLater()
