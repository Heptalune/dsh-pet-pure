# -*- coding: utf-8 -*-
"""thrown × 静态成员（灵动岛）碰撞回归：触岛泵能根修 + 落岛收尾。

产品化自 ``.scratch/thrown-island-fuzz/repro.py``（18 角度 × 3 速度全向抛掷
单 sprite）。修复前的实机故障链：

1. 碰撞真撞击 → sprite 进 ``thrown``；
2. 静态成员（岛）参与的每一次触岛都按 ``STATIC_RESTITUTION=1.3`` 放大法向
   速度（等效恢复系数 >1）→ 每次触岛净吸能、越弹越高；
3. 抛掷物理的 ``physics.is_at_rest`` 只认屏幕地板，被岛托住的 sprite 速度
   被接触冲量抹平却永远满足不了地板判据；
4. ``interaction_state`` 一直挂在 ``thrown`` → 行为机永不重绑、抛掷 clip
   播完停在最后一帧 = 用户看到的「碰撞后画面卡住不动」。

时序纪律（AGENTS.md）：全部确定性同步驱动——碰撞世界与抛掷物理控制器都是
纯 ``tick`` 函数（无 QTimer、无线程、无 sleep），每例按固定 dt 推进固定
tick 数，不赌墙钟、不赌目录枚举顺序。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect

from pet import collision, physics
from pet.sprite_collision import SpriteCollisionWorld
from pet.sprite_physics import ThrowPhysicsController

INTERACTION_NORMAL = "normal"
INTERACTION_THROWN = "thrown"

TICK_DT = 0.006
# 与 repo 复现脚本同口径：2560×1440 活动区、sprite 从 (1100, 200) 抛出
BOUNDS = QRect(0, 0, 2560, 1440)
START_X, START_Y = 1100.0, 200.0
BODY_W, BODY_H = 120, 150
ISLAND_RECT = (1000.0, 400.0, 400.0, 80.0)
# 抛掷物理的"地板"（ThrowPhysicsController._sprite_bounds：身体框贴 bounds 底）
FLOOR_Y = float(BOUNDS.bottom()) - BODY_H
# 复现脚本的观察窗口（10s）——下面所有"落定"断言都按它收紧
REPRO_BUDGET_TICKS = int(10.0 / TICK_DT)

ANGLES = tuple(range(0, 360, 20))
SPEEDS = (200.0, 500.0, 900.0)


class FakeSprite:
    """repo 复现脚本同款鸭子类型 sprite（pos/velocity/rect/body_rect/状态）。"""

    def __init__(self, x: float, y: float, *, vx: float = 0.0, vy: float = 0.0,
                 cid: str = "fake-1") -> None:
        self.pos = QPointF(x, y)
        self.velocity = QPointF(vx, vy)
        self.interaction_state = INTERACTION_NORMAL
        self.collision_id = cid

    def rect(self) -> QRect:
        return QRect(int(self.pos.x()), int(self.pos.y()), BODY_W, BODY_H)

    def body_rect(self) -> QRect:
        return self.rect()

    def set_pos(self, p) -> None:
        self.pos = QPointF(p)

    def set_velocity(self, v) -> None:
        self.velocity = QPointF(v)

    def speed(self) -> float:
        return math.hypot(self.velocity.x(), self.velocity.y())


# ---------------------------------------------------------------- 夹具
class Case:
    """一例抛掷的完整轨迹记录：落定 tick、峰值速度、上岛次数。"""

    def __init__(self) -> None:
        self.settle_tick: int | None = None
        self.peak_speed = 0.0
        self.island_contacts = 0


def _world(with_island: bool = True) -> SpriteCollisionWorld:
    world = SpriteCollisionWorld()
    if with_island:
        world.add_static_member(collision.ISLAND_MEMBER_ID, *ISLAND_RECT)
    return world


def _run_throw(angle_deg: float, speed: float, *,
               with_island: bool = True,
               max_ticks: int = REPRO_BUDGET_TICKS,
               sprite: FakeSprite | None = None) -> Case:
    """单 sprite 抛掷一例：同步推进 world.tick + physics.tick 至回 normal。"""
    world = _world(with_island)
    controller = ThrowPhysicsController(BOUNDS)
    a = sprite if sprite is not None else FakeSprite(START_X, START_Y)
    a.interaction_state = INTERACTION_THROWN
    ang = math.radians(angle_deg)
    a.set_velocity(QPointF(speed * math.cos(ang), speed * math.sin(ang)))
    case = Case()
    sprites = [a]
    for tick in range(max_ticks):
        results = world.tick(sprites, TICK_DT)
        case.island_contacts += sum(
            1 for res in results
            if collision.ISLAND_MEMBER_ID in (res.a, res.b))
        controller.tick(sprites, TICK_DT)
        case.peak_speed = max(case.peak_speed, a.speed())
        if a.interaction_state == INTERACTION_NORMAL:
            case.settle_tick = tick
            return case
    return case


def _energy_ceiling(speed: float) -> float:
    """出手 + 重力能给出的速度物理上限：sqrt(v0² + 2g·最大落差)。

    墙体/静态成员都是 e≤1 的反弹面（不产能量），所以任何时刻的速率都不该
    超过它——修复前的 1.3 泵能让峰值速度（实测 2121）越过这条线。
    """
    drop = FLOOR_Y - START_Y
    return math.sqrt(speed * speed + 2.0 * physics.GRAVITY * drop)


# ---------------------------------------------------------------- 落定矩阵
def test_thrown_island_matrix_all_settle_within_repro_budget():
    """18 角度 × 3 速度全向抛掷：有岛时每一例都在 10s 内回 normal。

    修复前：54/54 永不回 normal（岛泵能 + 落岛无出口）。
    """
    stuck = [(ang, speed) for ang in ANGLES for speed in SPEEDS
             if _run_throw(ang, speed).settle_tick is None]
    assert stuck == [], f"thrown 未落定（岛在场）：{stuck}"


def test_thrown_no_island_control_all_settle():
    """对照组（无岛）：同一矩阵同样在 10s 内落定——地板物理没有被改坏。"""
    stuck = [(ang, speed) for ang in ANGLES for speed in SPEEDS
             if _run_throw(ang, speed, with_island=False).settle_tick is None]
    assert stuck == [], f"thrown 未落定（无岛对照）：{stuck}"


def test_thrown_speed_never_exceeds_throw_plus_gravity_ceiling():
    """无能量增长：整段飞行的速率不超过出手 + 重力的物理上限。"""
    bad = []
    for ang in ANGLES:
        for speed in SPEEDS:
            case = _run_throw(ang, speed)
            if case.peak_speed > _energy_ceiling(speed) + 1e-6:
                bad.append((ang, speed, case.peak_speed, _energy_ceiling(speed)))
    assert bad == [], f"峰值速度越过物理上限（泵能）：{bad}"


def test_sprite_landing_on_island_settles_fast():
    """落岛即落地：被岛托住的 thrown 在支撑判据窗口内收尾（不是等 10s）。"""
    case = _run_throw(270.0, 200.0)
    assert case.settle_tick is not None
    # 岛在起点正下方：首次触岛后应很快收尾，远早于地板弹跳段
    assert case.island_contacts > 0
    assert case.settle_tick < REPRO_BUDGET_TICKS // 2


# ---------------------------------------------------------------- 冲量口径
def test_thrown_static_bounce_does_not_amplify_speed():
    """已在抛掷中的 sprite 撞静态成员：出射速度 ≤ 入射速度（恢复系数 ≤1）。"""
    world = SpriteCollisionWorld()
    world.add_static_member("island", 300, 0, 40, 200)
    a = FakeSprite(230, 50, vx=500, cid="a")
    a.interaction_state = INTERACTION_THROWN
    world.tick([a], 0.016)
    assert a.velocity.x() < 0.0                       # 被弹回
    assert abs(a.velocity.x()) <= 500.0               # 出射 ≤ 入射
    assert abs(a.velocity.x()) < 500.0                # e<1：严格衰减
    assert a.interaction_state == INTERACTION_THROWN  # 抛掷中不因触岛被改写


def test_non_thrown_entry_keeps_jelly_trampoline():
    """对照（legacy 语义保留）：未抛掷的 sprite 撞岛的入场那一次仍是 1.3 弹床。

    对齐 ``island_collision._clamp_body`` 的非 throw 分支：撞岛像撞弹床、
    并把桌宠切进抛掷物理；只有"已在抛掷中"的触岛才退化成 ≤1 的普通反弹。
    同一几何 / 同一入射速度下两种状态必须给出不同的出射速度。
    """
    def outgoing(state: str) -> tuple[float, str]:
        world = SpriteCollisionWorld()
        world.add_static_member("island", 300, 0, 40, 200)
        a = FakeSprite(230, 50, vx=500, cid="a")
        a.interaction_state = state
        world.tick([a], 0.016)
        return a.speed(), a.interaction_state

    entry_speed, entry_state = outgoing(INTERACTION_NORMAL)
    thrown_speed, thrown_state = outgoing(INTERACTION_THROWN)
    assert entry_state == INTERACTION_THROWN          # 撞岛进抛掷（入场业务）
    assert entry_speed > 500.0                        # 1.3 弹床：出射 > 入射
    assert thrown_speed <= 500.0                      # 抛掷中：出射 ≤ 入射
    assert thrown_state == INTERACTION_THROWN
    assert entry_speed > thrown_speed


def test_resting_on_static_member_settles_after_support_window():
    """静置在静态成员上（低速贴合）连续若干 tick 后收尾回 normal 并停住。"""
    world = SpriteCollisionWorld()
    world.add_static_member(collision.ISLAND_MEMBER_ID, *ISLAND_RECT)
    a = FakeSprite(START_X, 300.0)                    # 身体框压在岛顶
    a.interaction_state = INTERACTION_THROWN
    settled_at = None
    for tick in range(120):
        world.tick([a], TICK_DT)                      # 不给重力：纯贴合
        if a.interaction_state == INTERACTION_NORMAL:
            settled_at = tick
            break
    assert settled_at is not None
    assert settled_at >= SpriteCollisionWorld().support_settle_ticks - 1
    assert a.velocity == QPointF(0, 0)


# ---------------------------------------------------------------- 多 sprite fuzz
def _fuzz_sprites(count: int, seed: int) -> list[FakeSprite]:
    """确定性伪随机（线性同余）：角度/速度/起点都可复现，不依赖 random 实现。

    起点按 260px 横向错开（身体框 120 宽），避免出生即重叠——重叠开局的
    冲量会把能量在 sprites 之间搬来搬去，干扰"无能量增长"的系统能断言。
    """
    out = []
    state = seed
    for i in range(count):
        state = (1103515245 * state + 12345) % (1 << 31)
        angle = (state % 36000) / 100.0
        state = (1103515245 * state + 12345) % (1 << 31)
        speed = 200.0 + (state % 700)
        x = 860.0 + 260.0 * i
        sprite = FakeSprite(x, START_Y, cid=f"fuzz-{i}")
        sprite.interaction_state = INTERACTION_THROWN
        ang = math.radians(angle)
        sprite.set_velocity(QPointF(speed * math.cos(ang), speed * math.sin(ang)))
        out.append(sprite)
    return out


def _system_energy(sprites) -> float:
    """系统机械能（地板为零势面）之和：½v² + g·(地板 - y)。"""
    return sum(0.5 * s.speed() ** 2
               + physics.GRAVITY * (FLOOR_Y - s.pos.y())
               for s in sprites)


def test_multi_sprite_island_fuzz_settles_without_energy_growth():
    """2~3 只 sprite + 岛混合场景：全部落定，系统能量不增长。

    纯 e≤1 的反弹面 + 重力不会创生机械能：全过程系统能量不得超过出手时刻
    的总能量（修复前的 1.3 泵能会立刻把它顶穿）。
    """
    for count in (2, 3):
        for seed in (1, 7, 99):
            world = _world()
            controller = ThrowPhysicsController(BOUNDS)
            sprites = _fuzz_sprites(count, seed)
            budget = _system_energy(sprites)
            peak = budget
            settled_at = None
            for tick in range(REPRO_BUDGET_TICKS * 2):   # 混合场景给 2× 预算
                world.tick(sprites, TICK_DT)
                controller.tick(sprites, TICK_DT)
                peak = max(peak, _system_energy(sprites))
                if all(s.interaction_state == INTERACTION_NORMAL for s in sprites):
                    settled_at = tick
                    break
            stuck = [s.collision_id for s in sprites
                     if s.interaction_state != INTERACTION_NORMAL]
            assert stuck == [], f"多 sprite 未落定 n={count} seed={seed}: {stuck}"
            assert settled_at is not None
            assert peak <= budget + 1e-6, (
                f"系统能量增长 n={count} seed={seed}: peak={peak} budget={budget}")
            for s in sprites:
                assert s.velocity == QPointF(0, 0)
