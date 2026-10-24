# -*- coding: utf-8 -*-
"""常规域的两层收敛：光标穿透回主组、直播捕获单独折叠且只留一句摘要。

原先把「光标隐藏时自动穿透」和「直播捕获兼容」一起塞进一个「Windows 兼容（高级）」——
两者的性质其实不同：前者是普通窗口旗标偏好（跟置顶 / 全屏自动隐藏同族，说明只有两行），
后者才是"要不要现在读"的那种长说明（四段：重启级切换、OBS 捕获方式、平台限制、作用域）。
于是现在：

1. **光标穿透回「窗口与系统」主组**（不再被折叠埋着）；
2. **直播捕获单独进一层高级折叠**，组描述只留**一句摘要**（"这一项做什么"），
   重启 / 平台限制 / 作用域全部留在行内（展开即见全文，分层不等于删说明）。
3. **作用域是行内独立标注**，不再粘在灰说明末尾；但可访问说明与搜索索引仍然拿得到
   这句（同一句话只是换了呈现位置，见 ``SettingRow.set_scope_note``）。

非 Windows 上「直播捕获兼容」与「光标隐藏时自动穿透」这两行都不构造（能力是 Windows
专属），用例一律按**实际构造结果**驱动：缺行就跳过，行在却摆错位置照样红。平台闸门本身
另有 ``test_desktop_pet_features`` 与 ``test_menu_layout`` 盯着。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea  # noqa: E402

import pet.modern_settings_dialog as settings_mod  # noqa: E402
from pet import settings_page_guide  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.modern_settings_dialog import SettingRow, SettingsSection  # noqa: E402
from pet.settings_pet_controls import GLOBAL_SCOPE_NOTE  # noqa: E402

app = QApplication.instance() or QApplication([])

CAPTURE_TITLE = "直播捕获（高级）"
WINDOW_GROUP = "窗口与系统"


def _dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    return settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=True)


def _row(dialog, key: str) -> SettingRow:
    row = dialog.findChild(SettingRow, f"settingRow_{key}")
    assert row is not None, f"缺少设置行 {key}"
    return row


def _section_of(row: SettingRow) -> SettingsSection:
    parent = row.parentWidget()
    while parent is not None and not isinstance(parent, SettingsSection):
        parent = parent.parentWidget()
    assert parent is not None, "行必须落在某个 SettingsSection 内"
    return parent


def _section_by_title(dialog, title: str) -> SettingsSection | None:
    for section in dialog.findChildren(SettingsSection):
        label = section.findChild(QLabel, "sectionTitle")
        name = section.toggle.text() if section.toggle is not None else (
            label.text() if label is not None else "")
        if name == title:
            return section
    return None


def _current_page(dialog):
    return dialog.pages.currentWidget()


def _search(dialog, query: str):
    dialog.search_edit.setText("")
    app.processEvents()
    dialog.search_edit.setText(query)
    app.processEvents()
    return dialog._search_matches[0] if dialog._search_matches else None


def _capture_section(dialog) -> SettingsSection:
    """直播捕获那个高级组。

    **不能静默跳过**：本平台没有这一行（非 Windows）才是合法缺席；一旦行还在、组没了
    （改名/挪回主组/又跟别的开关合组），这里必须红——否则分层结构被改坏时整组用例
    集体"跳过"，等于没人盯着。
    """
    section = _section_by_title(dialog, CAPTURE_TITLE)
    if section is not None:
        return section
    if _optional_row(dialog, "stream_capture") is None:
        pytest.skip("本平台不构造 Windows 专属的直播捕获行（平台闸门另有专用用例）")
    pytest.fail(f"有直播捕获行，却没有「{CAPTURE_TITLE}」这一组：分层结构被改坏了")


def _optional_row(dialog, key: str) -> SettingRow | None:
    return dialog.findChild(SettingRow, f"settingRow_{key}")


def test_cursor_passthrough_is_back_in_the_window_group(tmp_path, monkeypatch):
    """「光标隐藏时自动穿透」回到「窗口与系统」主组，不再被折叠埋着。

    **这一行是 Windows 专属**（``settings_pet_controls`` 里 ``sys.platform == "win32"``
    才建控件与行），非 Windows 上按实际构造结果跳过——但**不是静默跳过**：Windows 上少了
    这一行就红（平台闸门只在非 Windows 关），且行在、组却不对同样红。
    """
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        row = _optional_row(dialog, "cursor_hidden_passthrough")
        if row is None:
            assert sys.platform != "win32", (
                "Windows 上必须有「光标隐藏时自动穿透」这一行：平台闸门只在非 Windows 关")
            pytest.skip("本平台不构造 Windows 专属的光标隐藏穿透行（平台闸门另有专用用例）")
        section = _section_of(row)
        assert [r.objectName() for r in section.rows] == [
            "settingRow_on_top",
            "settingRow_auto_hide_fullscreen",
            "settingRow_cursor_hidden_passthrough",
            "settingRow_cross_screen",
        ]
        assert section.advanced is False, "窗口旗标类偏好不该进高级折叠"
        assert not section.card.isHidden(), "主组默认是展开的"
    finally:
        dialog.close()


def test_capture_row_keeps_its_full_copy_inside_the_collapsed_group(tmp_path, monkeypatch):
    """直播捕获单独成组：默认收起、行文案一字未改（重启 / 平台限制都还在行内）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        section = _capture_section(dialog)
        assert section.advanced is True, "长说明必须进一层高级折叠"
        assert section.card.isHidden() is True, "默认收起：首屏不再摆四段说明"
        assert [r.objectName() for r in section.rows] == ["settingRow_stream_capture"]

        stream = _row(dialog, "stream_capture")
        hint = stream.hint_label.text()
        assert "重启后生效" in hint, "重启级说明不许从行内消失"
        assert "Windows 10（1903 及以上）" in hint and "BitBlt" in hint, "平台限制不许缩水"
        assert "OBS" in hint, "能力说明不许缩水（行内文案原样保留）"
        assert "多只桌宠会先收拢为主宠物" in hint, "多宠收拢的后果不许缩水"
    finally:
        dialog.close()


def test_collapsed_summary_is_one_sentence_and_keeps_the_details_in_the_row(
        tmp_path, monkeypatch):
    """收起时那句摘要只回答"这一项做什么"：重启 / 平台限制 / 作用域留在行内。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        section = _capture_section(dialog)
        assert section.description_label.isVisibleTo(section), (
            "收起时组描述必须可见，否则「这里是什么」要先展开才知道")
        summary = section.description_label.text().strip()
        assert summary, "组描述不许空"
        assert summary.count("。") <= 1, f"摘要只留一句，实得：{summary}"
        for detail in ("重启", "BitBlt", "Windows 10", "1903", "收拢"):
            assert detail not in summary, f"「{detail}」这类实施细则是行内的事，摘要里不写"
        assert "影响所有桌宠" not in summary, "作用域是行内标注，不在摘要里重复"

        hint = _row(dialog, "stream_capture").hint_label.text()
        assert "重启后生效" in hint and "BitBlt" in hint, "细则必须在行内（展开即见）"
    finally:
        dialog.close()


def test_scope_is_a_separate_annotation_but_still_accessible_and_searchable(
        tmp_path, monkeypatch):
    """作用域从灰说明末尾拆成独立标注：可访问说明与搜索索引都还拿得到这句。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        _capture_section(dialog)
        stream = _row(dialog, "stream_capture")
        assert stream.scope_note.text() == GLOBAL_SCOPE_NOTE
        assert stream.scope_note.isHidden() is False, "标注要真的呈现"
        assert GLOBAL_SCOPE_NOTE not in stream.hint_label.text(), "不许还粘在说明末尾"
        assert stream.control.accessibleDescription() == (
            stream.hint_label.text() + GLOBAL_SCOPE_NOTE), "读屏仍要听到完整说明"

        # 搜索索引读的是"说明 + 标注"：拆位置不等于从搜索里掉出去。
        hits = [row.objectName() for row in dialog._search_matches] if _search(
            dialog, "所有桌宠") is not None else []
        assert "settingRow_stream_capture" in hits, (
            f"搜「所有桌宠」应当仍能命中这一行（标注已拆位置，实得 {hits}）")
    finally:
        dialog.close()


def test_search_finds_the_capture_row_and_expands_the_group(tmp_path, monkeypatch):
    """搜行文案里的关键词：命中该行 → 自动展开组 → 切到常规域且行实际可见。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        _capture_section(dialog)  # 没有这一行就跳过
        page = _current_page(dialog)
        row = _search(dialog, "BitBlt")
        assert row is not None and row.objectName() == "settingRow_stream_capture"
        section = _capture_section(dialog)
        assert section.toggle.isChecked() is True, "命中折叠组内的行必须自动展开"
        assert row.isVisibleTo(page), "命中的行必须真的看得见（不是折在组里）"
        assert dialog.sidebar.currentItem().text() == "常规"

        # 键盘路径同样成立：Tab/回车遍历到的每一个命中都不能折在组里。
        dialog.search_edit.setText("")
        app.processEvents()
        dialog.search_edit.setText("所有桌宠")
        app.processEvents()
        assert dialog._search_matches
        for _step in range(len(dialog._search_matches)):
            QTest.keyClick(dialog.search_edit, Qt.Key.Key_Return)
            app.processEvents()
            current = dialog._search_matches[dialog._search_index]
            assert current.isVisibleTo(_current_page(dialog)), (
                f"回车遍历到的 {current.objectName()} 必须可见（组要跟着展开）")
    finally:
        dialog.close()


def test_search_on_the_group_title_reaches_the_capture_row(tmp_path, monkeypatch):
    """搜组标题（「直播捕获」）：该行进候选（高级组标题进索引）且实际可达。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        _capture_section(dialog)
        hit = _search(dialog, "直播捕获")
        assert hit is not None and hit.objectName() == "settingRow_stream_capture"
        assert _row(dialog, "stream_capture").isVisibleTo(_current_page(dialog))
    finally:
        dialog.close()


def test_section_guide_documents_the_capture_group(tmp_path, monkeypatch):
    """组描述表里有这一组、且没有留下旧组名的残迹（覆盖率守卫的同一口径）。"""
    assert CAPTURE_TITLE in settings_page_guide.SECTION_GUIDE
    assert "Windows 兼容（高级）" not in settings_page_guide.SECTION_GUIDE
    if sys.platform != "win32":  # pragma: no cover - 只在非 Windows 上走到
        dialog = settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=True)
        try:
            assert _section_by_title(dialog, CAPTURE_TITLE) is None
        finally:
            dialog.close()


def test_scroll_area_has_no_horizontal_scrolling_at_the_compact_width(tmp_path, monkeypatch):
    """分层后的常规域在 720px 窄窗下仍不需要横向滚动（长说明换行而不是撑宽）。

    长文案行收在折叠里、未展开时没有有效几何，所以先把组展开再量——量的是"展开后
    这段长说明在窄窗里是换行而不是把内容撑宽"。
    """
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        dialog.resize(720, 500)
        dialog.show()
        app.processEvents()
        page = dialog.pages.widget(next(
            i for i in range(dialog.sidebar.count())
            if dialog.sidebar.item(i).text() == "常规"))
        scroll = page.findChild(QScrollArea, "settingsScroll")
        section = _capture_section(dialog)
        section.toggle.setChecked(True)
        app.processEvents()
        row = _row(dialog, "stream_capture")
        assert row.isVisibleTo(page), "前提：展开后长文案行可见"
        assert row.width() <= scroll.viewport().width(), "行比视口宽（会横向滚动）"
        assert scroll.horizontalScrollBar().maximum() == 0
    finally:
        dialog.close()
