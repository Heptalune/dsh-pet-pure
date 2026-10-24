# -*- coding: utf-8 -*-
"""单宠（只活跃 slot0）的「关闭这只」用户入口：托盘逐只菜单恒在 + 关宠不退应用。

背景（实测发现，``.scratch/product-close-approved-gui-20261011/REPORT.md`` §5）：
``OverlayShell._refresh_tray_menu`` 只在 ``self._spawned`` 非空（≥2 只）时才建逐只
子菜单，于是**单宠时托盘根本没有"关闭这只"入口**；而规范 §一/§四 要求"关闭宠物 =
该槽留空、档案与位置保留、托盘保留、退出应用另算"。用户在单宠下想关掉桌宠只有
"退出"（= 退出整个应用）一条路——关闭与退出在 UI 上被合并了。

本批只补托盘这一条**用户入口**（不动右键菜单、不动"退出"的语义）：
逐只菜单随"至少 1 只"出现，关最后一只只留空、不退出进程，0 只时恢复入口仍在。

时序纪律：菜单条目按既有契约「菜单先关、回调延后」派发，测试先证明没有内联执行，
再用宽预算轮询等它落地；不赌固定 sleep。
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from pet import pet_profiles as profiles
from pet.config import APP_DIR_NAME
from tests.test_overlay_spawn import _center_ratios, _make_persistent_shell

app = QApplication.instance() or QApplication([])

MAIN_LABEL = "主肥鱼 [slot-0]"


def _tray_menu(shell):
    assert shell.tray is not None, "测试环境应能建出托盘对象"
    menu = shell.tray.contextMenu()
    assert menu is not None
    return menu


def _labels(menu) -> list[str]:
    return [a.text() for a in menu.actions()]


def _submenus(menu) -> dict:
    return {a.text(): a.menu() for a in menu.actions() if a.menu() is not None}


def _action(container, label: str):
    for act in container.actions():
        if act.text() == label:
            return act
    raise AssertionError(
        f"菜单缺项 {label}；实际: {[a.text() for a in container.actions()]}")


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _teardown(shell) -> None:
    shell._teardown_settings_command_watch()
    shell._delete_runtime_marker()
    if getattr(shell, "tray", None) is not None:
        shell.tray.hide()


# ================================================================ 入口存在
def test_single_pet_tray_exposes_close_entry_and_keeps_quit_separate(tmp_path):
    """单宠托盘：逐只菜单在，含「退出这只」；顶层「退出」语义不动（仍是退出应用）。

    同时钉住这条入口的 tooltip 说清"档案保留 + 怎么恢复"——否则用户点它之后
    唯一的疑问就是"我的设置还在吗、怎么弄回来"。
    """
    shell, _made = _make_persistent_shell(tmp_path)
    try:
        menu = _tray_menu(shell)
        subs = _submenus(menu)
        assert MAIN_LABEL in subs, f"单宠也必须有逐只菜单；实际: {_labels(menu)}"
        sub = subs[MAIN_LABEL]
        assert {"回到右下角", "退出这只"} <= set(_labels(sub)), _labels(sub)
        assert "显示这只" not in _labels(sub), (
            "只剩一只时逐只显隐没有意义（顶层「显示 / 隐藏」已是同一件事）")

        assert "退出" in _labels(menu), "退出应用是独立入口，不许被关闭入口顶掉"
        assert menu.toolTipsVisible() is True, "tooltip 要真的显示得出来"

        close = _action(sub, "退出这只")
        tip = close.toolTip()
        assert tip, "关闭入口必须带 tooltip"
        assert "档案" in tip, f"tooltip 要说明档案保留：{tip}"
        assert "恢复" in tip, f"tooltip 要指出恢复方式：{tip}"

        quit_action = _action(menu, "退出")
        assert quit_action.toolTip() != tip, "关闭与退出是两个动作，不许共用文案"
    finally:
        _teardown(shell)


# ================================================================ 闭环：关 → 空 → 恢复 → 退出
def test_single_pet_close_then_restore_then_quit(tmp_path, monkeypatch):
    """真 QAction 触发走完整闭环：关这只 → 0 只但进程/托盘还在 → 恢复 slot0 保档
    案与位置 → 「退出」才是真的退出应用。

    这条是本批的验收主链：全程从**托盘菜单动作**进入（不是直接 call exit_pet），
    路由经 ``_exit_pet_from_tray`` → ``SpriteMenuFacade.on_exit_pet`` →
    ``OverlayShell.exit_pet``（scope 交界），并顺带守住既有"菜单先关、回调延后"
    的派发契约。
    """
    directory = tmp_path / APP_DIR_NAME
    profiles.write_profile_settings(directory, 0, {"character": "nova",
                                                   "rx": 0.30, "ry": 0.40})
    shell, _made = _make_persistent_shell(tmp_path)
    quits: list = []
    monkeypatch.setattr(shell.app, "quit", lambda *a, **k: quits.append(1))
    try:
        assert shell.sprite is not None, "前提：单宠（slot0）在"
        shell.overlay.show()
        shell.sprite.set_pos(QPointF(600, 500))
        before = _center_ratios(shell, shell.sprite)

        # ---- 关闭这只（真 QAction 触发）
        _action(_submenus(_tray_menu(shell))[MAIN_LABEL], "退出这只").trigger()
        assert shell.sprite is not None, "菜单还开着，关闭动作不许内联执行"
        assert quits == [], "关闭这只不是退出应用"
        assert _wait_until(lambda: shell.sprite is None), "菜单关闭后必须真的关掉它"

        # ---- 0 只：进程/托盘活着，活跃集合留空，没留下任何指向 None 的条目
        assert quits == [], "关闭最后一只不许退出应用（规范 §一/§四）"
        assert shell._active_slots() == []
        assert profiles.load_active_set(directory).slots == ()
        assert shell.overlay.sprites == []
        assert shell.tray is not None, "托盘必须保留（无宠时它是唯一恢复入口）"
        menu = _tray_menu(shell)
        assert _submenus(menu) == {}, "0 只时不许再挂逐只菜单"
        assert not [text for text in _labels(menu) if "None" in text], _labels(menu)
        assert "生成 / 恢复 slot0" in _labels(menu), _labels(menu)

        # ---- 恢复：档案（角色）与位置都回来
        _action(menu, "生成 / 恢复 slot0").trigger()
        assert shell.sprite is None, "菜单还开着，恢复动作不许内联执行"
        assert _wait_until(lambda: shell.sprite is not None), "菜单关闭后必须恢复 slot0"
        assert shell._active_slots() == [0]
        assert shell._sprite_config(shell.sprite).get("character") == "nova"
        restored = _center_ratios(shell, shell.sprite)
        assert abs(restored[0] - before[0]) < 0.02 and abs(restored[1] - before[1]) < 0.02, (
            f"位置必须保留：关闭前 {before} → 恢复后 {restored}")

        # 逐只菜单随恢复重建（关闭入口又回来了）
        assert MAIN_LABEL in _submenus(_tray_menu(shell))

        # ---- 退出应用：独立入口，触发即真的 quit（既有语义不变）
        _action(_tray_menu(shell), "退出").trigger()
        assert quits == [1]
        assert shell.sprite is not None, "退出应用不是「关闭这只」，不替用户关宠"
    finally:
        _teardown(shell)


def test_single_pet_close_entry_tooltip_differs_from_child_copy(tmp_path):
    """逐只 tooltip 按"这只"的身份给恢复方式：slot0 走「生成 / 恢复 slot0」，
    子宠走「生小肥鱼」（子宠是靠自己的槽档案回来的，不冒充 slot0 那条路）。"""
    shell, _made = _make_persistent_shell(tmp_path)
    try:
        main_tip = _action(
            _submenus(_tray_menu(shell))[MAIN_LABEL], "退出这只").toolTip()
        shell.spawn_pet()
        child = shell._spawned[0]
        sub = _submenus(_tray_menu(shell))[f"小肥鱼 [slot-{shell._spawned_slots[child]}]"]
        child_tip = _action(sub, "退出这只").toolTip()
        assert main_tip and child_tip and main_tip != child_tip
        assert "生成 / 恢复 slot0" in main_tip
        assert "生小肥鱼" in child_tip
    finally:
        _teardown(shell)
