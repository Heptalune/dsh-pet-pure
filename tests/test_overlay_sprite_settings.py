# -*- coding: utf-8 -*-
"""config → sprite/behavior 同步点回归（DS 全量审查 M6/M7/M9/M10）。

M6：drag_physics/throw_strength/no_move/playback_speed 此前启动不读 config、
refresh 不覆盖、sprite 默认值与 config 默认相反（drag_physics=True vs False、
throw_speed_cap=6000 vs standard=4800）——开箱行为不一致 + 重启即丢。
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

import tests.test_overlay_window_capabilities as cap
import tests.test_sprite_menu_facade as fac
from pet.overlay_shell import OverlayShell
from pet.pet_sprite import PetSprite
from pet.physics import THROW_STRENGTH_CAPS

app = QApplication.instance() or QApplication([])


def _make_shell(tmp_path, values=None):
    config = cap.CapConfig(tmp_path, values or {})
    instance = cap.CapInstance(config)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    lib = fac.RichLibrary()
    shell.lib = lib
    shell.sprite.library = lib
    return shell, config


def test_config_syncs_to_sprite_at_build(tmp_path):
    """启动即按 config 应用：默认值不再相反，四键全部生效。"""
    shell, _config = _make_shell(tmp_path, {
        "drag_physics": False,
        "throw_strength": "gentle",
        "no_move": True,
        "playback_speed": 1.5,
    })
    try:
        sprite = shell.sprite
        assert sprite.drag_physics is False            # 旧默认 True，与 config 相反（M6）
        assert sprite.throw_speed_cap == THROW_STRENGTH_CAPS["gentle"]
        assert sprite.playback_speed == 1.5
        assert shell.behavior.no_move is True
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_refresh_settings_resyncs_sprite(tmp_path):
    """设置页改完经 refresh_settings 即生效（不重启）。"""
    shell, config = _make_shell(tmp_path, {"drag_physics": False})
    try:
        config.set("drag_physics", True)
        config.set("throw_strength", "crazy")
        config.set("playback_speed", 0.5)
        shell.refresh_settings()
        assert shell.sprite.drag_physics is True
        assert shell.sprite.throw_speed_cap == THROW_STRENGTH_CAPS["crazy"]
        assert shell.sprite.playback_speed == 0.5
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_spawned_sprite_gets_config_sync(tmp_path):
    """spawn 的子肥鱼同样按 config 应用（不是只吃构造默认）。"""
    shell, _config = _make_shell(tmp_path, {"drag_physics": False,
                                            "throw_strength": "strong"})
    try:
        shell._spawn_slot(7)
        spawned = shell._spawned[-1]
        assert spawned.drag_physics is False
        assert spawned.throw_speed_cap == THROW_STRENGTH_CAPS["strong"]
        lib = shell._spawned_libs.pop(spawned)
        shutdown = getattr(lib, "shutdown", None)
        if callable(shutdown):
            shutdown()
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_stop_halts_self_talk_timer(tmp_path):
    """M9：shell.stop() 后 self_talk 定时器不再自我重排。"""
    shell, _config = _make_shell(tmp_path, {
        "self_talk_enabled": True,
        "self_talk_texts": ["台词"],
        "self_talk_min_interval": 5.0,
        "self_talk_max_interval": 5.0,
    })
    try:
        shell.overlay.show()
        assert shell._self_talk_timer.isActive() is True
        shell.start()
        shell.stop()
        assert shell._self_talk_timer.isActive() is False
        shell._delete_runtime_marker()
    finally:
        timer = getattr(shell, "_self_talk_timer", None)
        if timer is not None:
            timer.stop()
        shell._delete_runtime_marker()


def test_settle_supported_resets_flight_playback_speed():
    """M7：落岛支撑落定与抛掷落地共用速率复位语义（duration 失真源）。

    真世界最小落定场景：thrown sprite 低速贴岛，连 tick 到支撑落定阈值
    → 收尾必须调 reset_playback_speed（否则飞行倍率除进 duration()，
    后续 _plan_move 量化失配——sprite_physics 落地分支不是唯一出口）。
    """
    from pet.sprite_collision import SpriteCollisionWorld
    from tests.test_island_bridge import FakeSprite

    world = SpriteCollisionWorld()
    world.add_static_member("island", 300.0, 140.0, 200.0, 44.0)
    sprite = FakeSprite(370.0, 100.0)          # 60×60 底部 160 贴岛顶 140（内切圆相交）
    sprite.interaction_state = "thrown"
    sprite.set_velocity(type(sprite.velocity)(0.0, 0.0))
    resets: list = []
    sprite.reset_playback_speed = lambda: resets.append(1)

    for _ in range(world.support_settle_ticks):
        world.tick([sprite], 1 / 60)

    assert sprite.interaction_state == "normal"
    assert resets, "支撑落定收尾未复位飞行播放速率（M7）"


def test_island_hit_below_300_still_squashes(tmp_path):
    """GPT 审查阻断：岛撞 60-300 是合法真撞击（静态阈值 60），壳层不得再按
    hit_min_dv=300 二次过滤——低速撞岛的挤压/反馈链不再被截断。"""
    import types

    shell, _config = _make_shell(tmp_path, {})
    try:
        sprite = shell.sprite
        squashed: list = []
        sprite.squash = lambda: squashed.append(1)
        cid = sprite.collision_id
        event = types.SimpleNamespace(a="island", b=cid, j=100.0)  # 60<100<300
        shell._on_collision_squash(event)
        assert squashed == [1]
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- B7a：碰撞物理 4 键
def test_collision_physics_keys_reach_world(tmp_path):
    """设置页「碰撞参数（高级）」4 键 → 世界求解参数（此前 overlay 零消费）。

    旧架构每只宠各读自己 cfg 并逐次求解时使用（collision_client.py:140-145 /
    :558-563）；新版此前只写 ``SpriteCollisionWorld()`` 默认值，改了什么都不会变。
    """
    shell, config = _make_shell(tmp_path, {})
    try:
        world = shell.collision
        config.set("collision_restitution", 0.5)
        config.set("collision_friction", 0.2)
        config.set("collision_mass_scale", 1.5)
        config.set("collision_impulse_cap", 4000.0)
        shell.refresh_settings()
        assert world.restitution == pytest.approx(0.5)
        assert world.friction == pytest.approx(0.2)
        assert world.mass_scale == pytest.approx(1.5)
        assert world.impulse_cap == pytest.approx(4000.0)
    finally:
        shell._delete_runtime_marker()


def test_collision_physics_keys_reach_solver(tmp_path, monkeypatch):
    """4 键必须在运行期真的进求解器入参（不只是写在世界属性上）。"""
    import tests.test_overlay_collision_toggle as toggle
    from pet import collision as collision_mod

    shell, _cfg = toggle._make_slot_shell(tmp_path, collision_enabled=True)
    seen: dict = {}
    real_solve = collision_mod.solve_multi_body_collision

    def spy(members, **kwargs):
        seen.update(kwargs)
        return real_solve(members, **kwargs)

    monkeypatch.setattr(collision_mod, "solve_multi_body_collision", spy)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        sprites = list(shell.overlay.sprites)
        shell._config.set("collision_restitution", 0.5)
        shell._config.set("collision_friction", 0.2)
        shell._config.set("collision_mass_scale", 1.5)
        shell._config.set("collision_impulse_cap", 4000.0)
        shell.refresh_settings()
        toggle._setup(shell.collision, sprites, vx=500.0)
        shell.collision.tick(list(shell.overlay.sprites), 0.016)
        assert seen, "前提：真 tick 走到了多体求解"
        assert seen["restitution"] == pytest.approx(0.5)
        assert seen["friction"] == pytest.approx(0.2)
        assert seen["impulse_cap"] == pytest.approx(4000.0)
        assert shell.collision.mass_scale == pytest.approx(1.5)
    finally:
        monkeypatch.undo()
        toggle._teardown(shell)


def _collision_relative_speed(tmp_path, restitution: float) -> float:
    """单案例现场：真壳 + 迎面真撞一次 → 撞后相对速度（每案例独立壳/世界）。

    必须一案一壳：世界的扫掠快照/重叠去抖是跨 tick 的，同一实例连撞两次时
    第二拍的冲量口径与首拍不同（实测），会把「恢复系数有没有生效」这件事测糊。
    """
    import tests.test_overlay_collision_toggle as toggle

    case_dir = tmp_path / f"rest{restitution}"
    case_dir.mkdir(parents=True, exist_ok=True)
    shell, cfg = toggle._make_slot_shell(case_dir, collision_enabled=True)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        sprites = list(shell.overlay.sprites)
        cfg.set("collision_restitution", restitution)
        cfg.set("collision_mass_scale", 1.0)
        cfg.set("collision_impulse_cap", 12000.0)
        shell.refresh_settings()
        toggle._setup(shell.collision, sprites, vx=500.0)
        shell.collision.tick(list(shell.overlay.sprites), 0.016)
        assert sprites[0].interaction_state == "thrown", "前提：真撞发生了"
        return abs(sprites[0].velocity.x() - sprites[1].velocity.x())
    finally:
        toggle._teardown(shell)


def test_collision_restitution_changes_real_solve(tmp_path):
    """端到端：恢复系数真的改变弹开速度（0.95 明显比 0.2 弹得开）。"""
    slow = _collision_relative_speed(tmp_path, 0.2)
    fast = _collision_relative_speed(tmp_path, 0.95)
    assert fast > slow + 100.0, f"恢复系数未影响求解：0.2→{slow}, 0.95→{fast}"



# ---------------------------------------------------------------- B7b：per-slot 生效
def test_scale_applies_at_runtime(tmp_path):
    """设置页改大小 → refresh_settings 即生效；子宠按**它自己**的 slot 配置取源。

    B7b 改写：原断言锁"子宠 scale 本批不动（归 B7b）"——那是 B7a 的过渡口径。
    新契约 = 旧版"每只宠各读自己那份 config"：子宠的缩放/速率由它自己的
    ``config-slot-N.json`` 决定，主宠的设置不再拖走子宠，反之亦然。
    """
    import tests.test_overlay_collision_toggle as toggle
    from pet import overlay_spawn_state as ovs

    shell, cfg = toggle._make_slot_shell(tmp_path, scale=0.5)
    try:
        assert shell.sprite.scale == pytest.approx(0.5)
        # 主宠的"大小"同样是每宠键：真值在 ``config-slot-0.json``（生产里设置页经
        # SlotRoutedConfig 写它；裸 ``Config.set`` 只改全局那份，全局保存会把每宠键
        # 过滤掉，改不动任何一只宠）。
        assert ovs.write_slot_setting(cfg.dir, 0, "scale", 1.0,
                                      user_customized=True)
        shell.refresh_settings()
        assert shell.sprite.scale == pytest.approx(1.0)

        shell.spawn_pet()
        child = shell._spawned[0]
        # 这只子肥鱼自己的设置页保存了 0.5 → 只有它按 0.5 走
        assert ovs.write_slot_setting(cfg.dir, 1, "scale", 0.5,
                                      user_customized=True)
        shell._on_settings_command_dir_changed(str(cfg.dir))
        assert child.scale == pytest.approx(0.5)
        assert shell.sprite.scale == pytest.approx(1.0)

        # slot0 档案再改：主宠跟着变，子宠按自己那份不变
        assert ovs.write_slot_setting(cfg.dir, 0, "scale", 0.72,
                                      user_customized=True)
        shell.refresh_settings()
        assert shell.sprite.scale == pytest.approx(0.72)
        assert child.scale == pytest.approx(0.5)
    finally:
        toggle._teardown(shell)
