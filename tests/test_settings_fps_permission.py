# -*- coding: utf-8 -*-
"""设置页「帧率采集权限」行：仅 Windows 创建、跟随帧率开关显隐、后台授权回填（不弹真 UAC）。"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import pet.modern_settings_dialog as settings_mod
from pet import fps_permission, settings_network
from pet.config import Config
from pet.modern_settings_dialog import SettingRow

app = QApplication.instance() or QApplication([])

win_only = pytest.mark.skipif(sys.platform != "win32", reason="授权行仅 Windows 创建")


@pytest.fixture(autouse=True)
def _reset_process_state():
    settings_network._FPS_GRANT_JOB.clear()
    fps_permission._state["granted"] = False
    yield
    t = settings_network._FPS_GRANT_JOB.get("thread")
    if t is not None:
        t.join(5)
    settings_network._FPS_GRANT_JOB.clear()
    fps_permission._state["granted"] = False


def _dialog(tmp_path, monkeypatch, status=fps_permission.STATUS_MISSING, supported=True):
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    monkeypatch.setattr(fps_permission, "is_supported", lambda: supported)
    calls = {"status": status}
    monkeypatch.setattr(
        fps_permission, "query_status",
        lambda granted=None, **_k: (fps_permission.STATUS_PENDING
                                    if fps_permission.granted_this_process()
                                    else calls["status"]))
    cfg = Config(base=tmp_path)
    return settings_mod.ModernSettingsDialog(cfg, include_ai=True), cfg


def _row(dialog, key) -> SettingRow:
    row = dialog.findChild(SettingRow, f"settingRow_{key}")
    assert row is not None, f"缺少设置行 {key}"
    return row


def _wait(predicate, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@win_only
def test_row_exists_and_follows_fps_switch(tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch)
    try:
        row = _row(dialog, "game_fps_permission")
        assert row.control is dialog.game_fps_permission_btn
        dialog.island_enabled_check.setChecked(True)
        dialog.island_info_check.setChecked(True)
        dialog.island_fps_check.setChecked(False)
        assert row.isHidden()
        dialog.island_fps_check.setChecked(True)
        assert not row.isHidden()
    finally:
        dialog.deleteLater()


@win_only
@pytest.mark.parametrize(
    "status, enabled",
    [
        (fps_permission.STATUS_MISSING, True),
        (fps_permission.STATUS_ACTIVE, False),
        (fps_permission.STATUS_UNSUPPORTED, True),
    ],
)
def test_initial_button_state(tmp_path, monkeypatch, status, enabled):
    dialog, _ = _dialog(tmp_path, monkeypatch, status)
    try:
        btn = dialog.game_fps_permission_btn
        assert btn.text() == settings_network._FPS_PERMISSION_TEXT[status]
        assert btn.isEnabled() is enabled
    finally:
        dialog.deleteLater()


@win_only
def test_grant_ok_moves_to_pending_relogon(tmp_path, monkeypatch):
    dialog, cfg = _dialog(tmp_path, monkeypatch)
    try:
        before = dict(cfg.data) if hasattr(cfg, "data") else None
        def ok():
            fps_permission.mark_granted()
            return fps_permission.GrantResult("ok")

        settings_network.start_fps_permission_grant(dialog, grant=ok)
        btn = dialog.game_fps_permission_btn
        assert _wait(lambda: btn.text() == settings_network._FPS_PERMISSION_TEXT[
            fps_permission.STATUS_PENDING])
        assert not btn.isEnabled()
        if before is not None:
            assert dict(cfg.data) == before, "授权不写配置"
    finally:
        dialog.deleteLater()


@win_only
@pytest.mark.parametrize("outcome", ["cancelled", "error", "unknown"])
def test_grant_failure_restores_button_with_reason(tmp_path, monkeypatch, outcome):
    dialog, _ = _dialog(tmp_path, monkeypatch)
    try:
        settings_network.start_fps_permission_grant(
            dialog, grant=lambda: fps_permission.GrantResult(outcome, "原因X"))
        btn = dialog.game_fps_permission_btn
        assert _wait(lambda: btn.isEnabled())
        assert btn.text() == settings_network._FPS_PERMISSION_BUTTON
        assert btn.toolTip() == "原因X"
    finally:
        dialog.deleteLater()


@win_only
def test_grant_exception_is_reported_not_swallowed(tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("炸了")

    try:
        settings_network.start_fps_permission_grant(dialog, grant=boom)
        btn = dialog.game_fps_permission_btn
        assert _wait(lambda: btn.isEnabled())
        assert "炸了" in btn.toolTip()
    finally:
        dialog.deleteLater()


def test_not_created_on_unsupported_platform(tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch, supported=False)
    try:
        assert getattr(dialog, "game_fps_permission_btn", None) is None
        assert dialog.findChild(SettingRow, "settingRow_game_fps_permission") is None
    finally:
        dialog.deleteLater()


@win_only
def test_status_query_spawns_no_subprocess(monkeypatch):
    """状态查询走进程内令牌 API（默认 check）：打开设置页不起子进程、不卡 GUI。"""
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("不应起子进程"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("不应起子进程"))
    assert fps_permission.query_status() in {fps_permission.STATUS_ACTIVE,
                                              fps_permission.STATUS_MISSING,
                                              fps_permission.STATUS_PENDING}


@win_only
def test_close_dialog_mid_grant_then_reopen(tmp_path, monkeypatch):
    """授权进行中关闭并销毁对话框：任务不随窗口消失，重开后接上结果且不重复弹 UAC。"""
    import threading

    gate = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        gate.wait(5)
        fps_permission.mark_granted()
        return fps_permission.GrantResult("ok")

    dialog, _ = _dialog(tmp_path, monkeypatch)
    settings_network.start_fps_permission_grant(dialog, grant=slow)
    dialog.close()
    dialog.deleteLater()
    app.processEvents()

    dialog2, _ = _dialog(tmp_path, monkeypatch)
    try:
        btn = dialog2.game_fps_permission_btn
        assert not btn.isEnabled() and btn.text() == "等待系统授权…"
        settings_network.start_fps_permission_grant(dialog2, grant=slow)  # 进行中再点
        gate.set()
        assert _wait(lambda: btn.text() == settings_network._FPS_PERMISSION_TEXT[
            fps_permission.STATUS_PENDING])
        assert calls == [1], "同一时刻只跑一个授权"
    finally:
        dialog2.deleteLater()


@win_only
def test_pending_survives_dialog_rebuild(tmp_path, monkeypatch):
    fps_permission.mark_granted()
    dialog, _ = _dialog(tmp_path, monkeypatch)
    try:
        btn = dialog.game_fps_permission_btn
        assert btn.text() == settings_network._FPS_PERMISSION_TEXT[
            fps_permission.STATUS_PENDING]
        assert not btn.isEnabled()
    finally:
        dialog.deleteLater()


@win_only
def test_rebuild_after_job_finished_shows_its_result(tmp_path, monkeypatch):
    """窗口关闭期间授权已失败：同进程重开设置页直接回填该结果，不丢原因。"""
    dialog, _ = _dialog(tmp_path, monkeypatch)
    settings_network.start_fps_permission_grant(
        dialog, grant=lambda: fps_permission.GrantResult("unknown", "等待授权超时"))
    settings_network._FPS_GRANT_JOB["thread"].join(5)
    dialog.deleteLater()
    app.processEvents()
    dialog2, _ = _dialog(tmp_path, monkeypatch)
    try:
        btn = dialog2.game_fps_permission_btn
        assert btn.isEnabled() and btn.text() == settings_network._FPS_PERMISSION_BUTTON
        assert btn.toolTip() == "等待授权超时"
    finally:
        dialog2.deleteLater()
