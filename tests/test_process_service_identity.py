# -*- coding: utf-8 -*-
"""进程级服务的角色身份 = **每宠真值**（slot 档案），不是全局配置的默认残留。

回归（2026-10-19）：换完角色后身体与头像已换，灵动岛名、托盘「切换角色」勾选、
主动识屏发给模型的 ``pet_name`` 却都停在默认角色。三处都在**进程级**服务上，
读的是进程级 ``config``（全局面），而 ``character`` 是 ``PER_PET_KEYS`` 里的每宠键：
真值在 ``config-slot-N.json``。全局面那个 ``character`` 只是出厂默认的残留——
slot0 迁移与 ``Config.save`` 都会把每宠键从 ``config.json`` 清掉——所以这些读点
不是「没值」，是稳定读到**错的默认值**。

修法口径：进程级配置继续给全局设置用，角色身份另走一条明确的 provider
（``AppShell._identity_config``：当前呈现身份的每宠视图），不把共享服务的
``config`` 换成某只宠的配置。断言因此落在三处**公开面的真实取值**上。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from pet import catalog, pet_profiles  # noqa: E402
from pet.app import AppShell  # noqa: E402
from pet.config import Config  # noqa: E402

app = QApplication.instance() or QApplication([])


class _TrayWin:
    """建托盘所需的最小窗口桩（``_build_tray`` 只用到这几个面）。"""

    def __init__(self):
        self._speech_bubble = QWidget()

    def icon_pixmap(self, _size: int = 64):
        from PySide6.QtGui import QPixmap

        return QPixmap(2, 2)

    def hide_speech_bubble(self) -> None:
        pass

    def go_default_corner(self) -> None:
        pass

    def set_mouse_through(self, on: bool) -> None:
        pass

    def isVisible(self) -> bool:  # noqa: N802 (Qt 命名)
        return True

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass


@pytest.fixture
def shell_with_slot_role(tmp_path):
    """真 AppShell：slot0 档案角色 = ``other``，全局面仍是出厂默认。

    刻意走真 ``AppShell``（不是 ``__new__`` 桩）：本缺陷要证明的正是「进程级服务
    经哪条路径取角色」，桩掉构造等于把被验的接线一起桩掉。
    """
    global_cfg = Config(base=tmp_path)
    assert pet_profiles.write_profile_settings(
        global_cfg.dir, 0, {"character": "other"}).status == "ok"
    shell = AppShell(app, global_cfg, enable_chat=False)
    try:
        yield shell
    finally:
        # 建岛会向 ChatService 注册进程级完成回调（弱引用只覆盖弱集，这份是类级表）：
        # 撤掉它，免得测试进程里留下指向已拆壳的回调。
        from pet.chat.service import ChatService

        ChatService.unregister_global_finished(shell._on_global_chat_finished)
        island = getattr(shell, "island", None)
        if island is not None:
            island.hide()
            island.deleteLater()
        shell._shared.proactive.stop_all()


def test_global_config_only_carries_the_default_role(shell_with_slot_role):
    """前提：全局面读到的是出厂默认（本缺陷不是「缺值」，是读到错值）。"""
    shell = shell_with_slot_role
    assert shell.config.get("character") == catalog.DEFAULT_CHARACTER
    assert shell.instance.config.get("character") == "other", (
        "前提：每宠真值在 slot0 档案里")


def test_island_name_follows_the_slot_role(shell_with_slot_role):
    """灵动岛名读当前身份，不是全局面的默认角色。"""
    shell = shell_with_slot_role
    shell._sync_dynamic_island()

    assert shell.island is not None
    assert shell.island._character_name() == "other"

    # 换角色不需要重新注入：provider 每次取名现取（用户点「切换角色」的那一刻起生效）
    shell.instance.config.set("character", "third-role")
    assert shell.island._character_name() == "third-role"


def test_tray_character_check_follows_the_slot_role(shell_with_slot_role, monkeypatch):
    """托盘「切换角色」勾选态标当前身份；素材只有随包一只时可注入第二只 id。"""
    shell = shell_with_slot_role
    monkeypatch.setattr(
        catalog, "list_available_characters", lambda: ["shenshen", "other"])

    tray = shell._build_tray(_TrayWin())
    menu = tray.contextMenu()
    assert menu is not None
    char_menu = next(action.menu() for action in menu.actions()
                     if action.text() == "切换角色" and action.menu() is not None)
    checked = [action.text() for action in char_menu.actions() if action.isChecked()]

    assert checked == ["other"]


def test_proactive_pet_name_follows_the_slot_role(shell_with_slot_role):
    """主动识屏注入模型的宠物名同样是当前身份（共享 watcher 的 cfg 是全局面）。"""
    shell = shell_with_slot_role
    watcher = shell._shared.proactive

    assert watcher.cfg is shell.config, "前提：共享 watcher 持进程级配置"
    assert watcher.pet_name() == "other"


def test_identity_config_prefers_the_main_sprite_view_in_overlay():
    """overlay 拓扑：身份取**主 sprite** 的每宠视图（不是进程级配置，也不能传 None）。

    传 ``None`` 会落进 ``_SpriteConfig`` 的"无 slot"分支，全线回落主配置——正是
    本缺陷的错值来源，故把"传的是主 sprite"这一条也钉住。
    """
    from types import SimpleNamespace

    main_sprite = object()
    sprite_view = SimpleNamespace(character="child-of-the-view")

    class _Overlay:
        sprite = main_sprite

        def __init__(self):
            self.asked: list = []

        def _sprite_config(self, sprite):
            self.asked.append(sprite)
            return sprite_view

    overlay = _Overlay()
    shell = AppShell.__new__(AppShell)
    shell.config = SimpleNamespace()
    shell.instance = SimpleNamespace(config=SimpleNamespace())
    shell._overlay_shell = overlay

    assert shell._identity_config() is sprite_view
    assert overlay.asked == [main_sprite]
