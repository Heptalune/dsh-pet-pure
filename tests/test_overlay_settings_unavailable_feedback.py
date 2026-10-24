# -*- coding: utf-8 -*-
"""overlay 拓扑「桌宠设置」交不出去时的可见反馈（此前静默无反应）。

背景：``settings_process_isolation`` 是设置界面没有入口的隐藏 config 键（默认
True）。手工置 false 时，legacy 拓扑有进程内设置窗兜底（``PetInstance.
open_modern_settings`` 回退 ``ModernSettingsDialog``），overlay 拓扑没有
（没有 PetWindow 可挂 parent），``OverlayShell.open_settings_for`` 直接
return False——菜单项看着可用，点下去毫无反应。

修法：交不出去（开关关闭或独立设置进程拉起失败）就在被点处冒一条气泡说明 +
日志。反馈收在 ``open_settings_for``（唯一路由点）→ 托盘、右键 facade 的
``on_open_modern_settings`` / ``on_open_settings`` 三条入口自动都有。

时序纪律：offscreen；真壳 + 真气泡控件（读 ``_speech_bubble._raw_text``），
同步直调 + 宽预算轮询，不用固定 sleep 猜时序。
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pet.app import AppShell
from tests.test_overlay_spawn import _make_persistent_shell

app = QApplication.instance() or QApplication([])

MENTION_KEY = "settings_process_isolation"


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _settings_host(config):
    """最小 AppShell 桩：只接 ``open_settings_process`` 那条真闸门。

    口径同 ``tests/test_settings_process_isolation.py::_bare_shell``——用真方法而
    不是 lambda 替身，测的才是产品里那条判定（开关关闭 → 不拉起、返回 False）。
    """
    host = AppShell.__new__(AppShell)
    host.config = config
    return host


def _tray_entry(shell, label: str = "桌宠设置"):
    menu = shell.tray.contextMenu()
    assert menu is not None
    for action in menu.actions():
        if action.text() == label:
            return action
    raise AssertionError(f"托盘菜单缺项 {label}")


def _teardown(shell) -> None:
    shell._teardown_settings_command_watch()


def test_open_settings_for_with_isolation_off_bubbles_and_returns_false(tmp_path):
    """真开关：``settings_process_isolation=false`` → False + 气泡点名那个键。"""
    shell, _made = _make_persistent_shell(tmp_path)
    try:
        shell._config.set("settings_process_isolation", False)
        host = _settings_host(shell._config)
        assert host.open_settings_process(object()) is False, "前提：真闸门拒绝拉起"
        shell._instance.shell = host
        shell.overlay.show()

        assert shell.open_settings_for(shell.sprite) is False
        text = shell._speech_bubble._raw_text
        assert MENTION_KEY in text, f"必须说明原因，实际：{text!r}"
    finally:
        _teardown(shell)


def test_open_settings_for_with_launch_failure_bubbles_other_reason(tmp_path):
    """开关开着但拉起失败：同样有反馈，且不误报成"开关被关掉"。"""
    shell, _made = _make_persistent_shell(tmp_path)
    try:
        shell._instance.shell = SimpleNamespace(
            open_settings_process=lambda inst: False)
        shell.overlay.show()

        assert shell.open_settings_for(shell.sprite) is False
        text = shell._speech_bubble._raw_text
        assert "桌宠设置没打开" in text, f"必须给可见反馈，实际：{text!r}"
        assert MENTION_KEY not in text, "开关开着时不许说成开关被关掉"
    finally:
        _teardown(shell)


def test_open_settings_for_hands_off_stays_silent(tmp_path):
    """交出去成功（isolation=true 的正常路径）：行为不变，不冒反馈气泡。"""
    shell, _made = _make_persistent_shell(tmp_path)
    try:
        shell._instance.shell = SimpleNamespace(
            open_settings_process=lambda inst: True)
        shell.overlay.show()

        assert shell.open_settings_for(shell.sprite) is True
        assert shell._speech_bubble._raw_text == "", "成功路径不许冒反馈气泡"
    finally:
        _teardown(shell)


def test_open_settings_for_without_app_shell_stays_silent(tmp_path):
    """没有 AppShell（测试替身/最小外壳）：返回 False 但不冒泡（不是用户可见故障）。"""
    shell, _made = _make_persistent_shell(tmp_path)
    try:
        shell._instance.shell = None
        shell.overlay.show()

        assert shell.open_settings_for(shell.sprite) is False
        assert shell._speech_bubble._raw_text == ""
    finally:
        _teardown(shell)


def test_overlay_tray_settings_click_with_isolation_off_bubbles(tmp_path):
    """用户路径：托盘点「桌宠设置」，isolation=false 时看到气泡而不是静默。"""
    shell, _made = _make_persistent_shell(tmp_path)
    try:
        shell._config.set("settings_process_isolation", False)
        shell._instance.shell = _settings_host(shell._config)
        shell.overlay.show()

        _tray_entry(shell).trigger()
        assert _wait_until(
            lambda: MENTION_KEY in shell._speech_bubble._raw_text
        ), f"点击必须给可见反馈，实际：{shell._speech_bubble._raw_text!r}"
    finally:
        _teardown(shell)
