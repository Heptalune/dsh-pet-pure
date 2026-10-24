# -*- coding: utf-8 -*-
"""G4 宠物间总碰撞开关：settings → OverlayShell → SpriteCollisionWorld（offscreen）。

分层纪律：每 pair 的过滤语义在 ``tests/test_sprite_collision.py``（纯逻辑）；
本文件只验真实接线一层——真 ``OverlayShell`` + 真 ``SpriteCollisionWorld`` +
真 ``PetSprite``（含真实 ``spawn_pet`` 生出的两只子宠），断言「现成 settings 值经
``refresh_settings`` 落到每只 sprite 的资格位，并改变世界的实际结算行为」。

岛（静态成员）与宠物开关彼此独立：岛-宠碰撞由 ``dynamic_island.collision_enabled``
（壳侧挂/摘岛桥）单独负责，本开关只过滤宠-宠 pair。

纪律：同步直调 tick / refresh_settings，不 sleep、不起真实 QTimer 赌时序、不写
真实配置目录（config.dir 全部落在 tmp_path）。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

import tests.test_sprite_menu_facade as fac
from pet import collision as collision_mod
from pet.sprite_collision import SpriteCollisionWorld

app = QApplication.instance() or QApplication([])

# 真 PetSprite 的矩形不小（整画布），摆放重叠量按 rect().width() 实测取一个常量
OVERLAP = 20.0


def _spawn_three_pets(tmp_path):
    """真壳 + 主宠 + 两只子宠（**真实落盘 Config**：子宠身份配置可被 per-slot 读点读到）。

    子宠 spawn 时按旧语义从主配置落种 → 各自 ``config-slot-N.json`` 自带当时那份
    ``collision_enabled``；此后每只按自己的文件走（本文件后面的 per-slot 段专门测）。
    """
    shell, _cfg = _make_slot_shell(tmp_path, collision_enabled=True)
    shell.spawn_pet()
    shell.spawn_pet()
    sprites = list(shell.overlay.sprites)
    assert len(sprites) == 3
    return shell, sprites


def _park(sprite, x):
    sprite.set_pos(QPointF(x, 0.0))
    sprite.set_velocity(QPointF(0.0, 0.0))
    sprite.interaction_state = "normal"


def _setup(world, sprites, *, vx=500.0):
    """a/b 水平重叠 OVERLAP、迎面速度 ±vx；c 停在远处互不接触。

    位置是测试直接摆放的瞬移，故清空帧间扫掠快照（``_prev_circles``）——否则
    把"瞬移跨过对方"当成高速掠过（该语义另在纯逻辑层覆盖）。**注意**：因此本
    文件这条用例不构成"无幽灵残留"的证据；那条结论由
    ``test_pet_collision_toggle_hot_switch_across_ticks`` 承担（全程不清私有簿记）。
    """
    a, b, c = sprites
    a.set_pos(QPointF(0.0, 0.0))
    a.set_velocity(QPointF(vx, 0.0))
    a.interaction_state = "normal"
    b.set_pos(QPointF(float(a.rect().width()) - OVERLAP, 0.0))
    b.set_velocity(QPointF(-vx, 0.0))
    b.interaction_state = "normal"
    _park(c, 3000.0)
    world._prev_circles = {}


def _teardown(shell):
    timer = getattr(shell, "_self_talk_timer", None)
    if timer is not None:
        timer.stop()
    shell._teardown_settings_command_watch()
    overlay = getattr(shell, "overlay", None)
    if overlay is not None:
        overlay.close()
    # 排空延迟音效回调：sprite_sound._submit 用 QTimer.singleShot(0, ...) 把播放
    # 推迟到下一轮事件循环，不排空会漏进下一条用例的 play_sound 打桩里（音量 0.7
    # 的碰撞音被记成别人的点击音）。调两次：回调执行本身也可能再投递事件。
    QApplication.processEvents()
    QApplication.processEvents()


def test_refresh_settings_applies_main_collision_switch(tmp_path):
    """主配置的开关 → refresh_settings → 主宠资格 → 世界行为（关/开热切）。

    子宠不在本用例断言面内：它们按各自的 ``config-slot-N.json`` 走（per-slot 段专测）。
    """
    shell, sprites = _spawn_three_pets(tmp_path)
    try:
        world = shell.collision
        events = []
        world.add_collision_listener(events.append)
        assert sprites[0].collision_enabled is True

        # 默认（未改）：主宠参与——真撞有冲量 + 事件
        _setup(world, sprites)
        world.tick(list(shell.overlay.sprites), 0.016)
        assert events, "默认应参与宠-宠碰撞"
        assert sprites[0].velocity.x() < 0 < sprites[1].velocity.x()
        assert sprites[0].interaction_state == "thrown"

        # 关（外部保存 → reload → refresh_settings 这条真实应用链）：主宠退出宠-宠结算
        _save_main(tmp_path, shell, collision_enabled=False)
        assert sprites[0].collision_enabled is False
        events.clear()
        _setup(world, sprites)
        world.tick(list(shell.overlay.sprites), 0.016)
        assert events == []
        assert sprites[0].velocity.x() == 500.0 and sprites[1].velocity.x() == -500.0
        assert sprites[0].interaction_state == "normal"
        assert sprites[0].pos.x() == 0.0  # 也不做位置分离

        # 再开：热切回来照常真撞（证明开关能唤醒结算；"无幽灵残留"由下面那条
        # 连续 tick 用例作证——那条全程不清任何私有簿记）
        _save_main(tmp_path, shell, collision_enabled=True)
        assert sprites[0].collision_enabled is True
        events.clear()
        _setup(world, sprites)
        world.tick(list(shell.overlay.sprites), 0.016)
        assert events
        assert sprites[0].velocity.x() < 0 < sprites[1].velocity.x()
    finally:
        _teardown(shell)


def test_pet_collision_switch_is_independent_from_island_wall(tmp_path):
    """岛（静态成员）接触不受宠物开关影响：关了宠-宠，撞岛照样弹开。"""
    shell, sprites = _spawn_three_pets(tmp_path)
    try:
        world = shell.collision
        events = []
        world.add_collision_listener(events.append)
        world.add_static_member(collision_mod.ISLAND_MEMBER_ID, 300, 0, 40, 200)

        _save_main(tmp_path, shell, collision_enabled=False)
        assert sprites[0].collision_enabled is False

        fish = sprites[0]
        _park(fish, 0.0)
        # 把鱼身体框的中心对到岛的左中圆（岛 300..340 × 0..200，中圆 (320,100) r=20）
        # 前方 40px：相对法向速度正对岛、接触重叠 30px（口径同静态成员 1.3 分支用例）
        body = fish.body_rect()
        fish.set_pos(QPointF(280.0 - float(body.width()) / 2.0,
                             100.0 - float(body.height()) / 2.0))
        fish.set_velocity(QPointF(500.0, 0.0))
        for other, x in zip(sprites[1:], (3000.0, 6000.0)):
            _park(other, x)
        world._prev_circles = {}
        world.tick(list(shell.overlay.sprites), 0.016)

        fish_id = SpriteCollisionWorld._member_id(fish)
        assert [set(event.pair.split("|")) for event in events] == [
            {fish_id, collision_mod.ISLAND_MEMBER_ID}]
        assert fish.velocity.x() < 0  # 岛墙弹回（口径同静态成员 1.3 分支）
    finally:
        _teardown(shell)


# ---------------------------------------------------------------- G4 残留：per-slot 配置
class _SlotInstance:
    """真壳最小宿主（per-slot 用例用真实 Config）：只提供 per-pet 库工厂。"""

    def __init__(self, config):
        self.config = config

    def _create_library(self, _character_id):
        return fac.RichLibrary()


def _make_slot_shell(tmp_path, **config_values):
    """**真实落盘 Config**（``tmp_path`` 下）的真壳 + 真 PetSprite。

    与上面 ``fac._make_shell``（dict 假 config）不同：per-slot 用例必须走真配置
    文件——``_spawn_slot`` 会用 ``slot_manager.seed_slot_config_from_main`` 把主
    配置落种成 ``config-slot-N.json``，这正是旧架构"每个桌宠各自 config"的物质
    基础。所有写盘都在 tmp_path 内。
    """
    from pet.config import Config
    from pet.overlay_shell import OverlayShell
    from pet.pet_sprite import PetSprite
    from tests.test_overlay_window_capabilities import FakeScreen

    cfg = Config(base=tmp_path)
    for key, value in config_values.items():
        cfg.set(key, value)
    cfg.save()
    shell = OverlayShell(
        app, _SlotInstance(cfg),
        screen=FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040)),
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    lib = fac.RichLibrary()
    shell.lib = lib
    shell.sprite.library = lib
    return shell, cfg


def _write_slot(parent, slot, **values):
    """按设置进程的写法改 ``config-slot-N.json``（真 Config + 真落盘）。"""
    from pet.config import Config

    from pet.settings_slot_config import route_settings_config

    cfg = route_settings_config(Config(base=parent, instance_id=f"slot-{slot}"))
    for key, value in values.items():
        cfg.set(key, value)
    assert cfg.save()


def _notify_slot_saved(shell):
    """走壳体既有的"配置目录有变动"入口（D12 watcher 的目录事件回调）。"""
    shell._on_settings_command_dir_changed(str(shell._config.dir))


def _save_main(parent, shell, **values):
    """模拟主配置被外部（设置进程/主进程保存）改写：落盘 → reload → 应用。"""
    from pet.config import Config

    from pet.settings_slot_config import route_settings_config

    other = route_settings_config(Config(base=parent, instance_id="slot-0"))
    for key, value in values.items():
        other.set(key, value)
    assert other.save()
    shell._config.reload()
    shell.refresh_settings()


def _flags(shell):
    """当前三只的资格位（主 + 两只子宠，按 overlay 顺序）。"""
    return [bool(getattr(s, "collision_enabled", True))
            for s in shell.overlay.sprites]


def test_per_slot_collision_switch_applies_per_child(tmp_path):
    """三宠：主 true / childA false / childB true → 各自资格按各自 config。

    childA 的更新走"设置保存 → 目录变动"既有入口，且**不得**改到主宠与 childB。
    """
    shell, _cfg = _make_slot_shell(tmp_path, collision_enabled=True)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        main, child_a, child_b = list(shell.overlay.sprites)
        assert _flags(shell) == [True, True, True]

        # 设置进程写出 childA=false / childB=true，然后触发既有通知入口
        _write_slot(tmp_path, 1, collision_enabled=False)
        _write_slot(tmp_path, 2, collision_enabled=True)
        _notify_slot_saved(shell)
        assert _flags(shell) == [True, False, True]
        assert child_a.collision_enabled is False

        # 同一条变更再走既有 3s 兜底轮询入口 → 幂等，不得把别的宠改掉
        shell._on_settings_command_poll()
        assert _flags(shell) == [True, False, True]
        # 只有 childA 的配置再动一次（触发 A 的更新），主宠/childB 必须原样
        _write_slot(tmp_path, 1, collision_enabled=False, playback_speed=1.5)
        shell._on_settings_command_poll()
        assert _flags(shell) == [True, False, True]
        assert child_b.collision_enabled is True
        # 行为侧：A 直接穿过去，主↔B 照常结算
        world = shell.collision
        events = []
        world.add_collision_listener(events.append)
        a_w = float(main.body_rect().width())
        main.set_pos(QPointF(600.0, 0.0))
        main.set_velocity(QPointF(0.0, 0.0))
        main.interaction_state = "normal"
        child_a.set_pos(QPointF(600.0 + a_w - OVERLAP, 0.0))
        child_a.set_velocity(QPointF(0.0, 0.0))
        child_a.interaction_state = "normal"
        child_b.set_pos(QPointF(3000.0, 0.0))
        child_b.set_velocity(QPointF(0.0, 0.0))
        child_b.interaction_state = "normal"
        world.tick(list(shell.overlay.sprites), 0.016)
        assert events == []                              # A 与主不结算
        assert child_a.pos.x() == 600.0 + a_w - OVERLAP  # A 没被推开
    finally:
        _teardown(shell)


def test_slot_config_event_only_updates_that_slot(tmp_path):
    """B7b 改写（原断言把"slot 事件只动资格位"锁成期望）：目录事件按各 slot 自己
    那份文件重下发**只有它自己**的设置（旧契约：该宠设置保存即覆盖它的运行态），
    别的宠（含主宠）一个键都不碰；全量 ``refresh_settings`` 的语义照旧。

    原窄契约的动机（"目录事件把运行期手改一律覆回主配置值"）在新口径下依然成立：
    被重下发的只有签名变了的那一个 slot，且取源是它**自己的**文件——不是主配置。
    """
    shell, _cfg = _make_slot_shell(tmp_path, collision_enabled=True,
                                   playback_speed=1.0, drag_physics=False)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        main, child_a, child_b = list(shell.overlay.sprites)
        assert main.playback_speed == 1.0 and main.drag_physics is False

        # 运行期手改（模拟右键菜单/其它热改路径写下的值）：三只都改
        for sprite in (main, child_a, child_b):
            sprite.playback_speed = 2.5
            sprite.drag_physics = True
            sprite.throw_speed_cap = 1234.0

        # 只有 childA 的配置变了 → 只有它按自己那份重下发（含资格位）
        _write_slot(tmp_path, 1, collision_enabled=False,
                    playback_speed=0.5, drag_physics=False)
        _notify_slot_saved(shell)
        assert _flags(shell) == [True, False, True]
        assert child_a.playback_speed == 0.5
        assert child_a.drag_physics is False
        for sprite in (main, child_b):
            assert sprite.playback_speed == 2.5
            assert sprite.drag_physics is True
            assert sprite.throw_speed_cap == 1234.0

        # 全量 refresh_settings 语义照旧：主配置值下发到主宠，子宠各读自己那份
        _save_main(tmp_path, shell, playback_speed=0.5, drag_physics=True)
        assert main.playback_speed == 0.5
        assert main.drag_physics is True
        assert child_a.playback_speed == 0.5   # 它自己文件里就是 0.5
        assert child_b.playback_speed == 1.0   # 子宠不跟主配置走（B7b）
        assert _flags(shell) == [True, False, True]  # 资格位跟各自配置
    finally:
        _teardown(shell)


def test_main_switch_change_keeps_child_own_values(tmp_path):
    """刷新主配置不得无条件覆掉子宠各自的独立值（不许总开关强关全组）。"""
    shell, _cfg = _make_slot_shell(tmp_path, collision_enabled=True)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        _write_slot(tmp_path, 1, collision_enabled=True)
        _write_slot(tmp_path, 2, collision_enabled=False)
        _notify_slot_saved(shell)
        assert _flags(shell) == [True, True, False]

        _save_main(tmp_path, shell, collision_enabled=False)
        assert _flags(shell) == [False, True, False]     # 子宠各自的值不变

        _save_main(tmp_path, shell, collision_enabled=True)
        assert _flags(shell) == [True, True, False]
    finally:
        _teardown(shell)


def test_child_missing_or_broken_config_falls_back_and_never_touches_main(tmp_path):
    """子宠缺文件/键取同源默认；坏档案不覆盖；不碰主配置或别宠。"""
    shell, cfg = _make_slot_shell(tmp_path, collision_enabled=False)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        main, child_a, child_b = list(shell.overlay.sprites)
        slot_a = cfg.dir / "config-slot-1.json"
        slot_b = cfg.dir / "config-slot-2.json"
        # spawn 落种 = 旧"缺文件按主配置落种"语义：两只子宠各自文件里都是主值 false
        assert _flags(shell) == [False, False, False]
        main_bytes = (cfg.dir / "config.json").read_bytes()

        _write_slot(tmp_path, 1, collision_enabled=True)   # A 改成自己的 true
        _notify_slot_saved(shell)
        assert _flags(shell) == [False, True, False]

        # 缺键（老/半手写文件）→ 旧 Config 清洗口径的缺省 true
        slot_b.write_text('{"version": 4}', encoding="utf-8")
        _notify_slot_saved(shell)
        assert _flags(shell) == [False, True, True]
        # 损坏（非 JSON）→ 同样缺省 true，不改主配置、不动别宠
        slot_b.write_text("{oops", encoding="utf-8")
        _notify_slot_saved(shell)
        assert _flags(shell) == [False, True, True]
        # 档案缺失按同源默认 true，不持续继承主宠 false（固定身份规范 §五）。
        slot_a.unlink()
        _notify_slot_saved(shell)
        assert _flags(shell) == [False, True, True]
        assert not slot_a.exists()  # 读侧不能暗中重新落种。
        assert (cfg.dir / "config.json").read_bytes() == main_bytes
    finally:
        _teardown(shell)


def test_restore_and_promotion_keep_per_slot_switch(tmp_path):
    """恢复：活跃清单里的 slot 保留各自开关；主退出提升：资格跟提升者的 slot 配置。"""
    shell, cfg = _make_slot_shell(tmp_path, collision_enabled=True)
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        _write_slot(tmp_path, 1, collision_enabled=False)
        _write_slot(tmp_path, 2, collision_enabled=True)
        _notify_slot_saved(shell)
        assert _flags(shell) == [True, False, True]
    finally:
        _teardown(shell)

    # 重启复活：同目录新壳按活跃清单恢复两只，各自的 per-slot 值保留
    restored, cfg2 = _make_slot_shell(tmp_path, collision_enabled=True)
    try:
        assert len(restored.overlay.sprites) == 3
        assert _flags(restored) == [True, False, True]

        # 主宠退出 → 提升 slot-1（它自己的配置是 false）
        assert restored._exit_main_pet() is True
        assert restored.sprite is None
        assert _flags(restored) == [False, True]
        assert [restored._spawned_slots[s] for s in restored._spawned] == [1, 2]
    finally:
        _teardown(restored)


def test_pet_collision_toggle_hot_switch_across_ticks(tmp_path):
    """连续 tick 的关→开热切，**全程不清任何 world 私有簿记**：
    远离正常态 → 关 → 关闭期间逐 tick"穿过"另一只 → 开（两只已分离）→ 连续
    tick 不得凭关闭期间的旧轨迹造冲量/分离；开态重新重叠必须恢复分离；关态
    不阻断自身飞行物理（真实抛掷控制器）。
    """
    shell, sprites = _spawn_three_pets(tmp_path)
    try:
        world = shell.collision
        events = []
        world.add_collision_listener(events.append)
        a, b, c = sprites
        a_w = float(a.body_rect().width())
        # a 放在屏中偏左，给"从右侧穿到左侧"留足空间（PetSprite.set_pos 会按
        # bounds 钳制，x 不能为负——所以不能拿 x=0 当被穿的一方）
        a_x = 600.0

        def _tick():
            world.tick(list(shell.overlay.sprites), 0.016)

        # 0) 远离的正常态：连续 tick 先把帧间快照自然建起来（不清历史）
        a.set_pos(QPointF(a_x, 0.0))
        a.set_velocity(QPointF(0.0, 0.0))
        a.interaction_state = "normal"
        b.set_pos(QPointF(a_x + a_w + 400.0, 0.0))
        b.set_velocity(QPointF(0.0, 0.0))
        b.interaction_state = "normal"
        # c 停到画面另一行（横排三只 461px 宽的身体塞不进 1920 屏，靠 y 分开），
        # 免得它被 b 穿过时产生与本题无关的 pet-pet 事件
        c.set_pos(QPointF(1460.0, 700.0))
        c.set_velocity(QPointF(0.0, 0.0))
        c.interaction_state = "normal"
        for _ in range(3):
            _tick()
        assert events == []

        # 1) 关（主配置外部保存 → reload → refresh_settings → 主宠资格位；
        #    配对侧 b 是子宠，它自己的 config 在 spawn 时从主配置落种为 true，
        #    但 pair 只要有一侧被关就整体过滤，本用例的 a↔b 因此停摆）
        _save_main(tmp_path, shell, collision_enabled=False)
        assert a.collision_enabled is False

        # 2) 关闭期间逐步穿过另一只：每 tick 12.8px（无瞬移），越过一整只身位
        b.set_velocity(QPointF(-800.0, 0.0))
        step = 12.8
        steps = int((b.pos.x() + float(b.rect().width()) - a_x) / step) + 2
        for _ in range(steps):
            b.set_pos(QPointF(b.pos.x() - step, 0.0))
            _tick()
            assert events == []                        # 关态：穿过也不算
            assert a.pos.x() == a_x and a.velocity.x() == 0.0  # 对面那只未被碰
            assert a.interaction_state == "normal"
            assert b.velocity.x() == -800.0            # 关态不改自身速度
            assert b.interaction_state == "normal"
        assert b.pos.x() + float(b.rect().width()) < a_x   # 已完整到 a 左侧
        # 3) 关态不阻断自身飞行物理：真实抛掷控制器照常（重力 + 位置积分）
        fish = a
        fish.set_pos(QPointF(a_x, 400.0))             # 垂直移动，关态无需回避 pair
        fish.set_velocity(QPointF(300.0, 0.0))
        fish.interaction_state = "thrown"
        shell.physics.tick([fish], 0.016)
        assert fish.velocity.y() > 0.0                 # 重力照常作用
        assert fish.pos.y() != 400.0                   # 位置积分照常
        v_before = (fish.velocity.x(), fish.velocity.y())
        _tick()                                        # 关态：世界不碰它
        assert (fish.velocity.x(), fish.velocity.y()) == v_before

        # 4) 开：两只此刻已分离 → 不得凭关闭期间的旧轨迹造冲量/分离
        _save_main(tmp_path, shell, collision_enabled=True)
        assert a.collision_enabled is True
        fish.interaction_state = "normal"
        fish.set_velocity(QPointF(0.0, 0.0))
        fish.set_pos(QPointF(a_x, 0.0))
        b.set_velocity(QPointF(0.0, 0.0))
        pos_before = (a.pos.x(), a.pos.y(), b.pos.x(), b.pos.y())
        for _ in range(3):
            _tick()
        assert events == []
        assert (a.pos.x(), a.pos.y(), b.pos.x(), b.pos.y()) == pos_before
        assert a.interaction_state == "normal" and b.interaction_state == "normal"

        # 5) 开态重新靠近：第一次重叠那一 tick 必须恢复位置分离
        target = a_x + a_w - OVERLAP
        pushed = False
        while b.pos.x() < target:
            want = min(b.pos.x() + 12.8, target)
            b.set_pos(QPointF(want, 0.0))
            _tick()
            if abs(b.pos.x() - want) > 1e-6:
                pushed = True
                break
        assert pushed, "开态重叠应立即恢复结算（位置分离）"
    finally:
        _teardown(shell)
