# -*- coding: utf-8 -*-
"""4.2a 位置持久化 + 4.2b 多 sprite 生灭/身份/提升 offscreen 单测。

覆盖：rx/ry 恢复与保存（身体中心比例口径）、无记录落右下角、facing 恢复、
spawn 增 sprite（per-pet 库/行为接管/错开落位）、D1 clip 所有权隔离、clear
全清（clip 释放/库 shutdown/行为注销）、菜单 spawn 入口；4.2b 后段：D6 无锁
身份分配（单调不撞）、D5 活跃宠清单增减、重启按清单复活（含各自位置/朝向/
尺寸）、退出即不复活、清单损坏回退只有主宠、主宠退出提升首只子宠为主。
"""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, QRect
from PySide6.QtWidgets import QApplication

import tests.test_overlay_window_capabilities as cap
import tests.test_sprite_menu_facade as fac
from pet import overlay_spawn_state as ovs
from pet.config import APP_DIR_NAME, Config
from pet.overlay_shell import OverlayShell
from pet.pet_sprite import PetSprite
from pet.sprite_menu_facade import build_sprite_full_menu

app = QApplication.instance() or QApplication([])


def _recursive_labels(container) -> set:
    """递归枚举菜单文本（模板感知后条目可能落在分组子菜单里）。"""
    labels = set()
    for action in container.actions():
        if action.isSeparator():
            continue
        labels.add(action.text())
        if action.menu() is not None:
            labels |= _recursive_labels(action.menu())
    return labels


def _make_persistent_shell(base, values=None):
    """真 Config（config.json 落盘）+ 真 PetSprite 的壳。

    身份/持久化测试必须走真实 ``config-slot-N.json`` 命名与写盘，才能锁住
    "各自 config 身份"语义；返回 (shell, 新建库列表)。
    """
    config = Config(str(base))
    for key, value in (values or {}).items():
        config.set(key, value)
    config.save()
    instance = cap.CapInstance(config)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    main_lib = fac.RichLibrary()
    if shell.sprite is not None:
        shell.lib = main_lib
        shell.sprite.library = main_lib
    made: list = []

    def make_lib():
        lib = fac.RichLibrary()
        made.append(lib)
        return lib

    shell._create_main_library = make_lib
    return shell, made


def _center_ratios(shell, sprite):
    """sprite 身体中心相对可用区比例（与 save_position 同口径）。"""
    body = sprite.body_rect()
    bounds = shell._bounds
    return ((body.x() + body.width() / 2.0 - bounds.left()) / bounds.width(),
            (body.y() + body.height() / 2.0 - bounds.top()) / bounds.height())



def test_restore_position_from_rx_ry(tmp_path):
    shell, _ = fac._make_shell(tmp_path, {"rx": 0.5, "ry": 0.25, "facing": "right"})
    try:
        body = shell.sprite.body_rect()
        cx = body.x() + body.width() / 2.0
        cy = body.y() + body.height() / 2.0
        b = shell._bounds
        assert abs((cx - b.left()) / b.width() - 0.5) < 0.02
        assert abs((cy - b.top()) / b.height() - 0.25) < 0.02
        assert shell.sprite.facing == "right"
    finally:
        shell._delete_runtime_marker()


def test_restore_falls_back_to_corner(tmp_path):
    shell, _ = fac._make_shell(tmp_path)
    try:
        expected = shell._default_corner_pos(shell._bounds, shell.sprite.rect())
        assert shell.sprite.pos == expected
    finally:
        shell._delete_runtime_marker()


def test_save_position_writes_ratios(tmp_path):
    shell, _ = fac._make_shell(tmp_path)
    try:
        shell.sprite.set_pos(QPointF(400, 300))
        shell.sprite.facing = "right"
        shell.save_position()
        body = shell.sprite.body_rect()
        b = shell._bounds
        saved = ovs.read_slot_geometry(shell._config.dir, 0)
        assert abs(saved["rx"]
                   - (body.x() + body.width() / 2.0 - b.left()) / b.width()) < 1e-9
        assert abs(saved["ry"]
                   - (body.y() + body.height() / 2.0 - b.top()) / b.height()) < 1e-9
        assert saved["facing"] == "right"
    finally:
        shell._delete_runtime_marker()


def test_spawn_and_clear_pets(tmp_path):
    shell, main_lib = fac._make_shell(tmp_path)
    try:
        made = []
        shell._create_main_library = lambda: (made.append(1), fac.RichLibrary())[1]
        shell.spawn_pet()
        shell.spawn_pet()
        assert len(shell.overlay.sprites) == 3
        assert len(shell._spawned) == 2
        assert len(made) == 2                       # per-pet 库（T3）
        for s in shell._spawned:
            assert s.library is not main_lib
        # 行为接管：tick 会为 spawn 的 sprite 绑定 idle
        shell.behavior.tick(shell.overlay.sprites, 0.05)
        for s in shell._spawned:
            assert s._clip_name == "idle1"
        libs = [s.library for s in shell._spawned]
        shell.clear_spawned_pets()
        assert shell.overlay.sprites == [shell.sprite]
        assert shell._spawned == []
        assert all(lib.shutdown_calls == 1 for lib in libs)
        # 行为状态已注销（V-9 挂点）
        for s in libs:
            pass
    finally:
        shell._delete_runtime_marker()


def test_spawn_pet_clips_are_per_sprite_owned(tmp_path):
    """D1 clip 所有权守卫：同名 clip 在每个 sprite 上必须是不同对象。

    设计稿 T3/D1：共享 clip = 一速多宠锁步/互相冻结（demo 已修）。产品侧由
    spawn_pet 的 per-pet MovieLibrary 保证——本测试锁定该语义：主 sprite 与
    两个子 sprite 各自 bind 同名 ``idle1`` 后，库对象与 clip 对象都不得同一。
    """
    shell, main_lib = fac._make_shell(tmp_path)
    try:
        made = []
        shell._create_main_library = lambda: (made.append(1), fac.RichLibrary())[1]
        shell.spawn_pet()
        shell.spawn_pet()
        assert len(made) == 2                       # per-pet 库（T3）
        sprites = [shell.sprite, *shell._spawned]
        clips = []
        for sprite in sprites:
            sprite.bind_clip("idle1")
            clip = sprite.library.movie("idle1")
            assert sprite._clip is clip             # bind 取自本 sprite 的库
            clips.append(clip)
        # 同角色的同名 clip 不允许跨 sprite 共享同一播放器对象
        assert len({id(c) for c in clips}) == len(sprites)
        assert all(s.library is not main_lib for s in shell._spawned)
    finally:
        shell._delete_runtime_marker()


def test_menu_has_spawn_entries(tmp_path):
    shell, _ = fac._make_shell(tmp_path)
    try:
        menu = build_sprite_full_menu(shell)
        labels = _recursive_labels(menu)
        assert "生小肥鱼" in labels
        assert "关闭此鱼" in labels
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 4.2b 身份/活跃清单
def test_spawn_allocates_monotonic_identity_and_persists_list(tmp_path):
    """D6：spawn 身份单调不撞；D5：spawn 进活跃清单、退出出清单。"""
    config_dir = tmp_path / APP_DIR_NAME
    shell, made = _make_persistent_shell(tmp_path)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        assert [shell._spawned_slots[s] for s in shell._spawned] == [1, 2]
        assert len(made) == 2                                  # per-pet 库（T3）
        assert ovs.load_active_slots(config_dir) == [0, 1, 2]
        assert (config_dir / "config-slot-1.json").is_file()
        assert (config_dir / "config-slot-2.json").is_file()
        shell.clear_spawned_pets()
        assert ovs.load_active_slots(config_dir) == [0]
        shell.spawn_pet()
        assert shell._spawned_slots[shell._spawned[0]] == 1
        assert ovs.load_active_slots(config_dir) == [0, 1]
    finally:
        shell._delete_runtime_marker()


def test_spawn_skips_legacy_slot_config_identities(tmp_path):
    """旧版多进程时代遗留的 slot 配置身份不被复用（身份记忆不覆盖）。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config-slot-1.json").write_text(
        json.dumps({"version": 4, "rx": 0.11, "ry": 0.22}), encoding="utf-8")
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        shell.spawn_pet()
        assert shell._spawned_slots[shell._spawned[0]] == 1
        legacy = json.loads(
            (config_dir / "config-slot-1.json").read_text(encoding="utf-8"))
        assert legacy["rx"] == 0.11 and legacy["ry"] == 0.22
    finally:
        shell._delete_runtime_marker()


def test_spawn_reuses_retired_identity_when_history_is_full(tmp_path, caplog):
    """B01：128 个历史身份占满目录时仍能生（复用最小退役身份，记 warning）。

    普通用户反复生/退累计 128 次后目录里全是历史 ``config-slot-N.json``；旧实现
    直接抛 ``SpawnStateError``，UI 也只写日志 → 「生小肥鱼」永久静默失效。
    """
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    for slot in range(1, ovs.MAX_SCAN_SLOTS + 1):
        (config_dir / f"config-slot-{slot}.json").write_text(
            json.dumps({"version": 4}), encoding="utf-8")
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="pet.overlay_spawn_state"):
            shell.spawn_pet()
        assert [shell._spawned_slots[s] for s in shell._spawned] == [1]
        assert ovs.load_active_slots(config_dir) == [0, 1]
        assert not any("覆盖" in record.getMessage() for record in caplog.records)
    finally:
        shell._delete_runtime_marker()


def test_spawn_exhausted_identities_reports_bubble(tmp_path, monkeypatch):
    """B01：真耗尽（128 个身份全活跃）时不许再静默——失败出口必须冒泡告知。"""
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        monkeypatch.setattr(
            ovs, "scan_slot_configs",
            lambda *_a, **_k: set(range(1, ovs.MAX_SCAN_SLOTS + 1)))
        shell._active_slots = lambda: list(range(0, ovs.MAX_SCAN_SLOTS + 1))
        said: list[str] = []
        shell.show_bubble = lambda text, **kwargs: said.append(str(text)) or True
        shell.spawn_pet()
        assert shell._spawned == []                       # 不留半只 sprite
        assert shell.overlay.sprites == [shell.sprite]
        assert len(said) == 1, "耗尽路径必须走壳上气泡通道告知用户"
        assert "生小肥鱼失败" in said[0] and "128" in said[0]
    finally:
        shell._delete_runtime_marker()


def test_clear_spawned_pets_does_not_revive_after_restart(tmp_path):
    """退出子肥鱼 = 出清单：配置保留但重启不再出现（D5 铁律）。"""
    shell, _ = _make_persistent_shell(tmp_path)
    shell.spawn_pet()
    shell.spawn_pet()
    shell.clear_spawned_pets()
    assert (tmp_path / APP_DIR_NAME / "config-slot-1.json").is_file()  # 配置保留
    shell2, _ = _make_persistent_shell(tmp_path)
    try:
        assert shell2._spawned == []
        assert shell2.overlay.sprites == [shell2.sprite]
    finally:
        shell2._delete_runtime_marker()
        shell._delete_runtime_marker()


def test_exit_single_spawned_pet_removes_only_that_slot(tmp_path):
    config_dir = tmp_path / APP_DIR_NAME
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        first, second = shell._spawned
        assert shell.exit_pet(first) is True
        assert shell._spawned == [second]
        assert first not in shell.overlay.sprites
        assert ovs.load_active_slots(config_dir) == [0, 2]
        assert shell.exit_pet(first) is False            # 已退出 → no-op
        shell2, _ = _make_persistent_shell(tmp_path)
        try:
            assert [shell2._spawned_slots[s] for s in shell2._spawned] == [2]
        finally:
            shell2._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 4.2b 重启复活
def test_restart_revives_active_pets_with_their_own_positions(tmp_path):
    shell, _ = _make_persistent_shell(tmp_path)
    shell.spawn_pet()
    shell.spawn_pet()
    first, second = shell._spawned
    first.set_pos(QPointF(120, 140))
    second.set_pos(QPointF(760, 420))
    first.facing = "right"
    want_first = _center_ratios(shell, first)
    want_second = _center_ratios(shell, second)
    shell.save_spawned_positions()  # 退出收口路径（aboutToQuit）

    shell2, _ = _make_persistent_shell(tmp_path)
    try:
        assert [shell2._spawned_slots[s] for s in shell2._spawned] == [1, 2]
        got_first = _center_ratios(shell2, shell2._spawned[0])
        got_second = _center_ratios(shell2, shell2._spawned[1])
        assert abs(got_first[0] - want_first[0]) < 0.005
        assert abs(got_first[1] - want_first[1]) < 0.005
        assert abs(got_second[0] - want_second[0]) < 0.005
        assert abs(got_second[1] - want_second[1]) < 0.005
        assert shell2._spawned[0].facing == "right"
        assert shell2._spawned[0].scale == pytest.approx(first.scale)
    finally:
        shell2._delete_runtime_marker()
        shell._delete_runtime_marker()


def test_revive_uses_each_slot_scale(tmp_path):
    """每只子肥鱼按各自 slot 身份恢复尺寸（不是跟随主宠当前尺寸）。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    ovs.save_active_slots(config_dir, [1, 2])
    (config_dir / "config-slot-1.json").write_text(
        json.dumps({"version": 4, "rx": 0.3, "ry": 0.3, "scale": 0.5}),
        encoding="utf-8")
    (config_dir / "config-slot-2.json").write_text(
        json.dumps({"version": 4, "rx": 0.7, "ry": 0.7, "scale": 0.25}),
        encoding="utf-8")
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        assert [s.scale for s in shell._spawned] == [pytest.approx(0.5),
                                                     pytest.approx(0.25)]
    finally:
        shell._delete_runtime_marker()


def test_spawn_persists_each_pet_position_separately(tmp_path):
    config_dir = tmp_path / APP_DIR_NAME
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        first, second = shell._spawned
        first.set_pos(QPointF(150, 130))
        second.set_pos(QPointF(800, 500))
        shell.save_spawned_positions()
        slot1 = ovs.read_slot_geometry(config_dir, 1)
        slot2 = ovs.read_slot_geometry(config_dir, 2)
        assert abs(slot1["rx"] - _center_ratios(shell, first)[0]) < 1e-9
        assert abs(slot2["rx"] - _center_ratios(shell, second)[0]) < 1e-9
        assert slot1["rx"] != slot2["rx"]
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 4.2b 清单损坏回退
def test_corrupt_active_list_revives_only_main(tmp_path):
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config-slot-1.json").write_text(
        json.dumps({"version": 4, "rx": 0.4, "ry": 0.4, "scale": 0.72}),
        encoding="utf-8")
    (config_dir / "overlay-active-pets.json").write_text("{ broken", encoding="utf-8")
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        assert shell._spawned == []
    finally:
        shell._delete_runtime_marker()


def test_active_entry_without_slot_config_is_dropped(tmp_path):
    """清单里指向已消失身份的条目：跳过复活并摘除（不让坏条目永久留下）。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    ovs.save_active_slots(config_dir, [1, 2])
    (config_dir / "config-slot-1.json").write_text(
        json.dumps({"version": 4, "rx": 0.4, "ry": 0.4, "scale": 0.72}),
        encoding="utf-8")
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        assert [shell._spawned_slots[s] for s in shell._spawned] == [1, 2]
        assert ovs.load_active_slots(config_dir) == [1, 2]
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 4.2b 主实例提升
def test_main_exit_promotes_first_spawned_pet(tmp_path):
    """主宠退出 → 活跃清单列表头提升为主（app.py P1-3 等价语义）。"""
    config_dir = tmp_path / APP_DIR_NAME
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        first, second = shell._spawned
        first.set_pos(QPointF(200, 160))
        want = _center_ratios(shell, first)
        old_main, old_lib = shell.sprite, shell.lib
        assert shell.exit_pet() is True
        assert shell.sprite is None
        assert shell.lib is None
        assert shell._spawned == [first, second]
        assert list(shell._spawned_slots.values()) == [1, 2]
        assert old_main not in shell.overlay.sprites
        assert old_lib.shutdown_calls == 1
        assert ovs.load_active_slots(config_dir) == [1, 2]
        assert _center_ratios(shell, first) == want
    finally:
        shell._delete_runtime_marker()


def test_promoted_pet_takes_over_main_identity_after_restart(tmp_path):
    shell, _ = _make_persistent_shell(tmp_path)
    shell.spawn_pet()
    promoted = shell._spawned[0]
    promoted.set_pos(QPointF(300, 220))
    want = _center_ratios(shell, promoted)
    shell.save_spawned_positions()
    assert shell.exit_pet() is True

    shell2, _ = _make_persistent_shell(tmp_path)
    try:
        assert shell2.sprite is None
        assert len(shell2._spawned) == 1
        assert shell2._spawned_slots[shell2._spawned[0]] == 1
        got = _center_ratios(shell2, shell2._spawned[0])
        assert abs(got[0] - want[0]) < 0.005
        assert abs(got[1] - want[1]) < 0.005
    finally:
        shell2._delete_runtime_marker()
        shell._delete_runtime_marker()


def test_main_exit_without_spawned_pets_quits_app(tmp_path):
    shell, _ = _make_persistent_shell(tmp_path)
    quits: list = []
    shell.app = SimpleNamespace(quit=lambda: quits.append(1))
    assert shell.exit_pet() is True
    assert quits == []
    assert shell.sprite is None
    assert ovs.load_active_slots(tmp_path / APP_DIR_NAME) == []


# ---------------------------------------------------------------- 新 sprite 的播放节拍初始化
def test_spawn_while_window_hidden_starts_paused(tmp_path):
    """隐藏期生成子宠：新 sprite 的播放节拍必须立刻压住。

    ``PetSprite.__init__`` 默认未暂停，spawn 后随行为链一起起播就按帧率解码
    ——而窗口根本没有像素要上屏（O3 契约）。``visible`` 照旧为真：它逻辑上
    仍是可见宠物，只是窗口不可见。
    """
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        assert shell.overlay.isVisible() is False
        shell.spawn_pet()

        spawned = shell._spawned[0]
        assert spawned.visible is True
        assert spawned.clip_paused is True
    finally:
        shell._delete_runtime_marker()


def test_spawn_while_window_shown_keeps_playback_pace(tmp_path):
    """显示期生成子宠不受影响：新 sprite 照常按播放节拍走（防过度收紧）。"""
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.spawn_pet()

        assert shell._spawned[0].clip_paused is False
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_hidden_spawned_pet_resumes_when_window_shows(tmp_path):
    """隐藏期压住的播放节拍在窗口恢复显示时补放行（否则那只宠永久冻在首帧）。"""
    shell, _ = _make_persistent_shell(tmp_path)
    try:
        shell.spawn_pet()
        spawned = shell._spawned[0]
        assert spawned.clip_paused is True

        shell.set_pet_visible(True)                       # 产品显示路径

        assert spawned.clip_paused is False
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_revived_pets_resume_playback_pace_when_shell_starts(tmp_path):
    """启动复活（窗口尚未 show）→ start() 显示后必须补放行。

    复活发生在构造期，那时窗口还没显示，新 sprite 一律被压成暂停；没有
    ``start()`` 的对称收口，启动复活的子宠会永久冻在首帧。
    """
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    ovs.save_active_slots(config_dir, [1])
    (config_dir / "config-slot-1.json").write_text(
        json.dumps({"version": 4, "rx": 0.3, "ry": 0.3, "scale": 0.5}),
        encoding="utf-8")

    shell, _ = _make_persistent_shell(tmp_path)
    try:
        revived = shell._spawned[0]
        assert revived.clip_paused is True, "窗口未显示：复活即压住播放节拍"

        shell.start()                                     # 显示（产品启动路径）

        assert shell.overlay.isVisible() is True
        assert revived.clip_paused is False
    finally:
        shell.stop()
        shell.overlay.close()
        shell._delete_runtime_marker()

