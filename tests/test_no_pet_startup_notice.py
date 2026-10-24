# -*- coding: utf-8 -*-
"""零宠物启动不许静默（issue #286）。

报告者的现场是「进程在、托盘在、桌面上什么都没有」：CPU 累计 0.72 秒且 5 秒不增长
（0 sprite 时 tick 表停表，M12c）、无线程活动、无子进程——观感上等同于"启动失败或
被安全软件拦了"，而程序**既不报错也不打日志**，用户侧完全无法自查。

零活跃宠物本身是合法状态（点过托盘「退出这只」、``overlay-active-pets.json`` 损坏或
版本不认识、slot0 档案降级被剥出活跃集），所以不改变它是否合法，只要求它**可观测**：
overlay 与 legacy 两条路径都必须留下 WARN，并把事实说给用户（托盘 tooltip / 恢复窗）。

纪律：offscreen；真 OverlayShell + 假屏；legacy 侧用绑定真方法的替身（同
``test_multi_screen_interaction.FakePet`` 的做法），不伪造产品逻辑。
"""
from __future__ import annotations

import json
import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import pet.app as app_mod
import tests.test_overlay_window_capabilities as cap
from pet import pet_profiles
from pet.overlay_shell import OverlayShell

app = QApplication.instance() or QApplication([])


def _write_empty_active_set(config_dir) -> None:
    """``overlay-active-pets.json`` 显式空清单：合法状态，一只宠物都不复活。"""
    result = pet_profiles.save_active_set(config_dir, ())
    assert result.ok, f"前提：空活跃集合应当可写：{result}"


def _empty_shell(tmp_path, screen, values=None):
    """零宠物壳：``values`` 传每宠键会落一份**合法** slot0 档案，故默认不落。"""
    config = cap.CapConfig(tmp_path, values)
    return OverlayShell(
        app, cap.CapInstance(config), screen=screen,
        sprite_factory=lambda lib, pos, scale: cap.CapSprite(lib, pos, scale))


# ---------------------------------------------------------------- overlay 拓扑


def test_overlay_zero_pet_startup_warns(tmp_path, caplog):
    """零宠物启动必须留 WARN，并把活跃集合与配置目录一并写出来（可排查）。"""
    _write_empty_active_set(tmp_path)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))

    with caplog.at_level(logging.WARNING, logger="pet.overlay_shell"):
        shell = _empty_shell(tmp_path, screen)

    assert shell._all_sprites() == [], "前提：这个壳确实一只宠物都没有"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("没有任何宠物" in message for message in warnings), warnings
    assert any(str(tmp_path) in message for message in warnings), \
        "诊断必须点名配置目录，否则用户无从自查"


def test_overlay_zero_pet_startup_explains_itself(tmp_path):
    """同一状态要说给用户听，**且不得污染 slot0 诊断源**。

    ``slot0_profile_diagnosis`` 是"slot0 档案降级"的专用诊断（``_spawn_failure_text``
    优先引用它、因此带上坏档案路径）；零宠物是另一回事，塞进去会把那条线索顶掉。
    """
    _write_empty_active_set(tmp_path)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))

    shell = _empty_shell(tmp_path, screen)

    assert shell.slot0_profile_diagnosis() == "", "零宠物不得占用 slot0 诊断源"
    label = getattr(shell, "_recovery_diagnosis_label", None)
    if label is not None:  # offscreen 下恢复窗会建起来：说明不许是空白
        assert "没有可显示的桌宠" in label.text()
    tray = getattr(shell, "tray", None)
    if tray is not None:  # 可用时必须说实话
        assert "没有可显示的桌宠" in tray.toolTip()


def test_zero_pets_does_not_occupy_the_slot0_diagnosis_source(tmp_path):
    """守卫：零宠物提示**不得**占用 slot0 降级诊断源。

    ``_spawn_failure_text`` 在 ``slot0_profile_diagnosis()`` 非空时优先引用它，因而
    带上坏档案路径；本片首版在这里塞了"没有宠物"，于是"生成桌宠失败"的说明丢掉
    路径（`tests/test_slot0_lifecycle.py` 的恢复窗用例当场红）。本用例把下游契约
    直接钉住：零宠物之后，失败说明仍然带路径。
    """
    directory = tmp_path  # 壳的 config 目录就是 tmp_path（CapConfig 的 dir）
    assert pet_profiles.save_active_set(directory, ()).ok
    broken = pet_profiles.slot_profile_path(directory, 0)
    broken.write_bytes(b"{broken")
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))

    shell = _empty_shell(tmp_path, screen)          # 构造期已经跑过一次提示
    assert shell._all_sprites() == [], "前提：这个壳确实一只宠物都没有"

    assert shell.slot0_profile_diagnosis() == "", "零宠物不得占用 slot0 诊断源"
    shell._announce_no_pets_if_empty()              # 再跑一次同样不许占用
    assert shell.slot0_profile_diagnosis() == ""

    # 下游契约：生成失败必须点名该槽档案路径
    assert str(broken) in shell._spawn_failure_text(0, RuntimeError("boom"))


def test_overlay_zero_pet_does_not_swallow_the_whole_screen(tmp_path):
    """守卫：空合成窗必须**处处穿透**，绝不吞掉桌面点击。

    一个看得见的全屏置顶窗如果不可穿透，用户会看到"整个桌面点不动"——那才是
    issue #286 里"整屏鼠标失效"的真实观感。空 sprite 列表下逐像素判据必须恒为
    穿透（这条是既有设计，此处钉住，防止以后为了"让空窗可点"而改坏它）。
    """
    _write_empty_active_set(tmp_path)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = _empty_shell(tmp_path, screen)

    overlay = shell.overlay
    from PySide6.QtCore import QPoint

    for point in (QPoint(0, 0), QPoint(960, 520), QPoint(1919, 1039)):
        assert overlay._is_transparent_at(point) is True, point
    assert overlay.sprite_at(QPoint(960, 520)) is None


def test_overlay_with_a_pet_keeps_quiet(tmp_path, caplog):
    """对照：有宠物时不打这条 WARN（否则日志噪音会把真现场埋掉）。"""
    cap.CapConfig(tmp_path, {"scale": 0.5})
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))

    with caplog.at_level(logging.WARNING, logger="pet.overlay_shell"):
        shell = _empty_shell(tmp_path, screen)

    assert shell._all_sprites(), "前提：默认档案下 slot0 应当建起来"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("没有任何宠物" in message for message in warnings), warnings


# ---------------------------------------------------------------- legacy 拓扑


class _TrayStub:
    """绑定真 ``AppShell`` 方法的替身：只补齐托盘路径真正用到的面。"""

    _build_empty_pet_tray = app_mod.AppShell._build_empty_pet_tray
    _install_tray_menu = app_mod.AppShell._install_tray_menu
    _tray_placeholder_icon = app_mod.AppShell._tray_placeholder_icon

    def __init__(self):
        self.tray = None
        self._tray_menu = None
        self._tray_submenus = []
        self._active_pet_slots = ()
        self.spawn_calls = 0

        class _Config:
            dir = "CONFIG-DIR-SENTINEL"

        class _Instance:
            config = _Config()

            def open_modern_settings(self):
                pass

        self.instance = _Instance()

    def spawn_pet(self):
        self.spawn_calls += 1

    def request_app_quit(self):
        pass


def test_legacy_empty_pet_tray_warns(caplog):
    """legacy 零宠物（只剩托盘、没有捕获窗）同样必须留 WARN 并点名配置目录。"""
    stub = _TrayStub()

    with caplog.at_level(logging.WARNING, logger="pet.app"):
        stub._build_empty_pet_tray()

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("slot0 未活跃" in message for message in warnings), warnings
    assert any("CONFIG-DIR-SENTINEL" in message for message in warnings), \
        "诊断必须点名配置目录"


def test_legacy_empty_pet_tray_keeps_recovery_entry(caplog):
    """可见入口不许被日志改动挤掉：菜单里仍有「生成 / 恢复 slot0」。"""
    stub = _TrayStub()

    with caplog.at_level(logging.WARNING, logger="pet.app"):
        stub._build_empty_pet_tray()

    menu = stub._tray_menu
    assert menu is not None
    labels = [action.text() for action in menu.actions()]
    assert "生成 / 恢复 slot0" in labels, labels
