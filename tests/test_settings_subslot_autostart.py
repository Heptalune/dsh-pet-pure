# -*- coding: utf-8 -*-
"""「开机自启」在任意宠的设置页都可编辑，且界面明示影响范围（终审 A04-b）。

开机自启是**全局**键（``autostart_wanted`` 写在主配置、系统登录项只有一份），规范 §五
要求"任意宠可以编辑全局设置，但界面必须明确说明影响所有桌宠"。旧判据
``if config.instance_id`` 在 slot0 拿到显式身份（``slot-0``）之后把**主宠自己的页面**
也一起禁掉了，tooltip 还写着"仅主桌宠可设置"——用户正站在主桌宠设置里，按提示找不到
任何可设之处。
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from pet.config import Config
from pet.modern_settings_dialog import ModernSettingsDialog
from pet.settings_slot_config import route_settings_config


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("instance_id", ["", "slot-0", "slot-1"])
def test_modern_settings_dialog_keeps_autostart_editable_for_every_pet(
        app, tmp_path, instance_id):
    cfg = route_settings_config(Config(tmp_path, instance_id=instance_id))
    dialog = ModernSettingsDialog(cfg, include_ai=False)
    try:
        assert dialog.autostart_check.isEnabled() is True, instance_id
        assert dialog.autostart_check.toolTip() != "仅主桌宠可设置"
        assert "影响所有桌宠" in dialog.autostart_check.toolTip()
    finally:
        dialog.close()


def test_autostart_row_hint_states_the_global_scope_on_a_pet_page(app, tmp_path):
    """行说明也要说清作用域：作用域是行内独立标注，且不再写"仅主桌宠可设置"。"""
    from pet.modern_settings_dialog import SettingRow

    cfg = route_settings_config(Config(tmp_path, instance_id="slot-1"))
    dialog = ModernSettingsDialog(cfg, include_ai=False)
    try:
        row = dialog.findChild(SettingRow, "settingRow_autostart")
        assert row is not None
        assert "影响所有桌宠" in row.scope_note.text()
        assert row.scope_note.isHidden() is False, "标注要真的呈现"
        assert "仅主桌宠可设置" not in row.hint_label.text()
    finally:
        dialog.close()


def test_child_page_autostart_save_lands_in_the_global_config(app, tmp_path, monkeypatch):
    """子宠页勾选后，值落在**主配置**（不是 slot 文件）——全局键只有一个真值。"""
    from pet import autostart as autostart_mod

    monkeypatch.setattr(autostart_mod, "is_enabled", lambda: False)
    monkeypatch.setattr(autostart_mod, "set_enabled", lambda _on: True)
    base = tmp_path / "appdata"
    main = Config(base=base)
    assert main.save() is True
    cfg = route_settings_config(Config(base=base, instance_id="slot-1"))
    dialog = ModernSettingsDialog(cfg, include_ai=False)
    try:
        dialog.autostart_check.setChecked(True)
        assert dialog._write_config() is True
    finally:
        dialog.close()

    assert Config(base=base).get("autostart_wanted") is True
    slot_path = base / "config-slot-1.json"
    assert not slot_path.exists() or "autostart_wanted" not in slot_path.read_text(
        encoding="utf-8"), "全局键不许在每宠档案里出现第二份真值"
