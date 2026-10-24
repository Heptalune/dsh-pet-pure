# -*- coding: utf-8 -*-
"""动画间隔（``animation_gap_seconds``）sprite 侧接线回归（DS 审查 M5d）。

旧架构落点：``window.py:2561-2576``（动作/移动播完 → 进入 gap，先播一段
待机/转向氛围步并起 ``animation_gap_seconds`` 定时器；gap 内待机步播完继续
播 gap 步；定时器到点后才回掷骰链）。overlay 侧此前零消费（死开关）。

落点：``sprite_behavior.BehaviorController`` 的动画收口/掷骰链，gap 秒数由
壳 ``_sync_sprite_settings`` 注入（config 热改即生效，改 0 立即取消在跑的 gap）。

纪律：offscreen；tick(dt) 同步驱动、确定性 rng，不启动真实 QTimer、不 sleep。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pet.sprite_behavior import STATE_ACTS, STATE_IDLE, STATE_TURN, BehaviorController
from tests.test_overlay_dead_switches import _make_shell
from tests.test_sprite_behavior import (
    BOUNDS,
    ScriptedRng,
    _make_library,
    _make_sprite,
    _roll_into_move,
    _run,
)

app = QApplication.instance() or QApplication([])

# 单圈时长（FakeClip：24 帧 × 42ms）
IDLE_DURATION = 24 * 42 / 1000.0


def _enter_acts(controller, sprite):
    """借 idle 素材进 STATE_ACTS（不掷骰，直接命中动作收口分支）。"""
    assert controller.play_once(sprite, "idle1") is True
    assert controller.state_of(sprite) == STATE_ACTS


def test_animation_gap_plays_idle_step_after_act():
    """动作播完 → 进 gap：先播一段待机步，gap 剩余时长 > 0。"""
    lib = _make_library()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.animation_gap_seconds = 0.5
    _enter_acts(c, sprite)

    c.tick([sprite], IDLE_DURATION + 0.1)

    state = c._states[sprite]
    # gap 氛围步 = 待机或转向（旧机 _play_animation_gap_step 池 = idles+turns）
    assert c.state_of(sprite) in (STATE_IDLE, STATE_TURN)
    assert state.gap_remaining == 0.5
    assert state.anim in ("idle1", "turn1")


def test_animation_gap_replays_step_until_expiry():
    """gap 内待机步播完继续播 gap 步（不回掷骰）；到点后才回掷骰链。"""
    lib = _make_library()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.animation_gap_seconds = 5.0
    _enter_acts(c, sprite)
    c.tick([sprite], IDLE_DURATION + 0.1)          # 进 gap，gap 步起播

    rolls: list = []
    c._roll_next = lambda target, st: rolls.append(st.anim)   # 观测掷骰链入口

    _run(c, sprite, IDLE_DURATION + 0.2, dt=0.1)
    assert rolls == [], "gap 剩余期内不得回掷骰链"

    c._states[sprite].gap_remaining = 0.0          # 模拟 gap 定时器到点
    _run(c, sprite, IDLE_DURATION + 0.2, dt=0.1)
    assert rolls, "gap 到点后必须回掷骰链"


def test_animation_gap_disabled_keeps_roll_chain():
    """默认 0（关）：动作播完直接回掷骰链——既有链路逐位不变。"""
    lib = _make_library()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    assert c.animation_gap_seconds == 0.0
    _enter_acts(c, sprite)

    rolls: list = []
    c._roll_next = lambda target, st: rolls.append(st.anim)
    c.tick([sprite], IDLE_DURATION + 0.1)
    assert rolls == ["idle1"]


def test_animation_gap_applies_after_move():
    """移动播完同样进 gap（旧机 name in moves 分支），不是直接回待机链。"""
    lib = _make_library()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.animation_gap_seconds = 0.5
    _roll_into_move(c, sprite, lib, ints=(100, 0))

    _run(c, sprite, 4.3)
    assert c.state_of(sprite) == STATE_IDLE
    assert c._states[sprite].gap_remaining > 0.0


def test_gap_turn_step_never_flips_facing_by_random_roll():
    """gap 掷中转向素材但无需纠正（中线滞回带内）→ 降级待机，朝向不变。

    旧机 gap 步走 ``_play_roll``（window.py:2699-2707 + :2743-2763），注释明言
    「朝向绝不由随机数翻转」：掷中转向但无需纠正 → 降级待机。旧实现的
    ``_play_animation_gap_step`` 直接 ``_enter_turn`` 绕过了这道闸门，
    turn 播完还会无条件翻 facing ⇒ 背对屏内方向随机转身。
    """
    lib = _make_library()
    sprite = _make_sprite(lib)                      # 中线滞回带内：want = None
    c = BehaviorController(BOUNDS, rng=ScriptedRng(choices=("turn1",)))
    c.animation_gap_seconds = 0.5
    _enter_acts(c, sprite)

    c.tick([sprite], IDLE_DURATION + 0.1)           # 进 gap → 氛围步掷中 turn1

    assert c.state_of(sprite) == STATE_IDLE, "无需纠正的转向素材必须降级待机"
    assert c.anim_of(sprite) == "idle1"
    assert lib.clip("turn1").start_count == 0       # 转向素材根本没起播
    assert sprite.facing == "left"                  # 朝向绝不由随机数翻转


def test_gap_turn_step_still_corrects_facing_when_off_centre():
    """需要纠正朝向时 gap 步照旧播转向，并由收口逻辑翻 facing（闸门不是禁播）。"""
    lib = _make_library()
    sprite = _make_sprite(lib, pos=(100, 400), facing="left")   # 靠左、朝外
    c = BehaviorController(BOUNDS, rng=ScriptedRng(choices=("turn1",)))
    c.animation_gap_seconds = 0.5
    _enter_acts(c, sprite)

    c.tick([sprite], IDLE_DURATION + 0.1)

    assert c.state_of(sprite) == STATE_TURN
    assert c.anim_of(sprite) == "turn1"
    assert sprite.facing == "left"                  # 播完才翻
    _run(c, sprite, lib.duration("turn1") + 0.1)
    assert sprite.facing == "right"


def test_animation_gap_cancelled_by_click_and_by_zero_config():
    """点击打断 / 配置改 0：在跑的 gap 立即作废（window.py:_cancel_animation_gap）。"""
    lib = _make_library()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.animation_gap_seconds = 5.0
    _enter_acts(c, sprite)
    c.tick([sprite], IDLE_DURATION + 0.1)
    assert c._states[sprite].gap_remaining > 0.0

    assert c.on_sprite_clicked(sprite) is True
    assert c._states[sprite].gap_remaining == 0.0

    # 再起一段 gap，改配置为 0 → 立即取消
    _enter_acts(c, sprite)
    c.tick([sprite], IDLE_DURATION + 0.1)
    assert c._states[sprite].gap_remaining > 0.0
    c.animation_gap_seconds = 0.0
    assert c._states[sprite].gap_remaining == 0.0


def test_shell_syncs_animation_gap_config_to_behavior(tmp_path):
    """壳在此同步 gap 秒数：启动读 config，refresh_settings 热改即生效。"""
    shell, _lib = _make_shell(tmp_path, {"animation_gap_seconds": 1.5})
    try:
        assert shell.behavior.animation_gap_seconds == 1.5
        shell._config.set("animation_gap_seconds", 2.5)
        shell.refresh_settings()
        assert shell.behavior.animation_gap_seconds == 2.5
        shell._config.set("animation_gap_seconds", 0.0)
        shell.refresh_settings()
        assert shell.behavior.animation_gap_seconds == 0.0
    finally:
        shell._delete_runtime_marker()
