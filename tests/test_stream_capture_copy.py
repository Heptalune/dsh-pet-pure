# -*- coding: utf-8 -*-
"""捕获模式的设置页文案与启动期拓扑分流回归（T4 Phase A）。

T4 原意（``.scratch/single-overlay-window/PHASE4_DESIGN.md:80-82``）：overlay
拓扑下勾选捕获模式 = **重启级拓扑切换**，重启后走 legacy 单窗 PetWindow 路径
（OBS 只能枚举真窗口），重启时多宠收拢为主宠物。此前该设想从未落地——壳上没有
``set_stream_capture_mode`` 运行期入口，设置页只能如实说明"暂不支持"并置灰。

Phase A 把它接通：拓扑判定增加"主配置 ``stream_capture_mode=true``"这一输入
（``overlay_settings_command.is_overlay_topology``，进程内快照一次），设置页去掉
置灰、文案改回 T4 口径；启动期收拢由拓扑分流本身保证（capture 模式不构造
OverlayShell ⇒ 不跑 ``_restore_spawned_pets``）。本文件锁定文案口径，启动期分流
见 ``tests/test_background_service_gates.py``。

「本阶段不治黑屏」：BitBlt/游戏捕获读不到分层窗口内容属已知限制（README 已如实
写明，Phase B 跟踪），与本文件的窗口枚举口径无关。

纪律：offscreen；同步直调，不 sleep。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from tests.test_overlay_dead_switches import _make_shell

app = QApplication.instance() or QApplication([])

LEGACY_HINT = "让 OBS 等工具能够枚举并捕获桌宠窗口；OBS 捕获方式请选 Windows 10（1903 及以上），BitBlt 会是黑屏。"
#: 重启级说明（行内那句）；作用域声明已从说明末尾拆成行内独立标注
#: （``SettingRow.set_scope_note``：GLOBAL_SCOPE_ROWS 里登记着这一行与这句话）。
RESTART_HINT = LEGACY_HINT + "重启后生效；重启时多只桌宠会先收拢为主宠物。"
#: 行内独立标注里的作用域声明。
SCOPE_ANNOTATION = "（影响所有桌宠）"


def _build_dialog(tmp_path, monkeypatch, *, topology: str | None = None,
                  instance_id: str = "", capture: bool | None = None):
    """构建设置页（Windows 场景），拓扑门的配置输入钉到本用例临时目录。

    ``topology=None`` = 不设 env，由主配置 ``stream_capture_mode`` 决定（生产
    路径）；``capture`` 非 None 时先写进**主配置**。``instance_id`` 非空 = 子宠页
    （它自己的 config-slot-N.json 才是读写对象，拓扑门仍只认主配置）。
    """
    import pet.modern_settings_dialog as settings_mod
    from pet import overlay_settings_command as cmd_mod
    from pet.config import APP_DIR_NAME, Config

    monkeypatch.setattr(settings_mod.sys, "platform", "win32")
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    if topology is None:
        monkeypatch.delenv("PET_RENDER_TOPOLOGY", raising=False)
    else:
        monkeypatch.setenv("PET_RENDER_TOPOLOGY", topology)
    config = Config(base=tmp_path, instance_id=instance_id) if instance_id else Config(tmp_path)
    if capture is not None:
        # ``stream_capture_mode`` 是**全局**键（规范 §五「捕获模式与 OS 开机自启」）：
        # 拓扑门只读主配置，子宠页的 slot 文件里没有它、也不许有它（裸 slot
        # ``Config.save`` 会被归属表拒绝）。主宠页的 ``config`` 本身就是主配置。
        main_config = config if not instance_id else Config(base=tmp_path)
        main_config.set("stream_capture_mode", capture)
        assert main_config.save()
    main_path = tmp_path / APP_DIR_NAME / "config.json"
    monkeypatch.setattr(cmd_mod, "topology_config_path", lambda: main_path)
    cmd_mod.reset_topology_snapshot()
    return settings_mod.ModernSettingsDialog(config, include_ai=False)


def _close(dialog):
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def _stream_capture_hint(tmp_path, monkeypatch, topology: str | None) -> str:
    """按拓扑构建设置页，取「直播捕获兼容」行的说明文本。"""
    import pet.modern_settings_dialog as settings_mod
    from pet import overlay_settings_command as cmd_mod

    dialog = _build_dialog(tmp_path, monkeypatch, topology=topology)
    try:
        row = dialog.findChild(settings_mod.SettingRow,
                               "settingRow_stream_capture")
        assert row is not None, "Windows 设置页必须有直播捕获兼容行"
        return row.hint_label.text()
    finally:
        _close(dialog)
        cmd_mod.reset_topology_snapshot()


def test_stream_capture_copy_is_restart_level_under_overlay_topology(tmp_path, monkeypatch):
    """overlay（默认路径）：如实说明这是重启级切换（T4 口径），作用域在行内标注里。"""
    import pet.modern_settings_dialog as settings_mod
    from pet import overlay_settings_command as cmd_mod

    dialog = _build_dialog(tmp_path, monkeypatch, topology="overlay")
    try:
        row = dialog.findChild(settings_mod.SettingRow, "settingRow_stream_capture")
        assert row is not None, "Windows 设置页必须有直播捕获兼容行"
        hint = row.hint_label.text()
        annotation = row.scope_note.text()
        visible = row.scope_note.isHidden() is False
    finally:
        _close(dialog)
        cmd_mod.reset_topology_snapshot()
    assert hint == RESTART_HINT
    assert "重启后生效" in hint
    assert "收拢为主宠物" in hint
    # 作用域声明仍在行内可见——只是换了呈现位置（说明末尾 → 独立标注）。
    assert annotation == SCOPE_ANNOTATION and visible
    assert SCOPE_ANNOTATION not in hint


def test_stream_capture_copy_same_restart_copy_when_already_checked(tmp_path, monkeypatch):
    """已勾选（配置 true，尚未重启的那一进程）：不得退回"看起来已经生效"的旧文案。"""
    import pet.modern_settings_dialog as settings_mod
    from pet import overlay_settings_command as cmd_mod

    dialog = _build_dialog(tmp_path, monkeypatch, capture=True)
    try:
        row = dialog.findChild(settings_mod.SettingRow,
                               "settingRow_stream_capture")
        # 设置页第三刀（2026-10-08）：被改过的行会在 hint 末尾追加一行默认值小字
        # （「默认：关」），本用例只关心重启级文案那一段，逐字断言照旧。
        hint = row.hint_label.text().split("\n默认：", 1)[0]
        checked = dialog.stream_capture_check.isChecked()
    finally:
        _close(dialog)
        cmd_mod.reset_topology_snapshot()
    assert hint == RESTART_HINT
    assert checked is True


def test_stream_capture_check_is_never_greyed_out(tmp_path, monkeypatch):
    """T4 Phase A：**主宠页**两个拓扑下都可点（不再置灰）。"""
    from pet import overlay_settings_command as cmd_mod

    for capture in (False, True):
        dialog = _build_dialog(tmp_path, monkeypatch, capture=capture)
        try:
            assert dialog.stream_capture_check.isEnabled() is True, capture
            assert dialog.stream_capture_check.isChecked() is capture
        finally:
            _close(dialog)
            cmd_mod.reset_topology_snapshot()


def test_stream_capture_check_editable_on_child_page(tmp_path, monkeypatch):
    """F03：子宠页同样可编辑这个**全局**键，点击生效并落**主** config.json。

    旧实现按 ``config.instance_id`` 置灰 + tooltip「仅主桌宠可设置」，理由写的是
    "子宠页保存只写 config-slot-N.json、拓扑判定只读主配置"——那条理由已被
    ``SlotRoutedConfig`` 的全局路由推翻：非每宠键一律落主配置。而正常 UI 入口
    **只有** slot 身份（``--instance slot-0``/``slot-N``），按"身份非空"判会把
    slot0 自己的页面也拒掉，连已经在捕获模式里的用户都关不掉它（2026-10-10 终审 F03）。
    规范 §五：任意宠可以编辑全局设置，界面说清影响所有桌宠即可。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from pet import overlay_settings_command as cmd_mod
    from pet.config import APP_DIR_NAME

    dialog = _build_dialog(tmp_path, monkeypatch, topology="overlay",
                           instance_id="slot-1", capture=False)
    try:
        check = dialog.stream_capture_check
        assert check is not None, "Windows 设置页必须有直播捕获兼容行"
        assert check.isEnabled() is True, "子宠页的全局键必须可编辑（不再是死权限）"
        assert check.toolTip() != "仅主桌宠可设置"

        dialog.show()
        app.processEvents()
        QTest.mouseClick(check, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert check.isChecked() is True, "可点就必须真的点得动"

        # 真实路由：值进**主** config.json，slot 文件里不出现这个键（不留第三真值）
        assert dialog._write_config() is True
        assert dialog.config.get("stream_capture_mode") is True
        main_disk = json.loads(
            (tmp_path / APP_DIR_NAME / "config.json").read_text(encoding="utf-8"))
        assert main_disk["stream_capture_mode"] is True, "拓扑门读的就是这一份"
        slot_disk = json.loads(
            (tmp_path / APP_DIR_NAME / "config-slot-1.json").read_text(encoding="utf-8"))
        assert "stream_capture_mode" not in slot_disk
    finally:
        _close(dialog)
        cmd_mod.reset_topology_snapshot()


def test_stream_capture_child_row_is_editable_in_legacy(tmp_path, monkeypatch):
    """legacy 下同一条路由成立：全局键写主配置，不因拓扑退回"只归主宠页"。"""
    dialog = _build_dialog(tmp_path, monkeypatch, topology="legacy",
                           instance_id="slot-1")
    try:
        assert dialog.stream_capture_check.isEnabled() is True
    finally:
        _close(dialog)


def test_stream_capture_copy_unchanged_under_explicit_legacy_env(tmp_path, monkeypatch):
    """显式 PET_RENDER_TOPOLOGY=legacy（dev 逃生门）：运行期即时切换，提示不重启。"""
    hint = _stream_capture_hint(tmp_path, monkeypatch, "legacy")
    assert hint == LEGACY_HINT
    assert "重启" not in hint


def test_capture_config_flips_topology_verdict(tmp_path, monkeypatch):
    """配置文件就是拓扑输入：true → 非 overlay（legacy 单窗）。"""
    from pet import overlay_settings_command as cmd_mod
    from pet.config import Config

    monkeypatch.delenv("PET_RENDER_TOPOLOGY", raising=False)
    config = Config(tmp_path)
    monkeypatch.setattr(cmd_mod, "topology_config_path", lambda: config.path)
    cmd_mod.reset_topology_snapshot()
    assert cmd_mod.is_overlay_topology() is True
    config.set("stream_capture_mode", True)
    assert config.save()
    cmd_mod.reset_topology_snapshot()
    try:
        assert cmd_mod.is_overlay_topology() is False
        assert json.loads(config.path.read_text(encoding="utf-8"))[
            "stream_capture_mode"] is True
    finally:
        cmd_mod.reset_topology_snapshot()


def test_overlay_topology_has_no_runtime_capture_switch(tmp_path, monkeypatch):
    """overlay 拓扑下不得触发 legacy 运行期切换路径（window.py:3826-3828）。

    该键在 overlay 侧只落 config，重启后由拓扑分流消费——壳上没有
    ``set_stream_capture_mode`` 这类运行期入口，refresh_settings 也不读它。
    """
    monkeypatch.setenv("PET_RENDER_TOPOLOGY", "overlay")
    shell, _lib = _make_shell(tmp_path, {"stream_capture_mode": False})
    try:
        assert not hasattr(shell, "set_stream_capture_mode")
        assert not hasattr(shell, "_stream_capture_mode")
        flags_before = shell.overlay.windowFlags()
        shell._config.set("stream_capture_mode", True)
        shell.refresh_settings()          # 不得抛错、不得改窗旗标
        assert shell.overlay.windowFlags() == flags_before
        assert shell._config.get("stream_capture_mode") is True
    finally:
        shell._delete_runtime_marker()
