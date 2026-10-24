# -*- coding: utf-8 -*-
"""Read-only author information shown at the bottom of the Updates page."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QToolTip, QVBoxLayout, QWidget,
)

from .context_menus.icons import vector_widget_icon
from .settings_widgets import SettingsSection

AUTHOR_ACCOUNTS = (
    ("BiliBili", "@張墨林"),
    ("BiliBili", "@快乐小咸鱼a"),
    ("抖音", "@Eager"),
    ("GitHub", "@MerZlin"),
)
GITHUB_PROFILE = "https://github.com/Heptalune"


def build_author_section(parent: QWidget | None = None) -> SettingsSection:
    """作者账号分区，挂在「更新」页底部。

    深浅不在本模块钉死：``vector_widget_icon`` 会把渲染请求记在按钮上，设置窗开窗与
    切主题时按**它自己的**主题（``settingsDark``）重画（``refresh_settings_icons``）——
    若在这里钉 ``modernDark``，跟随系统主题就会盖掉设置窗自己选的那一档。
    分区不写入 Config，也不参与重置写回。
    """
    rows = [_account_row(platform, handle) for platform, handle in AUTHOR_ACCOUNTS]
    return SettingsSection("作者账号", rows, parent)


class AuthorRow(QWidget):
    """作者账号行（读只读面，写入 Config 无关）。

    刻意**不是** ``SettingRow``：这一行没有配置键、没有「已修改 / 改回默认」，两行
    文字的主次也与标准行相反。但搜索只读 ``label`` / ``hint_label`` / ``objectName``
    和祖先组标题（见 ``settings_search._row_text``），所以按同一个形状暴露这两个属性
    就够被索引到，不必为"可搜"改掉这里的排版。
    """

    def __init__(self, platform: str, handle: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName(f"authorRow_{platform}")
        self.label = QLabel(platform, self)
        self.hint_label = QLabel(handle, self)


def _account_row(platform: str, handle: str) -> QWidget:
    row = AuthorRow(platform, handle)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(16, 10, 16, 10)
    layout.setSpacing(12)
    text = QVBoxLayout()
    text.setSpacing(3)
    for label, role in ((row.label, "settingHint"), (row.hint_label, "settingLabel")):
        label.setObjectName(role)
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        label.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        text.addWidget(label)
    layout.addLayout(text, 1)
    copy = _button(row, "copy", f"复制 {platform} 账号 {handle}")
    copy.clicked.connect(lambda: _copy_account(handle, copy))
    layout.addWidget(copy)
    if platform == "GitHub":
        link = _button(row, "external_link", "打开 GitHub 主页")
        link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB_PROFILE)))
        layout.addWidget(link)
    return row


def _button(parent: QWidget, icon: str, name: str) -> QPushButton:
    button = QPushButton(parent)
    button.setIcon(vector_widget_icon(button, icon))
    button.setFixedSize(32, 32)
    button.setToolTip(name)
    button.setAccessibleName(name)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _copy_account(handle: str, button: QPushButton) -> None:
    QApplication.clipboard().setText(handle)
    QToolTip.showText(button.mapToGlobal(QPoint(0, button.height())), "已复制", button)
