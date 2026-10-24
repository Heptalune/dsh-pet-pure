# -*- coding: utf-8 -*-
"""托盘「AI 设置 / 桌宠设置」入口：菜单先关、回调延到菜单跟踪会话之后。

背景：托盘菜单此前是裸 ``menu.addAction(text, callback)``——回调在原生菜单跟踪
会话里**内联执行**。macOS 的托盘菜单是 AppKit 原生 NSMenu，跟踪会话中新建/激活
窗口会被 AppKit 抑制（issue #4 家族：点了没反应，再点一次才对；右键菜单路径早已
用 ``close_on_trigger`` + ``exec()`` 返回后派发解决，托盘菜单没有 ``exec()``
收口点，故走 ``shared.run_after_menu_closed``：菜单先关、等它真的不在了再执行）。

同一口径覆盖托盘菜单里**会改窗面**的其余条目（显示/隐藏、回到右下角、切换角色、
鼠标穿透、开机自启、灵动岛）：它们的旧接法是裸 ``menu.addAction``(回调) 或
``toggled``(回调)，都在菜单跟踪会话里内联执行——显示/隐藏要 ``show()`` 桌宠窗、
切换角色要重建角色窗面（与 issue #4 同病），其余条目改的是窗面/冒泡/系统登录项，
统一到「菜单先关、回调延后」这一条契约上。

验收口径：菜单可见期内回调不得执行；点击后菜单先关；菜单关闭后执行一次。
时序纪律：事件同步 + 宽预算轮询（``app.processEvents()``），不赌固定 sleep。
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from pet.app import AppShell
from pet.config import Config

app = QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _entry(menu, label: str):
    for action in menu.actions():
        if action.text() == label:
            return action
    raise AssertionError(
        f"托盘菜单缺项 {label}；实际: {[a.text() for a in menu.actions()]}")


class _TrayWin:
    """建托盘所需的最小窗口桩（刻意非 QWidget：_build_tray 的调用方可能是替身）。

    显隐 / 归位 / 穿透都记账：这几条托盘条目本批从「菜单会话内联执行」改为
    「菜单关闭后执行」，回归要能先后点数——先证明没内联，再证明执行了**一次**。
    """

    def __init__(self, config):
        self.config = config
        self._speech_bubble = QWidget()
        self.hidden_bubbles = 0
        self.visible = True
        self.show_calls = 0
        self.hide_calls = 0
        self.corner_calls = 0
        self.mouse_through_calls: list[bool] = []

    def icon_pixmap(self, _size: int = 64) -> QPixmap:
        return QPixmap(2, 2)

    def hide_speech_bubble(self) -> None:
        self.hidden_bubbles += 1

    def go_default_corner(self) -> None:
        self.corner_calls += 1

    def set_mouse_through(self, on: bool) -> None:
        self.mouse_through_calls.append(bool(on))
        self.config.set("mouse_through", bool(on))

    def isVisible(self) -> bool:  # noqa: N802 (Qt 命名)
        return self.visible

    def show(self) -> None:
        self.show_calls += 1
        self.visible = True

    def hide(self) -> None:
        self.hide_calls += 1
        self.visible = False


def _tray_shell(tmp_path):
    """新壳 + 记账窗口桩 + 已建好的托盘菜单（本文件条目级回归共用）。

    窗口桩共用壳自己的 config：显示/穿透这些条目的勾选态与副作用都读同一份配置，
    替身窗口另持一份会让断言看着对、实际接错线。
    """
    shell = AppShell(app, Config(tmp_path), enable_chat=True)
    win = _TrayWin(shell.config)
    tray = shell._build_tray(win)
    menu = tray.contextMenu()
    assert menu is not None
    return shell, win, tray, menu


def _open_menu(menu) -> None:
    menu.show()
    app.processEvents()
    assert menu.isVisible(), "前提：菜单在显示态"


def _submenu(menu, label: str):
    for action in menu.actions():
        if action.text() == label and action.menu() is not None:
            return action.menu()
    raise AssertionError(
        f"托盘菜单缺子菜单 {label}；实际: {[a.text() for a in menu.actions()]}")


def _close_tray(tray, *menus) -> None:
    tray.hide()
    for menu in menus:
        menu.close()
    app.processEvents()


def _legacy_tray(tmp_path, calls):
    shell = AppShell(app, Config(tmp_path), enable_chat=True)
    shell.instance.open_chat_settings = lambda: calls.append("ai")
    shell.instance.open_modern_settings = lambda: calls.append("pet")
    win = _TrayWin(Config(tmp_path))
    tray = shell._build_tray(win)
    menu = tray.contextMenu()
    assert menu is not None
    return shell, tray, menu


@pytest.mark.parametrize("label,expected", [("AI 设置", "ai"), ("桌宠设置", "pet")])
def test_legacy_tray_settings_entry_waits_for_menu_close(tmp_path, label, expected):
    """托盘这两个设置入口不得在菜单可见期内执行回调（macOS 首点被抑制）。"""
    calls: list = []
    _shell, tray, menu = _legacy_tray(tmp_path, calls)
    try:
        menu.show()
        app.processEvents()
        assert menu.isVisible(), "前提：菜单在显示态"

        _entry(menu, label).trigger()
        assert calls == [], f"{label}：菜单还开着，回调不许内联执行"
        assert not menu.isVisible(), f"{label}：点击后菜单必须先关"

        assert _wait_until(lambda: calls == [expected]), f"菜单关闭后必须执行一次，实际 {calls}"
    finally:
        tray.hide()
        menu.close()
        app.processEvents()


@pytest.mark.parametrize(
    "label,expected", [("AI 对话", "chat"), ("快速对话（气泡）", "quick")])
def test_legacy_tray_chat_entry_waits_for_menu_close(tmp_path, label, expected):
    """W4：托盘这两个开窗入口与设置入口同族，同样必须等菜单关闭后再派发。

    它们是裸 ``menu.addAction(text, callback)``——回调在原生菜单跟踪会话里内联
    执行，会话中新建窗口被 AppKit 抑制（同上：点了没反应）。
    """
    calls: list = []
    shell = AppShell(app, Config(tmp_path), enable_chat=True)
    shell.instance.open_chat = lambda *args, **kwargs: calls.append("chat")
    shell.instance.open_quick_chat = lambda *args, **kwargs: calls.append("quick")
    tray = shell._build_tray(_TrayWin(Config(tmp_path)))
    menu = tray.contextMenu()
    assert menu is not None
    try:
        menu.show()
        app.processEvents()
        assert menu.isVisible(), "前提：菜单在显示态"

        _entry(menu, label).trigger()
        assert calls == [], f"{label}：菜单还开着，回调不许内联执行"
        assert not menu.isVisible(), f"{label}：点击后菜单必须先关"

        assert _wait_until(lambda: calls == [expected]), f"菜单关闭后必须执行一次，实际 {calls}"
    finally:
        tray.hide()
        menu.close()
        app.processEvents()


def test_tray_menu_open_syncs_check_marks_without_side_effects(tmp_path):
    """弹出托盘菜单只同步勾选态看相，绝不顺手执行副作用。

    旧接线（``toggled``）在弹出前的那次程序化 ``setChecked`` 也会触发副作用：
    开一次菜单就把鼠标穿透/岛开关/系统登录项重写一遍。副作用改挂 ``triggered``
    后，程序化同步不发这个信号——勾选态接线不变，改动只落在"不再误触发"。
    """
    shell, win, tray, menu = _tray_shell(tmp_path)
    synced: list = []
    autostart_calls: list = []
    shell._sync_dynamic_island = lambda: synced.append(True)
    shell.instance._set_autostart = (
        lambda enabled, target=None: (autostart_calls.append(enabled), True)[1])
    try:
        shell.config.set("mouse_through", True)
        shell.config.set("dynamic_island", {"enabled": False})
        menu.aboutToShow.emit()
        app.processEvents()

        assert _entry(menu, "鼠标穿透").isChecked() is True, "勾选态跟着配置走"
        assert _entry(menu, "灵动岛").isChecked() is False, "勾选态跟着配置走"
        assert win.mouse_through_calls == [], "弹菜单不得动穿透开关"
        assert synced == [], "弹菜单不得同步岛"
        assert autostart_calls == [], "弹菜单不得写系统登录项"
    finally:
        _close_tray(tray, menu)


def test_overlay_tray_settings_entry_waits_for_menu_close(tmp_path):
    """overlay 拓扑的同一个入口（``OverlayShell._refresh_tray_menu``）同口径。"""
    from tests.test_overlay_spawn import _make_persistent_shell

    shell, _made = _make_persistent_shell(tmp_path)
    opened: list = []
    try:
        shell._instance.shell = SimpleNamespace(
            open_settings_process=lambda inst: (opened.append(inst), True)[1])
        menu = shell.tray.contextMenu()
        assert menu is not None
        menu.show()
        app.processEvents()
        assert menu.isVisible(), "前提：菜单在显示态"

        _entry(menu, "桌宠设置").trigger()
        assert opened == [], "菜单还开着，设置入口不许内联执行"
        assert not menu.isVisible(), "点击后菜单必须先关"
        assert _wait_until(lambda: len(opened) == 1), "菜单关闭后必须打开设置"
    finally:
        menu = shell.tray.contextMenu()
        if menu is not None:
            menu.close()
        shell._teardown_settings_command_watch()
        app.processEvents()


# ------------------------------------------------ 其余改窗面条目：同一契约
def test_tray_show_hide_waits_for_menu_close(tmp_path):
    """托盘「显示 / 隐藏」开/收桌宠窗（并同步岛），与设置入口同族：菜单先关再切。"""
    _shell, win, tray, menu = _tray_shell(tmp_path)
    try:
        _open_menu(menu)

        _entry(menu, "显示 / 隐藏").trigger()
        assert (win.hide_calls, win.show_calls) == (0, 0), \
            "菜单还开着，显隐动作不许内联执行"
        assert not menu.isVisible(), "点击后菜单必须先关"

        assert _wait_until(lambda: win.hide_calls == 1), "菜单关闭后必须切换一次显隐"
        assert win.show_calls == 0, "起点可见 → 只隐藏（不是连点两下式的抖动）"
    finally:
        _close_tray(tray, menu)


def test_tray_return_to_corner_waits_for_menu_close(tmp_path):
    """托盘「回到右下角」只移动既有窗口，但同样按这一条契约派发（不内联）。"""
    _shell, win, tray, menu = _tray_shell(tmp_path)
    try:
        _open_menu(menu)

        _entry(menu, "回到右下角").trigger()
        assert win.corner_calls == 0, "菜单还开着，归位不许内联执行"
        assert not menu.isVisible(), "点击后菜单必须先关"

        assert _wait_until(lambda: win.corner_calls == 1), "菜单关闭后必须归位一次"
    finally:
        _close_tray(tray, menu)


def test_tray_switch_character_waits_for_menu_close(tmp_path):
    """托盘「切换角色」要重建角色窗面，与开窗入口同族：菜单先关再换。"""
    shell, _win, tray, menu = _tray_shell(tmp_path)
    calls: list = []
    shell.instance.switch_character = lambda cid: calls.append(cid)
    char_menu = _submenu(menu, "切换角色")
    target = char_menu.actions()[0]
    try:
        _open_menu(menu)
        char_menu.show()
        app.processEvents()
        assert char_menu.isVisible(), "前提：子菜单在显示态"

        target.trigger()
        assert calls == [], "菜单还开着，换角色不许内联执行"
        assert not char_menu.isVisible(), "点击后子菜单必须先关"

        assert _wait_until(lambda: calls == [target.text()]), \
            f"菜单关闭后必须换一次角色，实际 {calls}"
    finally:
        _close_tray(tray, char_menu, menu)


def test_tray_island_check_waits_for_menu_close(tmp_path):
    """托盘「灵动岛」勾选：勾选态即时翻转（用户看得到），岛同步延到菜单关闭后。

    旧接法是 ``toggled``——菜单弹出前 ``setChecked`` 同步勾选态时也会被误触发
    （弹一次菜单就把配置重写一遍），且开/关岛窗发生在跟踪会话里。
    """
    shell, _win, tray, menu = _tray_shell(tmp_path)
    synced: list = []
    shell._sync_dynamic_island = lambda: synced.append(True)
    action = _entry(menu, "灵动岛")
    before = bool(shell.config.get("dynamic_island", {}).get("enabled", True))
    try:
        _open_menu(menu)

        action.trigger()
        assert action.isChecked() is (not before), "勾选态必须即时翻转"
        assert synced == [], "菜单还开着，岛同步不许内联执行"
        assert not menu.isVisible(), "点击后菜单必须先关"

        assert _wait_until(lambda: synced == [True]), "菜单关闭后必须同步一次岛"
        assert bool(shell.config.get("dynamic_island", {}).get("enabled", True)) is (not before), \
            "落盘的是用户点出来的勾选态"
    finally:
        _close_tray(tray, menu)


def test_tray_mouse_through_check_waits_for_menu_close(tmp_path):
    """托盘「鼠标穿透」勾选：改的是窗面/点击穿透，同样等菜单关闭后落地。"""
    _shell, win, tray, menu = _tray_shell(tmp_path)
    action = _entry(menu, "鼠标穿透")
    assert action.isChecked() is False, "前提：配置默认关闭穿透"
    try:
        _open_menu(menu)

        action.trigger()
        assert action.isChecked() is True, "勾选态必须即时翻转"
        assert win.mouse_through_calls == [], "菜单还开着，穿透开关不许内联落到窗口"
        assert not menu.isVisible(), "点击后菜单必须先关"

        assert _wait_until(lambda: win.mouse_through_calls == [True]), \
            "菜单关闭后必须落一次穿透开关"
        assert win.config.get("mouse_through") is True, "落盘的是勾选态"
    finally:
        _close_tray(tray, menu)


def test_tray_autostart_check_waits_for_menu_close(tmp_path):
    """托盘「开机自启」勾选：写系统登录项（失败还要冒泡提示）同样延后执行。"""
    shell, win, tray, menu = _tray_shell(tmp_path)
    calls: list = []
    shell.instance._set_autostart = (
        lambda enabled, target=None: (calls.append((enabled, target)), True)[1])
    action = _entry(menu, "开机自启")
    try:
        _open_menu(menu)

        action.trigger()
        assert action.isChecked() is not None, "勾选态必须即时翻转"
        assert calls == [], "菜单还开着，系统登录项不许内联写"
        assert not menu.isVisible(), "点击后菜单必须先关"

        assert _wait_until(lambda: len(calls) == 1), "菜单关闭后必须写一次登录项"
        assert calls[0][0] is action.isChecked(), "写进去的是用户点出来的勾选态"
        assert calls[0][1] is win, "失败提示的宿主必须是本窗"
    finally:
        _close_tray(tray, menu)


# ------------------------------------------------ overlay 托盘全条目：同一契约
def _overlay_tray(tmp_path):
    """overlay 拓扑的托盘 + 菜单（overlay 下用户**实际看到**的那一份）。

    overlay 不构造 PetWindow，托盘挂在 ``OverlayShell`` 上：先前这批修复只覆盖了
    legacy 的 ``AppShell._build_tray``，本文件的其余用例也都打在 legacy 托盘上——
    于是 overlay 拓扑下的裸 ``addAction`` / ``toggled`` 一直没被回归覆盖。
    """
    from tests.test_overlay_spawn import _make_persistent_shell

    shell, _made = _make_persistent_shell(tmp_path)
    menu = shell.tray.contextMenu()
    assert menu is not None
    return shell, menu


def _close_overlay_tray(shell) -> None:
    """收口：菜单（生灭后可能已重建，故从托盘重取）+ 指令 watcher + 运行标记。"""
    if getattr(shell, "tray", None) is not None:
        menu = shell.tray.contextMenu()
        if menu is not None:
            try:
                menu.close()
            except RuntimeError:
                pass  # 菜单已随生灭重建销毁：跳过
        shell.tray.hide()
    shell._teardown_settings_command_watch()
    shell._delete_runtime_marker()
    app.processEvents()


def test_overlay_tray_show_hide_waits_for_menu_close(tmp_path):
    """overlay 托盘「显示 / 隐藏」：动的是合成窗，同样菜单先关再切。"""
    shell, menu = _overlay_tray(tmp_path)
    try:
        shell.overlay.show()
        app.processEvents()
        assert shell.overlay.isVisible(), "前提：合成窗可见"
        _open_menu(menu)

        _entry(menu, "显示 / 隐藏").trigger()
        assert shell.overlay.isVisible(), "菜单还开着，显隐动作不许内联执行"
        assert not menu.isVisible(), "点击后菜单必须先关"

        assert _wait_until(lambda: not shell.overlay.isVisible()), \
            "菜单关闭后必须切换一次显隐"
    finally:
        _close_overlay_tray(shell)


def test_overlay_tray_return_to_corner_waits_for_menu_close(tmp_path):
    """overlay 托盘「回到右下角」：移动主 sprite，同样不内联执行。"""
    shell, menu = _overlay_tray(tmp_path)
    try:
        expected = shell._default_corner_pos(shell._bounds, shell.sprite.rect())
        shell.sprite.set_pos(QPointF(0, 0))
        before = QPointF(shell.sprite.pos)
        _open_menu(menu)

        _entry(menu, "回到右下角").trigger()
        assert shell.sprite.pos == before, "菜单还开着，归位不许内联执行"
        assert not menu.isVisible(), "点击后菜单必须先关"

        assert _wait_until(lambda: shell.sprite.pos == expected), \
            "菜单关闭后必须归位一次"
    finally:
        _close_overlay_tray(shell)


@pytest.mark.parametrize("label,delta", [
    ("生小肥鱼", +1),
    ("关闭此鱼", -1),
])
def test_overlay_tray_pet_lifecycle_waits_for_menu_close(tmp_path, label, delta):
    """overlay 托盘「生小肥鱼 / 退出子肥鱼」：生灭 sprite 与各自库，同样延迟派发。"""
    shell, menu = _overlay_tray(tmp_path)
    try:
        if delta < 0:
            shell.spawn_pet()                # 前置：先有一只子肥鱼可退
            menu = shell.tray.contextMenu()  # 生灭会重建菜单，重取
        before = len(shell._spawned)
        _open_menu(menu)

        _entry(menu, label).trigger()
        assert len(shell._spawned) == before, f"{label}：菜单还开着不许内联执行"
        assert not menu.isVisible(), "点击后菜单必须先关"

        assert _wait_until(lambda: len(shell._spawned) == before + delta), \
            f"菜单关闭后必须生灭一次（{label}）"
    finally:
        _close_overlay_tray(shell)


def test_overlay_tray_per_pet_submenu_waits_for_menu_close(tmp_path):
    """逐只子菜单三项与顶层同族：子菜单也是同一个原生跟踪会话的一部分。

    「显示这只」是勾选条目：勾选态即时翻转（用户看得到），逐只显隐延到菜单关闭后。
    """
    shell, menu = _overlay_tray(tmp_path)
    try:
        first = None
        shell.spawn_pet()
        shell.spawn_pet()
        first, second = shell._spawned[0], shell._spawned[1]
        menu = shell.tray.contextMenu()
        sub = _submenu(menu, f"小肥鱼 [slot-{shell._spawned_slots[first]}]")
        corner = _entry(sub, "回到右下角")
        show_this = _entry(sub, "显示这只")
        exit_this = _entry(sub, "退出这只")
        assert show_this.isCheckable(), "「显示这只」必须可勾选"
        assert show_this.isChecked() is True, "前提：这只当前可见"
        expected = shell._default_corner_pos(shell._bounds, first.rect())
        first.set_pos(QPointF(0, 0))
        before_pos = QPointF(first.pos)
        shell.sprite.set_pos(QPointF(11, 13))   # 主宠挪开：错路由时位置不会碰巧相等
        main_pos = QPointF(shell.sprite.pos)
        _open_menu(menu)

        show_this.trigger()                 # 取消勾选 = 隐藏这只
        assert show_this.isChecked() is False, "勾选态必须即时翻转"
        assert shell._sprite_visible(first) is True, \
            "菜单还开着，逐只显隐不许内联执行"
        assert _wait_until(lambda: shell._sprite_visible(first) is False), \
            "菜单关闭后必须把这只藏起来"
        assert shell._sprite_visible(shell.sprite) is True, "只影响被点的那只"

        shell.set_sprite_visible(first, True)
        _open_menu(menu)
        corner.trigger()
        assert first.pos == before_pos, "菜单还开着，归位不许内联执行"
        assert _wait_until(lambda: first.pos == expected), "菜单关闭后必须归位"
        assert shell.sprite.pos == main_pos, "主宠不该被逐只动作带走"

        _open_menu(menu)
        exit_this.trigger()
        assert shell._spawned == [first, second], "菜单还开着，退出这只不许内联执行"
        assert _wait_until(lambda: shell._spawned == [second]), \
            "菜单关闭后只该退掉被点的那一只"
        assert first not in shell.overlay.sprites
    finally:
        _close_overlay_tray(shell)


def test_overlay_tray_submenu_entry_waits_for_the_whole_menu_tree(tmp_path):
    """逐只子菜单条目的关闭观测目标是**整棵树**（根菜单 + 子菜单），不是子菜单单点。

    ``add_tray_action(sub, ...)`` 此前只看 ``sub`` 自己：Windows 的 Qt popup 下关掉
    根菜单会连带各层子菜单一起消失，所以"子菜单看不见了"碰巧等价于"整棵树关了"；
    macOS 的托盘菜单是 AppKit 原生 NSMenu 跟踪会话，判定必须落在整棵树上——任一层
    还开着就仍在这个会话里，回调此时执行照样被抑制（issue #4 家族）。

    与其余子菜单用例的区别：这里真把 sub 也 ``show()`` 出来，断言根菜单与子菜单
    都关闭之后回调才执行（回调执行瞬间的菜单开合状态是断言对象，不是等执行完再看）。
    """
    shell, menu = _overlay_tray(tmp_path)
    try:
        shell.spawn_pet()
        menu = shell.tray.contextMenu()          # 生灭会重建菜单，重取
        first = shell._spawned[0]
        sub = _submenu(menu, f"小肥鱼 [slot-{shell._spawned_slots[first]}]")
        corner = _entry(sub, "回到右下角")
        expected = shell._default_corner_pos(shell._bounds, first.rect())
        first.set_pos(QPointF(0, 0))
        shell.sprite.set_pos(QPointF(11, 13))    # 主宠挪开：错路由时位置不会碰巧相等
        main_pos = QPointF(shell.sprite.pos)

        observed: list = []
        real_corner = shell._go_default_corner

        def spy(sprite, *args, **kwargs):
            observed.append((menu.isVisible(), sub.isVisible()))
            return real_corner(sprite, *args, **kwargs)

        shell._go_default_corner = spy

        _open_menu(menu)
        sub.show()
        app.processEvents()
        assert sub.isVisible(), "前提：子菜单在显示态"

        corner.trigger()
        assert not sub.isVisible(), "点击后子菜单必须先关"
        assert not menu.isVisible(), "根菜单也必须一起关（观测目标是整棵树，不是子菜单单点）"

        assert _wait_until(lambda: first.pos == expected), "整棵树关闭后必须归位一次"
        assert observed == [(False, False)], \
            f"回调执行时整棵树必须都已关闭，实际开合状态 {observed}"
        assert shell.sprite.pos == main_pos, "主宠不该被逐只动作带走"
    finally:
        _close_overlay_tray(shell)


def test_overlay_tray_menu_open_syncs_check_marks_without_side_effects(tmp_path):
    """overlay 托盘弹出只同步勾选态看相，绝不顺手执行副作用。

    旧接线（``toggled``）在弹出前那次程序化 ``setChecked`` 也会触发副作用：开一次
    菜单就把老鼠穿透的复合重算与系统登录项重写一遍。副作用改挂 ``triggered`` 后，
    程序化同步不发这个信号——勾选态接线不变，改动只落在"不再误触发"。
    """
    shell, menu = _overlay_tray(tmp_path)
    autostart_calls: list = []
    try:
        shell._instance._set_autostart = (
            lambda enabled, target=None: (autostart_calls.append(enabled), True)[1])
        shell._autostart_enabled = lambda: True
        shell._config.set("mouse_through", True)
        shell.overlay.mouse_through = False     # 前提：窗面尚未应用该意愿
        menu.aboutToShow.emit()
        app.processEvents()

        assert _entry(menu, "鼠标穿透").isChecked() is True, "勾选态跟着配置走"
        assert _entry(menu, "开机自启").isChecked() is True, "勾选态跟着系统登录项走"
        assert shell.overlay.mouse_through is False, "弹菜单不得动穿透开关"
        assert autostart_calls == [], "弹菜单不得写系统登录项"
    finally:
        _close_overlay_tray(shell)


def test_overlay_tray_quit_entry_stays_inline(tmp_path):
    """「退出」刻意不延迟派发：它不是窗面动作，legacy 托盘同为裸 addAction。

    这条把"有意不改"钉住：将来若有人顺手把它也改成延迟派发，本用例会提醒先想清楚
    ——退出发生在原生菜单跟踪会话里不需要 AppKit 放行（会话随进程一起结束）。
    """
    shell, menu = _overlay_tray(tmp_path)
    quit_calls: list = []
    try:
        shell.app = SimpleNamespace(quit=lambda: quit_calls.append(1))
        shell._refresh_tray_menu()
        menu = shell.tray.contextMenu()
        _entry(menu, "退出").trigger()
        assert quit_calls == [1], "「退出」触发即生效（无延迟派发）"
    finally:
        _close_overlay_tray(shell)
