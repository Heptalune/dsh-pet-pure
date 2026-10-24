"""Author accounts are a read-only section on the Updates page, in both settings variants."""
from __future__ import annotations

import copy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from pet.config import Config
from pet.context_menus.icons import THEMED_ICON_REQUEST
from pet.modern_settings_dialog import ModernSettingsDialog
from pet.settings_about import build_author_section
from pet.settings_widgets import refresh_settings_icons

app = QApplication.instance() or QApplication([])


def test_author_actions_take_the_settings_surface_theme():
    """按钮登记主题重画请求，由设置窗按自己的主题重画（深色档下必须是浅色墨）。

    这里不钉 ``modernDark`` 也能验证同一件事：入口记请求、``refresh_settings_icons``
    按宿主当时的主题重画——正是设置窗开窗/切主题走的路径。
    """
    section = build_author_section()
    host = QWidget()
    host.setProperty("menuStyle", "modern")
    host.setProperty("modernDark", True)
    QVBoxLayout(host).addWidget(section)
    try:
        app.processEvents()
        button = section.findChild(QPushButton)
        assert button.property(THEMED_ICON_REQUEST), "按钮没有登记主题重画请求"
        refresh_settings_icons(host)
        image = button.icon().pixmap(24, 24).toImage()
        pixels = [image.pixelColor(x, y) for y in range(image.height()) for x in range(image.width())]
        assert any(color.alpha() > 200 and color.lightness() > 180 for color in pixels)
    finally:
        host.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("include_ai", [True, False])
def test_author_section_sits_on_the_updates_page(tmp_path, monkeypatch, include_ai):
    cfg = Config(base=tmp_path)
    dialog = ModernSettingsDialog(cfg, include_ai=include_ai)
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    previous_clipboard = app.clipboard().text()
    try:
        dialog.show()
        app.processEvents()
        labels = [dialog.sidebar.item(i).text() for i in range(dialog.sidebar.count())]
        # 作者账号不是侧栏能力域：它并进「更新」页，侧栏域清单不含「关于」。
        assert labels[-1] == "更新"
        assert "关于" not in labels
        assert dialog.select_page("更新") is True
        app.processEvents()
        page = dialog.pages.currentWidget()
        before = copy.deepcopy(cfg.data)
        for platform, handle in [("BiliBili", "@張墨林"), ("BiliBili", "@快乐小咸鱼a"),
                                 ("抖音", "@Eager"), ("GitHub", "@MerZlin")]:
            assert any(label.text() == handle for label in page.findChildren(QLabel))
            button = next(button for button in page.findChildren(QPushButton)
                          if button.accessibleName() == f"复制 {platform} 账号 {handle}")
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            assert app.clipboard().text() == handle
        link = next(button for button in page.findChildren(QPushButton)
                    if button.accessibleName() == "打开 GitHub 主页")
        QTest.mouseClick(link, Qt.MouseButton.LeftButton)
        assert opened == ["https://github.com/Heptalune"]
        assert cfg.data == before
    finally:
        app.clipboard().setText(previous_clipboard)
        dialog.deleteLater()
        app.processEvents()
