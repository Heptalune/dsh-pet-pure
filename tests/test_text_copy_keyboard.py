# -*- coding: utf-8 -*-
"""只读文本要能用键盘复制（Ctrl+C），不只是鼠标选中 + 右键菜单。

2026-10-19 实机反馈：截图翻译结果里用鼠标选出一段文字，按 Ctrl+C 没反应，只能右键
菜单「复制」。两处成因：

1. **窗口层**：结果面板带 ``WindowDoesNotAcceptFocus``（``WS_EX_NOACTIVATE``），
   连"用户点击后激活"也一并禁掉 → 键盘事件永远进不来。现在只保留
   ``WA_ShowWithoutActivating``（打开时不激活），点击可以让面板获得焦点。
2. **控件层**：一批 QLabel 只设了 ``TextSelectableByMouse``，没有
   ``TextSelectableByKeyboard``，也没有可聚焦策略 → 选中了也没有 Ctrl+C 的落点。
   现在统一补上，并给 ``ClickFocus``（点击才聚焦，不抢 Tab 焦点流）。

这里钉两件事：控件层真按 Ctrl+C 能进剪贴板；那些 QLabel 的 flags/焦点策略没退回去。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

app = QApplication.instance() or QApplication([])


def test_result_panel_text_view_copies_with_ctrl_c():
    """结果面板的只读文本视图：选中后 Ctrl+C 必须真的进剪贴板。"""
    from pet.screen_capture.result_panel import ScreenResultPanel

    panel = ScreenResultPanel()
    try:
        view = panel.findChild(QPlainTextEdit, "screen-result-text")
        assert view is not None, "结果面板缺只读文本视图"
        view.setPlainText("No packages published")
        QApplication.clipboard().clear()
        view.selectAll()
        QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert QApplication.clipboard().text() == "No packages published"
    finally:
        panel.close()
        panel.deleteLater()


def test_result_panel_text_view_is_click_focusable():
    """视图要能因点击获得焦点，否则 Ctrl+C 没有落点（不等于"打开就抢焦点"）。"""
    from pet.screen_capture.result_panel import ScreenResultPanel

    panel = ScreenResultPanel()
    try:
        assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating) is True, (
            "打开时不激活这条不能丢")
        assert Qt.WindowType.WindowDoesNotAcceptFocus not in panel.windowFlags(), (
            "带这个标志时点击也激活不了，Ctrl+C 永远没落点")
        view = panel.findChild(QPlainTextEdit, "screen-result-text")
        assert view.focusPolicy() == Qt.FocusPolicy.ClickFocus
    finally:
        panel.close()
        panel.deleteLater()


def test_ask_dialog_bubble_is_keyboard_selectable():
    """截图问答气泡：除了鼠标选中，还要能用键盘选中并复制。"""
    from pet.screen_capture.ask_dialog import _Bubble

    colors = {"button": "#1677e8", "text": "#111111"}
    bubble = _Bubble("包\n尚未发布任何包", "assistant", colors)
    try:
        flags = bubble.textInteractionFlags()
        assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
        assert flags & Qt.TextInteractionFlag.TextSelectableByKeyboard, (
            "缺 TextSelectableByKeyboard 时 Ctrl+C 没有落点")
        assert bubble.focusPolicy() == Qt.FocusPolicy.ClickFocus
    finally:
        bubble.deleteLater()
