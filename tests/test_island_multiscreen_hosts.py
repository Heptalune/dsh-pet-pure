# -*- coding: utf-8 -*-
"""灵动岛在多窗（混合缩放）下的宿主绑定与「全体」可见性。

混合 DPI + 允许跨屏时一块屏一张合成窗（``OverlayShell._multi_window()``）：每张
窗有自己的碰撞世界与局部坐标原点。岛是独立顶层窗、自己有屏，所以：

- 墙必须绑**岛所在屏**那个世界。绑在主窗世界时，副屏上的宠在另一份世界/坐标系
  里——宠直接穿过岛，拖岛拍鱼也没有碰撞事件；
- 「还有没有宠可见 / 显示桌宠」必须看**全部**宿主的成员：主窗空着而副屏有宠不算
  "一只都没有"，反过来副屏那几只逐只藏光也不算"还有"。

本机通常只有一块屏，故这里用假屏驱动真实的宿主创建、岛桥接线与壳层可见性判据。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from pet import collision
from pet.app import AppShell
from tests.test_overlay_hosts_multiscreen import (
    _install_screens,
    _make_shell,
    _two_screens,
)

app = QApplication.instance() or QApplication([])

ISLAND_MEMBER = collision.ISLAND_MEMBER_ID


class _Island:
    """岛替身：自有屏（``_current_screen``）+ 全局几何 + 显隐。

    面与 ``DynamicIsland`` 对岛桥可见的那几项对齐（``geometry`` / ``isVisible`` /
    ``_mode`` / ``_geo_to`` / ``on_geometry_changed``）——桥的可见性与几何同步
    全走这些鸭子类型入口。
    """

    def __init__(self, screen, rect: QRect):
        self._screen = screen
        self._rect = QRect(rect)
        self._mode = "floating"
        self._hover_peek = False
        self._geo_to = None
        self._visible = True
        self.on_geometry_changed = None

    def _current_screen(self):
        return self._screen

    def geometry(self) -> QRect:
        return QRect(self._rect)

    def isVisible(self) -> bool:  # noqa: N802 (Qt 命名)
        return self._visible

    def bump(self, *_args) -> None:
        pass

    def move_to(self, screen, rect: QRect) -> None:
        """搬岛：换屏 + 换几何，并按产品口径发一次几何回调（Move 事件等价）。"""
        self._screen = screen
        self._rect = QRect(rect)
        callback = self.on_geometry_changed
        if callable(callback):
            callback()


def _mixed_shell(tmp_path, monkeypatch):
    """混合缩放双屏：A@100% + B@200%（= 每块屏一张合成窗）。"""
    a, b = _two_screens()
    b.set_rects((1920, 0, 1920, 1080), (1920, 0, 1920, 1040), dpr=2.0)
    _install_screens(monkeypatch, a, b)
    shell = _make_shell(tmp_path, a)
    assert len(shell.hosts()) == 2, "前提：混合缩放 = 每块屏一张窗"
    return shell, a, b


def _app_shell(shell) -> AppShell:
    """AppShell 桩：可见性判据只碰 ``_overlay_shell`` / ``_instances`` / ``island``。"""
    stub = AppShell.__new__(AppShell)
    stub._instances = []
    stub._overlay_shell = shell
    stub.island = None
    stub.island_chat = None
    return stub


def _cleanup(shell) -> None:
    shell.stop()
    QApplication.processEvents()


def _two_pets_a_and_b(shell, a, b):
    """主宠留在 A，新生那只有意搬到 B（两窗各有成员）。"""
    shell._show_all_hosts()
    shell.spawn_pet()
    child = shell._spawned[0]
    assert shell.move_sprite_to_screen(child, b) is True
    assert shell.host_for_sprite(shell.sprite).screen is a
    assert shell.host_for_sprite(child).screen is b
    return shell.sprite, child


# ---------------------------------------------------------------- 岛墙宿主绑定
def test_island_wall_binds_to_the_screen_the_island_sits_on(tmp_path, monkeypatch):
    """岛在副屏 → 墙绑**副屏**宿主（不是主宿主）。

    绑错时副屏那只宠与岛不在同一个碰撞世界：宠整只穿过岛，拖岛拍鱼也不再产生
    碰撞事件（岛速估算了也没人消费）。
    """
    shell, a, b = _mixed_shell(tmp_path, monkeypatch)
    try:
        host_b = shell.host_for_screen(b)
        assert host_b is not None and host_b is not shell._main_host
        island = _Island(b, QRect(2300, 100, 400, 80))

        shell.attach_island(island)

        assert shell._island_host is host_b, "岛在副屏：墙必须绑副屏宿主"
        assert shell.island_bridge.core._world is host_b.collision  # noqa: SLF001
        # 局部坐标 = 岛全局几何 - 副屏窗口原点（1920, 0）
        assert host_b.collision._static_members[ISLAND_MEMBER] == (  # noqa: SLF001
            380.0, 100.0, 400.0, 80.0)
        assert ISLAND_MEMBER not in shell.collision._static_members, (  # noqa: SLF001
            "主窗世界不该有幽灵墙")
    finally:
        shell.attach_island(None)
        _cleanup(shell)


def test_island_wall_follows_the_island_to_another_screen(tmp_path, monkeypatch):
    """岛搬到另一块屏 → 墙跟着换世界（旧世界的成员撤干净）。"""
    shell, a, b = _mixed_shell(tmp_path, monkeypatch)
    try:
        host_b = shell.host_for_screen(b)
        island = _Island(b, QRect(2300, 100, 400, 80))
        shell.attach_island(island)
        assert shell._island_host is host_b

        island.move_to(a, QRect(600, 300, 400, 80))

        assert shell._island_host is shell._main_host
        assert shell.island_bridge.core._world is shell.collision  # noqa: SLF001
        assert shell.collision._static_members[ISLAND_MEMBER] == (  # noqa: SLF001
            600.0, 300.0, 400.0, 80.0)
        assert ISLAND_MEMBER not in host_b.collision._static_members, (  # noqa: SLF001
            "旧世界的墙必须撤掉")
    finally:
        shell.attach_island(None)
        _cleanup(shell)


def test_island_wall_is_not_rebuilt_when_a_pet_changes_screens(tmp_path, monkeypatch):
    """宠跨窗移交不改岛的墙：岛与宠不是同一生命周期，岛的原点不跟着宠走。"""
    shell, a, b = _mixed_shell(tmp_path, monkeypatch)
    try:
        host_b = shell.host_for_screen(b)
        island = _Island(b, QRect(2300, 100, 400, 80))
        shell.attach_island(island)
        bridge = shell.island_bridge
        before = host_b.collision._static_members[ISLAND_MEMBER]  # noqa: SLF001

        _two_pets_a_and_b(shell, a, b)
        shell.move_sprite_to_screen(shell.sprite, b)  # 真移交：主宠也搬到副屏

        assert shell.island_bridge is bridge, "宠移交不重建岛桥"
        assert shell._island_host is host_b
        assert host_b.collision._static_members[ISLAND_MEMBER] == before  # noqa: SLF001
    finally:
        shell.attach_island(None)
        _cleanup(shell)


# ---------------------------------------------------------------- 全体可见性
def test_main_window_pets_hidden_but_secondary_pet_visible(tmp_path, monkeypatch):
    """主窗的宠全隐藏、副屏仍有可见宠 → 仍算「有可见宠」。"""
    shell, a, b = _mixed_shell(tmp_path, monkeypatch)
    try:
        main, child = _two_pets_a_and_b(shell, a, b)
        stub = _app_shell(shell)

        shell.set_sprite_visible(main, False)

        assert main.visible is False
        assert child.visible is True
        assert shell._main_host.overlay.sprites == [main], "前提：主窗只剩这只（已藏）"
        assert AppShell._overlay_sprite_visible(shell) is True
        assert AppShell._aggregate_pet_visible(stub) is True
    finally:
        _cleanup(shell)


def test_every_pet_on_secondary_and_each_hidden_is_not_visible(tmp_path, monkeypatch):
    """全部宠都在副屏、逐只藏光 → 判「无可见宠」（主窗空 ≠ 一只都没有）。"""
    shell, a, b = _mixed_shell(tmp_path, monkeypatch)
    try:
        main, child = _two_pets_a_and_b(shell, a, b)
        stub = _app_shell(shell)
        assert shell.move_sprite_to_screen(main, b) is True
        assert shell._main_host.overlay.sprites == [], "前提：主窗已空"

        for sprite in (main, child):
            shell.set_sprite_visible(sprite, False)

        assert AppShell._overlay_sprite_visible(shell) is False
        assert AppShell._aggregate_pet_visible(stub) is False
    finally:
        _cleanup(shell)


def test_show_pets_from_island_chat_restores_secondary_pets(tmp_path, monkeypatch):
    """「显示桌宠」把**副屏**被藏的宠也叫回来（不只恢复主窗）。"""
    shell, a, b = _mixed_shell(tmp_path, monkeypatch)
    try:
        main, child = _two_pets_a_and_b(shell, a, b)
        stub = _app_shell(shell)
        assert shell.move_sprite_to_screen(main, b) is True
        for sprite in (main, child):
            shell.set_sprite_visible(sprite, False)
        main_host = shell.host_for_screen(b)
        assert AppShell._overlay_sprite_visible(shell) is False

        AppShell._show_pets_from_island_chat(stub)

        assert shell._any_host_visible(), "窗要回来"
        assert all(sprite.visible for sprite in main_host.overlay.sprites), (
            "副屏被藏的宠也必须恢复")
        assert AppShell._overlay_sprite_visible(shell) is True
    finally:
        _cleanup(shell)


def test_unplugging_the_secondary_screen_rebinds_the_island_wall(tmp_path, monkeypatch):
    """拔掉混合缩放的副屏后，墙必须重绑到存活宿主（不能因为塌回单窗就早退）。

    拔屏后只剩一块屏 → ``_multi_window()`` 变假。原先``_rebind_island_host`` 在那
    一点直接 return，于是桥继续引用**已拆宿主**的碰撞世界：拔屏到岛下一次移动
    之间，存活屏上的宠撞不到岛（拖岛拍鱼也没有碰撞事件）。
    """
    shell, a, b = _mixed_shell(tmp_path, monkeypatch)
    try:
        island = _Island(b, QRect(2300, 100, 400, 80))
        shell.attach_island(island)
        dead = shell.host_for_screen(b)
        assert shell._island_host is dead, "前提：墙此刻绑在副屏宿主"

        island._screen = a                     # 系统把岛搬回存活屏
        shell.handle_screen_removed(b)         # 副屏被拔掉（含屏集合收缩）

        assert len(shell.hosts()) == 1, "前提：多窗已塌回单窗"
        assert shell._island_host is shell._main_host, "墙必须重绑到存活宿主"
        assert shell.island_bridge.core._world is shell.collision  # noqa: SLF001
        assert ISLAND_MEMBER in shell.collision._static_members  # noqa: SLF001
    finally:
        shell.attach_island(None)
        _cleanup(shell)
