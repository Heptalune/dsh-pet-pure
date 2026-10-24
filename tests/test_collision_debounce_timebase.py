# -*- coding: utf-8 -*-
"""纯位置分离去抖的时间基回归（DS 审查 M12a / REVIEW_VERDICT.md:62-63）。

旧口径按 tick 计数（15 tick）。M-1 闲置降档后 tick 间隔从 T0 的 ~16ms 拉长到
T2 的 250ms / T3 的 1000ms，同一「15 tick」去抖窗口在墙钟上放大 15~60 倍
（T3 下 15 秒不分离），贴贴抖动抑制反而变成"长时间不分开"。

改为秒基：``SEPARATION_DEBOUNCE_SECS`` = 0.24s（= T0 15 tick 等价），时钟
可注入以便确定性推进，不 sleep 赌时序。
"""
from __future__ import annotations

import pytest

from pet import sprite_collision as sc
from pet.sprite_collision import SEPARATION_DEBOUNCE_SECS, SpriteCollisionWorld
from tests.test_sprite_collision import FakeClock, FakeSprite


def _overlapping_pair():
    world_clock = FakeClock()
    world = SpriteCollisionWorld(clock=world_clock)
    a = FakeSprite(0, 0, collision_id="a")
    b = FakeSprite(80, 0, collision_id="b")  # 静止重叠 20px，vn=0 → j=0
    return world, world_clock, a, b


def test_default_debounce_equals_t0_fifteen_ticks():
    """默认窗口 = T0 15 tick 的墙钟等价（0.24s）。"""
    assert SEPARATION_DEBOUNCE_SECS == pytest.approx(15 * 0.016)
    assert sc.SEPARATION_DEBOUNCE_SECS == SEPARATION_DEBOUNCE_SECS


def test_debounce_window_is_wallclock_not_tick_count():
    """去抖按墙钟流逝：模拟 dt 推进再多（降档大 dt 亦然）也不提前失效。"""
    world, clock, a, b = _overlapping_pair()
    world.tick([a, b], 0.016)          # 首次分离生效
    ax1, bx1 = a.pos.x(), b.pos.x()
    assert ax1 < 0.0 and bx1 > 80.0

    # 10 个 tick、模拟时间累计 0.5s（> 0.24s），但墙钟只走了 0.05s：
    # 旧 tick 口径会在这里重新分离——时间基必须仍在窗口内
    for _ in range(10):
        world.tick([a, b], 0.05)
    assert a.pos.x() == ax1 and b.pos.x() == bx1

    # 墙钟满 0.24s：窗口届满，分离再次生效
    clock.advance(SEPARATION_DEBOUNCE_SECS)
    world.tick([a, b], 0.016)
    assert a.pos.x() < ax1 and b.pos.x() > bx1


def test_debounce_holds_across_downgraded_tick_interval():
    """降档口径（T2 250ms / T3 1000ms 间隔）下窗口不被放大：0.2s 内不分离。"""
    world, clock, a, b = _overlapping_pair()
    world.tick([a, b], 0.016)
    ax1 = a.pos.x()

    clock.advance(0.2)                 # T2 档：不足 0.24s
    world.tick([a, b], 0.25)
    assert a.pos.x() == ax1

    clock.advance(0.05)                # 累计 0.25s ≥ 0.24s（不到 T3 的 1 个 tick）
    world.tick([a, b], 1.0)
    assert a.pos.x() < ax1


def test_debounce_table_pruned_by_wallclock():
    """去抖表按墙钟清理陈旧条目（tick 口径 prune 在降档后会长期残留）。"""
    world, clock, a, b = _overlapping_pair()
    world.tick([a, b], 0.016)
    assert world._position_only_at
    clock.advance(SEPARATION_DEBOUNCE_SECS * 5)
    world.tick([a, b], 0.016)
    # 旧条目已在分离时被刷新为当前时刻；再走一个远超 4× 窗口的空档后清掉
    world._position_only_at["stale|pair"] = clock.now - SEPARATION_DEBOUNCE_SECS * 5
    world.tick([a, b], 0.016)
    assert "stale|pair" not in world._position_only_at


def test_constructor_accepts_seconds_window():
    """构造参数为秒（旧 separation_debounce_ticks 已退役）：0 = 不去抖。"""
    world = SpriteCollisionWorld(separation_debounce_secs=0.0)
    a = FakeSprite(0, 0, collision_id="a")
    b = FakeSprite(80, 0, collision_id="b")
    world.tick([a, b], 0.016)
    ax1 = a.pos.x()
    world.tick([a, b], 0.016)          # 窗口 0：立即允许再次分离
    assert a.pos.x() < ax1
