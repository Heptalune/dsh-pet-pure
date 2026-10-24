# -*- coding: utf-8 -*-
"""拖拽成员速度通道回归：拖鱼撞鱼/撞岛要有冲量弹开，不是只平推。

根因（与岛速缺失同族）：拖拽中的 sprite 自身 velocity 恒 0（on_press
清零、on_move 只 set_pos），碰撞世界 _member_from_sprite 直接读
sprite.velocity → 拖拽成员 v=0 → 相对速度≈0 → 冲量 j=0 → 只剩位置分离。
修法：世界按相邻 tick 位置差估拖拽速度（DRAG_VELOCITY_CAP 封顶）。
"""
from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from pet.sprite_collision import SpriteCollisionWorld
from tests.test_island_bridge import FakePoint, FakeSprite
from tests.test_island_bridge_velocity import FakeClock


def _world(clock):
    return SpriteCollisionWorld(clock=clock)


def test_dragged_sprite_knocks_stationary_sprite():
    """A 以 800px/s 被拖向静止的 B → B 吃冲量切 THROWN + fire 事件。"""
    clock = FakeClock()
    world = _world(clock)
    a = FakeSprite(100.0, 100.0, collision_id="A")
    a.dragging = True                       # 拖拽中（无限质量，自身 v=0）
    b = FakeSprite(300.0, 100.0, collision_id="B")
    events: list = []
    world.add_collision_listener(events.append)

    world.tick([a, b], 1 / 60)              # 采样基线
    clock.advance(0.05)
    a.set_pos(FakePoint(140.0, 100.0))      # 40px/0.05s = 800px/s
    world.tick([a, b], 1 / 60)
    assert world._drag_velocities["A"][0] == pytest.approx(800.0)

    clock.advance(0.05)
    a.set_pos(FakePoint(255.0, 100.0))      # 继续 800px/s 撞进 B
    world.tick([a, b], 1 / 60)

    assert events, "拖鱼撞鱼未 fire 碰撞事件"
    assert b.interaction_state == "thrown"
    assert math.hypot(b.velocity.x(), b.velocity.y()) > 0.0


def test_slow_drag_pushes_without_impulse():
    """慢拖（100px/s < 300 阈值）：只推开，不击飞（去抖/阈值语义不变）。"""
    clock = FakeClock()
    world = _world(clock)
    a = FakeSprite(100.0, 100.0, collision_id="A")
    a.dragging = True
    b = FakeSprite(300.0, 100.0, collision_id="B")
    events: list = []
    world.add_collision_listener(events.append)

    world.tick([a, b], 1 / 60)
    clock.advance(0.4)
    a.set_pos(FakePoint(140.0, 100.0))      # 40px/0.4s = 100px/s
    world.tick([a, b], 1 / 60)
    clock.advance(1.6)
    a.set_pos(FakePoint(255.0, 100.0))      # 继续 100px/s 贴进 B
    world.tick([a, b], 1 / 60)

    assert events == []
    assert b.interaction_state == "normal"


def test_drag_velocity_cleared_on_release():
    """松手（非拖拽态）后不再走估计通道：抛掷速度由 sprite.velocity 承担。"""
    clock = FakeClock()
    world = _world(clock)
    a = FakeSprite(100.0, 100.0, collision_id="A")
    a.dragging = True
    world.tick([a], 1 / 60)
    clock.advance(0.05)
    a.set_pos(FakePoint(140.0, 100.0))
    world.tick([a], 1 / 60)
    assert "A" in world._drag_velocities

    a.dragging = False                      # 松手
    world.tick([a], 1 / 60)
    assert "A" not in world._drag_velocities


def test_drag_velocity_capped_against_teleport():
    """光标瞬移型跳变：估计速度按 DRAG_VELOCITY_CAP 封顶，不放大成荒诞值。"""
    from pet.sprite_collision import DRAG_VELOCITY_CAP

    clock = FakeClock()
    world = _world(clock)
    a = FakeSprite(100.0, 100.0, collision_id="A")
    a.dragging = True
    world.tick([a], 1 / 60)
    clock.advance(0.05)
    a.set_pos(FakePoint(100.0 + 99999.0, 100.0))  # 瞬移跳变
    world.tick([a], 1 / 60)
    vx, vy = world._drag_velocities["A"]
    assert math.hypot(vx, vy) == pytest.approx(DRAG_VELOCITY_CAP)
