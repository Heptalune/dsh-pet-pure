# -*- coding: utf-8 -*-
"""设置行的「作用域标注」：从灰说明末尾拆出来，但可访问说明与搜索一个都不许丢。

背景：全局/窗口级作用的行原本把这句拼在说明末尾
（「登录系统后自动启动桌宠。（影响所有桌宠）」）——读起来像在讲"这一项做什么"，而它讲的是
"改了谁受影响"。现在它是**标签那一行右边**的独立标注（``SettingRow.scope_note``，复用
settingHint 的小字，不新增样式面）：既不与说明黏成一句，也不独占一行。

本文件钉五件事（都走真实控件，不 mock 产品对象）：

1. **拆得干净**：说明末尾那句被削掉，标注里逐字保留；说明里本来没有这句的行则一字不动。
2. **可访问性不缩水**：``accessibleDescription`` 仍是"说明 + 标注"全文，``accessibleName``
   不动——拆位置不是删说明。
3. **搜索不缩水**：索引仍读得到标注（``settings_search._row_text``），搜「所有桌宠」照旧命中。
4. **窄窗不裁切**：标注是 12px 小字、不参与"撑宽"，720px 最小窗口下不越界也不被纵向裁切。
5. **不占额外一行**：带标注的行与同款无标注行**等高**——它要是独占一行，常规域 800 宽下就从
   "装得下"变"要滚"，页头会重新长出跳转条。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

import pet.modern_settings_dialog as settings_mod  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.modern_settings_dialog import SettingRow  # noqa: E402
from pet.settings_pet_controls import (  # noqa: E402
    ALL_PETS_SCOPE_NOTE,
    GLOBAL_SCOPE_NOTE,
    _apply_window_scope_rows,
)
from pet.settings_widgets import ToggleSwitch  # noqa: E402

app = QApplication.instance() or QApplication([])

#: 声明里逐字写着的行（说明末尾那句 = 标注要显示的那句）。
GLOBAL_SCOPE_ROW_IDS = ("autostart", "harness_autostart", "harness_launch_target")


def _dialog(tmp_path, monkeypatch, *, instance_id: str = ""):
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    if instance_id:
        return settings_mod.ModernSettingsDialog(
            Config(base=tmp_path, instance_id=instance_id), include_ai=False)
    return settings_mod.ModernSettingsDialog(Config(base=tmp_path), include_ai=False)


def _row(dialog, key: str) -> SettingRow:
    row = dialog.findChild(SettingRow, f"settingRow_{key}")
    assert row is not None, f"缺少设置行 {key}"
    return row


def _plain_row(hint: str, *, title: str = "探针行", parent=None) -> SettingRow:
    """一个最小真实行（ToggleSwitch 控件）：测机制本身，不依赖某个域页的装配。"""
    control = ToggleSwitch(parent)
    row = SettingRow("unit_scope_probe", title, hint, control, parent)
    return row


# --------------------------------------------------------------- 1. 拆得干净
def test_trailing_declaration_moves_from_the_hint_into_the_annotation():
    """说明末尾那句挪进标注：说明不再带它，标注逐字是它，控件说明仍是全文。"""
    row = _plain_row("登录系统后自动启动桌宠。（影响所有桌宠）")
    try:
        row.set_scope_note("（影响所有桌宠）")
        assert row.hint_label.text() == "登录系统后自动启动桌宠。"
        assert row.scope_note.text() == "（影响所有桌宠）"
        assert row.scope_note.isHidden() is False, "标注要真的呈现"
        assert row.control.accessibleDescription() == "登录系统后自动启动桌宠。（影响所有桌宠）"
    finally:
        row.deleteLater()


def test_row_without_the_declaration_keeps_its_hint_untouched():
    """说明里本来没有这句的行：一字不动，只把标注挂上去（追加进可访问说明）。"""
    row = _plain_row("始终将桌宠保持在其他窗口上方。")
    try:
        row.set_scope_note(ALL_PETS_SCOPE_NOTE)
        assert row.hint_label.text() == "始终将桌宠保持在其他窗口上方。"
        assert row.scope_note.text() == ALL_PETS_SCOPE_NOTE
        assert row.control.accessibleDescription() == (
            "始终将桌宠保持在其他窗口上方。" + ALL_PETS_SCOPE_NOTE)
    finally:
        row.deleteLater()


def test_calling_twice_does_not_duplicate_or_eat_copy():
    """幂等：挂两次不会把标注追加成两遍，也不会把说明再削一刀。"""
    row = _plain_row("登录系统后自动启动桌宠。（影响所有桌宠）")
    try:
        row.set_scope_note("（影响所有桌宠）")
        row.set_scope_note("（影响所有桌宠）")
        assert row.scope_note.text() == "（影响所有桌宠）"
        assert row.hint_label.text() == "登录系统后自动启动桌宠。"
        assert row.control.accessibleDescription().count("（影响所有桌宠）") == 1
    finally:
        row.deleteLater()


def test_empty_note_takes_the_annotation_away():
    """传空串 = 摘掉标注（legacy 拓扑的窗口级键就不该带它）。"""
    row = _plain_row("始终将桌宠保持在其他窗口上方。")
    try:
        row.set_scope_note(ALL_PETS_SCOPE_NOTE)
        row.set_scope_note("")
        assert row.scope_note.text() == ""
        assert row.scope_note.isHidden() is True
        assert row.hint_label.text() == "始终将桌宠保持在其他窗口上方。"
    finally:
        row.deleteLater()


# --------------------------------------------------- 2. 真实行的作用域声明
@pytest.mark.parametrize("key", GLOBAL_SCOPE_ROW_IDS)
def test_global_rows_carry_the_annotation_and_not_the_hint_tail(
        tmp_path, monkeypatch, key):
    """全局键：标注在行内可见，说明末尾那句已经挪走（可访问说明仍是全文）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        row = _row(dialog, key)
        note = row.scope_note.text()
        assert note == ("（影响所有桌宠）" if key == "autostart"
                        else "此设置影响所有桌宠。")
        assert row.scope_note.isHidden() is False
        assert note not in row.hint_label.text()
        assert row.control.accessibleDescription() == row.hint_label.text() + note
        assert row.control.accessibleName(), "可访问名字不许被拆动作弄丢"
    finally:
        dialog.close()


def test_window_level_injection_is_not_lost_on_a_pet_page(tmp_path, monkeypatch):
    """窗口级原注入（overlay 一窗多宠）不许漏：七行都挂上标注。"""
    from pet.settings_pet_controls import OVERLAY_WINDOW_SCOPE_IDS

    monkeypatch.setenv("PET_RENDER_TOPOLOGY", "overlay")
    dialog = _dialog(tmp_path, monkeypatch, instance_id="slot-1")
    try:
        for key in OVERLAY_WINDOW_SCOPE_IDS:
            row = dialog.findChild(SettingRow, f"settingRow_{key}")
            if row is None:  # 平台闸门：本平台不建这一行
                continue
            assert row.scope_note.text() == ALL_PETS_SCOPE_NOTE, key
            assert row.scope_note.isHidden() is False, key
            assert ALL_PETS_SCOPE_NOTE not in row.hint_label.text(), key
            assert row.control.accessibleDescription().endswith(ALL_PETS_SCOPE_NOTE), key
    finally:
        dialog.close()


def test_the_scope_pass_leaves_rows_alone_when_it_is_not_applicable(tmp_path, monkeypatch):
    """legacy 拓扑：窗口级键不加（每宠独立窗口，键确实只作用于自己）——重跑也不加。"""
    from pet.settings_pet_controls import OVERLAY_WINDOW_SCOPE_IDS

    monkeypatch.setenv("PET_RENDER_TOPOLOGY", "legacy")
    dialog = _dialog(tmp_path, monkeypatch, instance_id="slot-1")
    try:
        _apply_window_scope_rows(dialog)  # 再跑一遍：必须仍是无声无息
        for key in OVERLAY_WINDOW_SCOPE_IDS:
            row = dialog.findChild(SettingRow, f"settingRow_{key}")
            if row is None:
                continue
            assert row.scope_note.text() == "", key
            assert ALL_PETS_SCOPE_NOTE not in row.control.accessibleDescription(), key
    finally:
        dialog.close()


# --------------------------------------------------------------- 3. 搜索
def test_scope_annotation_stays_searchable(tmp_path, monkeypatch):
    """搜作用域那句话仍能命中这些行（索引读的是"说明 + 标注"）。"""
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        for query, expected in (("影响所有桌宠", "settingRow_autostart"),
                                ("所有桌宠", "settingRow_harness_autostart")):
            dialog.search_edit.setText("")
            app.processEvents()
            dialog.search_edit.setText(query)
            app.processEvents()
            hits = [row.objectName() for row in dialog._search_matches]
            assert expected in hits, f"搜「{query}」应当命中 {expected}（实得 {hits}）"
    finally:
        dialog.close()


# --------------------------------------------------------- 4. 窄窗不裁切
@pytest.mark.parametrize("font_scale", [1.0, 1.5])
def test_annotation_fits_the_compact_window(tmp_path, monkeypatch, font_scale):
    """最小窗口（720×500）下、**默认与放大字体**都不裁切标注。

    放大字体这一档是必须的：标注与说明是同一把小字（``settingHint``），字号放大后
    标注需要的宽度跟着涨——只测默认字号会漏掉"标注被行宽压掉半句"。

    只看**当前可见行**：没被显示过的页内标签页还停在默认几何（宽 100px）上，量它不是
    在量用户看得见的东西（那份几何在页面第一次显示时就会重排）。
    """
    dialog = _dialog(tmp_path, monkeypatch)
    try:
        dialog.resize(720, 500)
        dialog.show()
        app.processEvents()
        if font_scale != 1.0:
            for widget in [dialog, *dialog.findChildren(QWidget)]:
                font = widget.font()
                if font.pixelSize() > 0:
                    font.setPixelSize(max(1, round(font.pixelSize() * font_scale)))
                    widget.setFont(font)
            dialog.resize(721, 500)
            app.processEvents()
            dialog.resize(720, 500)
            app.processEvents()
        checked = 0
        for index in range(dialog.sidebar.count()):
            dialog.sidebar.setCurrentRow(index)
            app.processEvents()
            for row in dialog.findChildren(SettingRow):
                if row.scope_note.isHidden() or not row.isVisible():
                    continue
                label = row.scope_note
                assert label.width() <= row.width(), (
                    f"{row.objectName()} 的标注比行还宽")
                # 标注是**不换行**的短标签（见 test_annotation_keeps_the_title_line_height_for_width_honest）：
                # 所以"有没有被压到裁字"的判据是它拿到的宽度够不够，而不是 hfw。
                assert label.width() >= label.sizeHint().width(), (
                    f"{row.objectName()}（字体 ×{font_scale}）的标注被压到 "
                    f"{label.width()}px（需要 {label.sizeHint().width()}px）：会裁字")
                checked += 1
        assert checked >= 2, f"只检查到 {checked} 个可见标注（本用例会假绿）"
    finally:
        dialog.close()


def test_the_longest_annotation_still_fits_next_to_its_label():
    """最紧的一对（最长标注 + 最长标签）在最小窗口列宽下仍放得下。

    标注不换行 ⇒ 放不下就会被裁字，所以这条按"最坏组合 + 1.5 倍字体"直接量：
    「随桌宠启动 dsh 服务」+「此设置影响所有桌宠。」（``HARNESS_TARGET_HINT_*`` 那一族，
    全局键里最长的两句）。列宽取 720 窗口下的实测值（``tests/test_settings_sidebar_and_chrome``
    的同一口径）。
    """
    row = _plain_row("这是说明。", title="随桌宠启动 dsh 服务")
    try:
        row.setFixedWidth(422)
        for widget in [row, *row.findChildren(QWidget)]:
            font = widget.font()
            if font.pixelSize() > 0:
                font.setPixelSize(round(font.pixelSize() * 1.5))
                widget.setFont(font)
        row.set_scope_note("此设置影响所有桌宠。")
        row.show()
        app.processEvents()
        row.layout().activate()
        app.processEvents()
        assert row.scope_note.width() >= row.scope_note.sizeHint().width(), (
            f"标注拿到 {row.scope_note.width()}px，需要 {row.scope_note.sizeHint().width()}px")
        assert row.label.width() >= row.label.minimumSizeHint().width(), "标签被压到裁字"
    finally:
        row.deleteLater()


# ------------------------------------------------- 5. 不占额外一行（页高不变）
def _row_size(row: SettingRow, width: int) -> int:
    """把行按给定宽度落版后返回高度（真实布局树，不是 sizeHint 估算）。"""
    row.setFixedWidth(width)
    row.show()
    app.processEvents()
    row.layout().activate()
    app.processEvents()
    return row.height()


def test_annotation_does_not_add_a_line_to_the_row():
    """带标注的行不许比同款无标注行高：标注挂在**标签那一行**的右边，不独占一行。

    为什么这是硬判据：独占一行会让每个带标注的行多出一整行。常规域有两行带标注，
    800 宽下页面因此从"装得下"变"要滚"——页头会重新长出跳转条（那条横排 chip 的
    判据就是"内容真的超过视口"，短页不摆）。
    """
    rows = []
    try:
        for index in range(2):
            control = ToggleSwitch()
            row = SettingRow(f"unit_scope_height_{index}", "开机自启", "登录系统后自动启动桌宠。", control)
            rows.append(row)
        baseline = _row_size(rows[0], 454)
        rows[1].set_scope_note(GLOBAL_SCOPE_NOTE)
        assert rows[1].scope_note.isHidden() is False, "前提：标注真的挂上了"
        with_note = _row_size(rows[1], 454)
        assert with_note == baseline, (
            f"带标注的行高 {with_note}px 比同款无标注行 {baseline}px 高：标注占了额外一行")
        # 标注与标签同一行（视觉上也确认一次：标注的纵向位置在灰说明之上）。
        assert rows[1].scope_note.y() < rows[1].hint_label.y(), (
            "标注应当和标签在同一行，不该跑到说明下面")
    finally:
        for row in rows:
            row.deleteLater()


def test_annotation_shares_the_hint_style_token():
    """标注复用 settingHint 的小字（不新增 QSS 面：对象名就是样式令牌）。"""
    row = _plain_row("始终将桌宠保持在其他窗口上方。")
    try:
        row.set_scope_note(ALL_PETS_SCOPE_NOTE)
        assert row.scope_note.objectName() == "settingHint"
        assert isinstance(row.scope_note, QLabel)
    finally:
        row.deleteLater()


def test_annotation_keeps_the_title_line_height_for_width_honest():
    """标签那一行里只能有**一个**可换行的标签——否则 Qt 的 ``heightForWidth`` 会退化成
    按当前几何求和，页高虚高。

    实测（``.scratch/settings-navfix/probe_title_line_hfw.py``）：标签行
    ``[标签(hfw) · 标注(hfw) stretch]`` 在"从未显示过的页内标签页"里量出
    ``heightForWidth(444) = 96``（标签自己只要 13），于是那个隐藏标签页的页高虚高 103px
    ——而页内标签容器取各页的**最大值**，"内容超不超视口"的判据跟着变（跳转条会莫名出现）。
    标注不换行后这一行只剩标签一个 hfw 项，判据回到标签自己的高度。
    """
    row = _plain_row("SHIFT+左键拖动")
    try:
        row.setFixedWidth(532)
        row.set_scope_note(GLOBAL_SCOPE_NOTE)
        app.processEvents()
        copy = row.layout().itemAt(0).layout()
        title_line = copy.itemAt(0).layout()
        assert row.scope_note.isHidden() is False, "前提：标注真的挂上了"
        assert title_line.heightForWidth(532) == row.label.heightForWidth(532), (
            f"标签行量出 {title_line.heightForWidth(532)}px，标签自己只要 "
            f"{row.label.heightForWidth(532)}px：标注参与进了高度计算")
    finally:
        row.deleteLater()
