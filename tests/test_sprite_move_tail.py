# -*- coding: utf-8 -*-
"""MOVE 圈末：墙钟到点 ≠ 本圈末帧已交付（IDLE/ACTS 之外的第三个同源缺口）。

实机口径（``.scratch/arch-ab/idlefix2-moveon-abba-20260926-1510/out/
CORRECTION-rc-frame-cpu.md`` §3）：新侧 MOVE 记录到的源帧只到 239（三宠格有到
238 的鱼），241 帧素材的回绕提前发生；旧侧同素材 0..240 全交付。241 帧 @24fps
的名义圈长 10.0417s，播放器定时器取整成 42ms 节拍 ⇒ 末帧要 241×42ms = 10.122s
才交付（实机「比 duration 晚 80-164ms」）。``STATE_MOVE`` 的收口**原为**纯墙钟
（``elapsed >= st.duration`` → snap + 立刻切待机），不接末帧证据 ⇒ 到点那一 tick
把最后 3 帧（238/239/240）一起换掉（复现证据见 ``.scratch/move-tail-repro-20260926/``）。

修复后（行为层一处闸门，``webm_clip`` 的 park 判据一字不动）：到点那一 tick 仍照旧
snap + 速度归零，只推迟**状态切换**；有 ``finished`` 契约的 clip 等本绑定末圈的
finished 登记、在 tick 内切 gap/待机（绝不在信号栈里换绑）；末圈提前到达的 finished
只登记不重播（中间圈照旧 re-arm）；无该接口的有限 clip 退到「末帧 + 一次交付机会」；
坏 clip（帧停滞）由宽限有界兜底；等待期点击/拖拽立即打断且绝不 snap 回旧终点。

本文件的用例同时是修复的验收面（**先红后绿**：修复前曲线/无曲线两档在
``assert c.state_of(sprite) == STATE_MOVE`` 处真断言失败 —— 末帧 238/241 未交付）：

1. 完整一圈计划（241 帧 / loops=1 / duration ≈ 10.0417）到点后，末帧交付前绝不切走；
2. 最终轨迹仍是整圈量化后的 204px；等待期不得继续往前走（不许走过自己的终点）；
3. 多圈 MOVE 的中间圈 re-arm 不得重复走位（位移单调、总量不翻倍）；末圈提前到的
   finished 绝不重播整圈；
4. 拖拽立即接管（计划作废、自主位移停止、松手不回旧终点），等待期点击/拖拽同样。

覆盖矩阵：24/48fps 两档节拍 × 曲线/无曲线 × 单圈/多圈 × finished 提前或迟到 ×
缺信号/帧停滞/帧接口缺失（有界兜底）。

协议复用 ``tests/test_sprite_idle_tail.py`` 的确定性夹具：tick 与帧交付全部显式
驱动，不起真实 QTimer、不 sleep、不碰 ffmpeg 与真实素材。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import QPointF

from pet.pet_sprite import PetSprite
from pet.sprite_behavior import (
    _IDLE_TAIL_GRACE_S,
    STATE_CLICK,
    STATE_DRAG,
    STATE_IDLE,
    STATE_MOVE,
    BehaviorController,
)
from tests.test_sprite_behavior import BOUNDS, ScriptedRng
from tests.test_sprite_idle_tail import (
    FRAME_S_24,
    INTERVAL_24,
    INTERVAL_48,
    TICK_S,
    ClipLibrary,
    IdleFeed,
    NoFrameApiClip,
    NoSignalClip,
    ParkClip,
    Runner,
)

MOVE_FRAMES_24 = 241       # 实机素材帧数（241 帧 @24fps = 10.0417s 一圈）
MOVE_FRAMES_48 = 481       # 48fps 档素材（481 帧 @48fps = 10.0208s 一圈）
FRAME_S_48 = 1.0 / 48
MOVE_STRIDE_PX = 408       # scale 0.5 → 步幅 204px（整圈量化后 = 实机观测位移）
MOVE_DISTANCE_PX = 204
START_X = 800.0
WAIT_TICK_LIMIT = 200      # 到点后的等待预算（tick）= 3.2s，远超任何合理宽限

#: 节拍档（播放器定时器取整口径）：24fps→42ms、48fps→21ms，均 ≥ 名义帧长
CADENCES = [
    ("24fps", MOVE_FRAMES_24, FRAME_S_24, INTERVAL_24),
    ("48fps", MOVE_FRAMES_48, FRAME_S_48, INTERVAL_48),
]
CADENCE_IDS = [c[0] for c in CADENCES]


def _move_curve(frames=MOVE_FRAMES_24, still_ratio=0.25):
    """实机形状的圈内位移曲线：前 25% 圈长静止蓄力，其余线性推进到 1.0。"""
    still = int(frames * still_ratio)
    tail = frames - still
    return [0.0] * still + [i / (tail - 1) for i in range(tail)]


def _move_scene(*, curve=None, distance=MOVE_DISTANCE_PX, drag=None, clicks=(),
                frames=MOVE_FRAMES_24, frame_s=FRAME_S_24, interval=INTERVAL_24,
                clip_cls=ParkClip, end_marker=True):
    """单宠 MOVE 现场：短 idle 播完掷中移动桶 → 素材起步走 ``distance``px。

    ``clip_cls`` = 移动素材的播放器替身（ParkClip / NoSignalClip / NoFrameApiClip）；
    ``end_marker=False`` = 只交帧、圈末标记交给用例手工触发（finished 迟到/提前档）。
    """
    clips = {"idle1": ParkClip("idle1", 24, FRAME_S_24),
             "walk": clip_cls("walk", frames, frame_s)}
    for name in clicks:
        clips[name] = ParkClip(name, 24, FRAME_S_24)
    if drag:
        clips[drag] = ParkClip(drag, 24, FRAME_S_24)
    lib = ClipLibrary(clips, idles=["idle1"], moves=["walk"], drag=drag,
                      clicks=list(clicks), strides={"walk": MOVE_STRIDE_PX})
    lib.move_curves = {"walk": curve} if curve else {}
    sprite = PetSprite(lib, pos=QPointF(START_X, 400), facing="right", scale=0.5)
    # 0.99 → 移动桶；ints=(距离, 0) → randint 取 204、纵向下一个 randint 取 0；右向
    c = BehaviorController(BOUNDS, rng=ScriptedRng(
        rolls=(0.99,), ints=(distance, 0), choices=(1,)))
    c.predict_enabled = False
    runner = Runner(c, sprite,
                    IdleFeed(interval=interval, end_marker=end_marker), dt=TICK_S)
    runner.step()                                     # 首 tick：进待机 + 起播 idle1
    assert c.state_of(sprite) == STATE_IDLE
    runner.run_until(lambda: c.state_of(sprite) == STATE_MOVE, limit=400)
    st = c._states[sprite]
    assert st.anim == "walk"
    assert st.duration == pytest.approx(st.loops * lib.duration("walk"))  # 整圈量化
    return lib, sprite, c, clips["walk"], runner, st


@pytest.mark.parametrize("cadence", CADENCES, ids=CADENCE_IDS)
@pytest.mark.parametrize("curve", [False, True], ids=["no-curve", "curve"])
def test_move_expiry_waits_for_last_frame_and_keeps_full_trajectory(cadence, curve):
    """完整一圈到点：末帧还在路上就切走 = 截尾；末帧交付后收口且位移一分不少。"""
    _label, frames, frame_s, interval = cadence
    _lib, sprite, c, walk, runner, st = _move_scene(
        curve=_move_curve(frames) if curve else None,
        frames=frames, frame_s=frame_s, interval=interval)
    target_x = START_X + MOVE_DISTANCE_PX

    runner.run_until(lambda: st.elapsed >= st.duration
                     or c.state_of(sprite) != STATE_MOVE)
    delivered = runner.feed.delivered(walk)
    assert delivered < walk.frames - 1, "前提失效：末帧竟然已交付"
    assert c.state_of(sprite) == STATE_MOVE, (
        f"墙钟到点那一 tick 就切走了：末帧 {delivered + 1}/{walk.frames} 还没交付"
        f"（本圈最后 {walk.frames - 1 - delivered} 帧被截）")

    limit = runner.ticks + WAIT_TICK_LIMIT
    while runner.feed.delivered(walk) < walk.frames - 1 and runner.ticks < limit:
        runner.step()
        assert c.state_of(sprite) == STATE_MOVE, (
            f"末帧还在路上就切走了（{runner.feed.delivered(walk) + 1}/{walk.frames}）")
        assert abs(sprite.pos.x() - target_x) <= 0.5, (
            f"终点等待期间还在往前走：x={sprite.pos.x()} target={target_x}")

    assert runner.feed.delivered(walk) == walk.frames - 1, "本圈末帧从未交付（截尾）"
    runner.run_until(lambda: c.state_of(sprite) != STATE_MOVE, limit=80)
    # 到点已 snap，等待只推迟切换：换绑落在真圈末上（软停，不是硬停换代）
    assert walk.hard_stops == 0, "尾巴收口仍走了硬停（换了进程/reader 代）"
    assert sprite.pos.x() - START_X == pytest.approx(MOVE_DISTANCE_PX, abs=0.5)


@pytest.mark.parametrize("cadence", CADENCES, ids=CADENCE_IDS)
def test_multi_loop_move_rearm_never_double_moves(cadence):
    """多圈 MOVE：中间圈 finished 触发 re-arm，位移单调推进且总量不翻倍。"""
    _label, frames, frame_s, interval = cadence
    lib, sprite, c, walk, runner, st = _move_scene(
        distance=2 * MOVE_DISTANCE_PX, frames=frames, frame_s=frame_s,
        interval=interval)
    target_x = START_X + 2 * MOVE_DISTANCE_PX
    assert st.loops == 2
    assert st.duration == pytest.approx(2 * lib.duration("walk"))

    x_prev = sprite.pos.x()
    limit = runner.ticks + 4000
    while c.state_of(sprite) == STATE_MOVE and runner.ticks < limit:
        runner.step()
        # 容差 1px：收口那一 tick 的 snap 会把积分残差（实测 0.26px）收回来，
        # 不算回退；re-arm 重置相位那种真回退是一整圈量级（≥ stride）
        assert sprite.pos.x() >= x_prev - 1.0, "移动途中回退（re-arm 重置了位移相位）"
        assert sprite.pos.x() <= target_x + 0.5, "越过终点（re-arm 重复走位）"
        x_prev = sprite.pos.x()

    assert c.state_of(sprite) != STATE_MOVE, "计划未在预算内收口"
    assert walk.start_count == 2, "中间圈没有 re-arm（第 2 圈起动画会冻结在末帧）"
    assert sprite.pos.x() - START_X == pytest.approx(2 * MOVE_DISTANCE_PX, abs=0.5)


def test_drag_takes_over_move_immediately_without_late_snap():
    """拖拽立即接管移动：计划作废、自主位移停止，松手不回旧终点。"""
    _lib, sprite, c, walk, runner, st = _move_scene(drag="hang")
    runner.run_until(lambda: sprite.pos.x() > START_X, limit=200)   # 已经在走
    assert c.state_of(sprite) == STATE_MOVE

    sprite.on_press(QPointF(10, 10))
    sprite.begin_drag()
    c.on_drag_started(sprite)

    assert c.state_of(sprite) == STATE_DRAG           # 同一调用内即接管
    assert sprite._clip_name == "hang"
    assert st.move_target is None                     # 计划作废：不会有到点 snap
    pos = QPointF(sprite.pos)

    for _ in range(60):                               # 0.96s：自主位移不得继续
        runner.step()
        assert c.state_of(sprite) == STATE_DRAG
        assert sprite.pos == pos
    assert walk.start_count == 1                      # 移动素材未被重播/换绑

    sprite.on_release(QPointF(12, 12))                # 原地放下（非甩出，光标 +2px）
    pos_released = QPointF(sprite.pos)                # 松手落在光标处（真拖拽语义）
    c.on_drag_released(sprite)
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite.pos == pos_released                 # 绝不 snap 回旧移动终点


# ---------------------------------------------------------------- 末圈 finished 的早晚
def test_move_final_loop_finished_before_expiry_never_replays():
    """末圈（单圈计划）的 finished 早于墙钟到点：只登记证据，绝不重播整圈。

    finished 是**真实时间**口径、``elapsed`` 是 tick 的 dt 累加口径，dt 上限/GUI
    卡顿会让后者落后——旧口径按「duration - elapsed > 0」在这里 restart_clip()，
    画面从末帧闪回第 0 帧，而位置已按墙钟走到终点。登记证据也不得缩短位移：到点
    那一 tick 才收口。
    """
    _lib, sprite, c, walk, runner, st = _move_scene(end_marker=False)
    runner.run_until(lambda: st.elapsed > 0.2 and c.state_of(sprite) == STATE_MOVE)

    walk.reach_loop_end()                              # 末圈 finished 提前到达
    assert st.move_clip_finished is True               # 只登记
    assert walk.start_count == 1, "末圈 finished 提前到达就重播了整圈"
    assert st.move_tail_grace is None                  # 未到点：等待窗口不提前开

    x_before = sprite.pos.x()
    for _ in range(10):                                # 到点之前照常按墙钟推进
        runner.step()
        assert c.state_of(sprite) == STATE_MOVE, "末圈证据被当成了收口信号（位移被截）"
    assert sprite.pos.x() > x_before, "位置不再推进（early finished 缩短了轨迹）"

    runner.run_until(lambda: st.elapsed >= st.duration, limit=1200)
    runner.run_until(lambda: c.state_of(sprite) != STATE_MOVE, limit=80)
    assert walk.start_count == 1                       # 全程未重播
    assert sprite.pos.x() - START_X == pytest.approx(MOVE_DISTANCE_PX, abs=0.5)


def test_multi_loop_final_finished_early_does_not_replay_last_loop():
    """多圈：中间圈 finished 照常 re-arm，末圈的提前 finished 只登记不重播。

    旧口径在末圈（``duration - elapsed`` 仍 > 0）会 restart_clip()，第 2 圈被整个
    重播（start_count 3）；位移总量仍须是 2×204px。
    """
    lib, sprite, c, walk, runner, st = _move_scene(
        distance=2 * MOVE_DISTANCE_PX, end_marker=False)
    assert st.loops == 2

    runner.run_until(lambda: st.elapsed >= lib.duration("walk"), limit=1200)
    walk.reach_loop_end()                              # 第 1 圈（中间圈）
    assert walk.start_count == 2, "中间圈没续圈（第 2 圈起动画会冻结在末帧）"
    assert st.move_clip_finished is False

    runner.run_until(lambda: st.elapsed >= 1.2 * lib.duration("walk"), limit=600)
    walk.reach_loop_end()                              # 第 2 圈（末圈）提前到达
    assert st.move_clip_finished is True
    assert walk.start_count == 2, "末圈 finished 提前到达就重播了末圈"

    runner.run_until(lambda: c.state_of(sprite) != STATE_MOVE, limit=2000)
    assert walk.start_count == 2
    assert sprite.pos.x() - START_X == pytest.approx(2 * MOVE_DISTANCE_PX, abs=0.5)


def test_move_tail_finished_late_still_closes_on_registration():
    """末帧已交付但 finished 迟到（> 1 个帧间隔）：只认 finished，不按帧号抢先切走。

    有 ``finished`` 契约的 clip，末帧交付 ≠ reader 已驻留圈边界（跨线程无同步点）；
    迟到量远小于宽限，故收口落在 finished 登记后的 1-2 个 tick 上，且换绑落在真
    圈末（软停）而非硬停换代。
    """
    _lib, sprite, c, walk, runner, st = _move_scene(end_marker=False)
    runner.run_until(lambda: st.elapsed >= st.duration
                     or c.state_of(sprite) != STATE_MOVE)
    assert c.state_of(sprite) == STATE_MOVE
    assert st.move_tail_grace is not None              # 窗口已开
    runner.run_until(lambda: runner.feed.delivered(walk) >= walk.frames - 1)

    t_final = runner.t
    runner.run_until(lambda: runner.t - t_final >= 2 * INTERVAL_24 + 2 * TICK_S,
                     limit=200)
    assert c.state_of(sprite) == STATE_MOVE, "末帧到了就抢先切走（finished 还没登记）"
    assert runner.feed.delivered(walk) == walk.frames - 1   # 末帧已在，但契约只认 finished
    assert st.move_clip_finished is False

    walk.reach_loop_end()                              # 迟到的 finished
    assert st.move_clip_finished is True
    before = runner.ticks
    runner.run_until(lambda: c.state_of(sprite) != STATE_MOVE, limit=50)
    assert runner.ticks - before <= 2                  # 登记后下一个 tick 内收口
    assert runner.feed.delivered(walk) == walk.frames - 1   # 整圈末帧一帧不少
    assert walk.hard_stops == 0                        # 真圈末上换绑 = 软停
    assert sprite.pos.x() - START_X == pytest.approx(MOVE_DISTANCE_PX, abs=0.5)


# ---------------------------------------------------------------- 缺信号 / 缺接口 / 卡住
def test_move_without_finished_signal_closes_on_frame_witness():
    """缺 finished 接口的有限素材：末帧交付 + 一次交付/绘制机会即收口，不拖满宽限。

    这一档只能靠帧号证据，故收口略早于圈末标记（软停判据此时不真 → 硬停，代价与
    改动前同档）；关键是**有界**且全圈帧一帧不少。
    """
    _lib, sprite, c, walk, runner, st = _move_scene(clip_cls=NoSignalClip)
    assert getattr(walk, "finished", None) is None      # 信号确实缺（接不上）

    runner.run_until(lambda: st.elapsed >= st.duration
                     or c.state_of(sprite) != STATE_MOVE)
    assert c.state_of(sprite) == STATE_MOVE             # 到点不切
    runner.run_until(lambda: runner.feed.delivered(walk) >= walk.frames - 1,
                     limit=WAIT_TICK_LIMIT)
    assert c.state_of(sprite) == STATE_MOVE             # 末帧那一 tick 只登记观测

    before = runner.ticks
    runner.run_until(lambda: c.state_of(sprite) != STATE_MOVE, limit=50)
    assert runner.ticks - before <= 2                   # 下一个 tick 内收口（不等宽限）
    assert runner.feed.delivered(walk) == walk.frames - 1
    assert sprite.pos.x() - START_X == pytest.approx(MOVE_DISTANCE_PX, abs=0.5)


def test_move_without_frame_api_keeps_wallclock_switch():
    """帧接口缺失（旧替身/降级播放器）→ 零额外等待：到点那一 tick 就切走。

    这条是「不许把所有移动都拖进等帧」的边界，也是既有 test_sprite_behavior 假库
    （clip 帧号恒为 0）仍必须原样通过的原因。
    """
    _lib, sprite, c, walk, runner, st = _move_scene(clip_cls=NoFrameApiClip)

    runner.run_until(lambda: st.elapsed >= st.duration
                     or c.state_of(sprite) != STATE_MOVE, limit=1200)
    assert c.state_of(sprite) != STATE_MOVE             # 到点即收口
    assert st.move_tail_grace is None                   # 从未开等待窗口
    assert st.elapsed <= TICK_S                         # 同一 tick 内完成切换
    assert walk.duration() > 0.0                        # 前提：计划确实建立过
    assert sprite.pos.x() - START_X == pytest.approx(MOVE_DISTANCE_PX, abs=0.5)


def test_move_stalled_clip_exits_on_bounded_grace():
    """帧停在中途且永不 finished（预取断链）→ 宽限到期有界收口，绝不挂起。

    尾巴确实等不到（有界的代价），但位移不受影响：到点已 snap，等待期速度恒 0。
    """
    _lib, sprite, c, walk, runner, st = _move_scene()
    runner.run_until(lambda: runner.feed.delivered(walk) >= 100)
    runner.feed.freeze()                                # 预取断链：帧停在 100、永不 finished

    runner.run_until(lambda: st.elapsed >= st.duration
                     or c.state_of(sprite) != STATE_MOVE, limit=1200)
    assert c.state_of(sprite) == STATE_MOVE             # 到点不切（帧号证据 > 0）
    t_open = runner.t
    assert st.move_tail_grace is not None

    runner.run_until(lambda: c.state_of(sprite) != STATE_MOVE, limit=400)
    waited = runner.t - t_open
    assert _IDLE_TAIL_GRACE_S - TICK_S <= waited <= _IDLE_TAIL_GRACE_S + 3 * TICK_S
    assert runner.feed.delivered(walk) < walk.frames - 1     # 尾巴确实没等到
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite.pos.x() - START_X == pytest.approx(MOVE_DISTANCE_PX, abs=0.5)


def test_move_tail_closes_before_animation_gap_starts():
    """有 gap 配置时：尾巴收口之前绝不进 gap（氛围步会打断尾巴），收口后照常进 gap。"""
    _lib, sprite, c, walk, runner, st = _move_scene()
    c.animation_gap_seconds = 0.5

    runner.run_until(lambda: st.elapsed >= st.duration
                     or c.state_of(sprite) != STATE_MOVE, limit=1200)
    assert c.state_of(sprite) == STATE_MOVE
    assert st.gap_remaining == 0.0                      # gap 未提前起
    runner.run_until(lambda: runner.feed.delivered(walk) >= walk.frames - 1,
                     limit=WAIT_TICK_LIMIT)
    assert c.state_of(sprite) == STATE_MOVE             # 尾巴仍在收口
    assert st.gap_remaining == 0.0

    runner.run_until(lambda: c.state_of(sprite) != STATE_MOVE, limit=80)
    assert c.state_of(sprite) == STATE_IDLE             # gap 氛围步（池 = idles）
    assert st.anim == "idle1"
    assert st.gap_remaining == pytest.approx(0.5)       # 收口后才起 gap 计时


# ---------------------------------------------------------------- 等待期打断
def test_move_tail_interrupted_by_click_never_switches_late():
    """末帧等待期被点击打断：立即切走、窗口作废，旧 clip 的 finished 不污染新绑定。"""
    _lib, sprite, c, walk, runner, st = _move_scene(clicks=("click1",))
    runner.run_until(lambda: st.elapsed >= st.duration
                     or c.state_of(sprite) != STATE_MOVE)
    assert c.state_of(sprite) == STATE_MOVE
    assert st.move_tail_grace is not None               # 等待窗口已开

    assert c.on_sprite_clicked(sprite) is True
    assert c.state_of(sprite) == STATE_CLICK            # 即时打断
    assert st.move_tail_grace is None                   # 窗口随换绑作废
    assert st.move_clip_finished is False
    assert st.move_loops_done == 0
    assert sprite._clip_name == "click1"

    walk.reach_loop_end()                               # 旧 clip 的圈末信号（已断开）
    assert st.move_clip_finished is False               # 旧 finished 绝不落进新绑定
    assert c.state_of(sprite) == STATE_CLICK            # 也不提前收口

    runner.run_until(lambda: c.state_of(sprite) != STATE_CLICK, limit=400)
    assert c.state_of(sprite) == STATE_IDLE             # 点击链照常收口
    for _ in range(40):                                 # 越过原宽限窗口：无第二次切换
        runner.step()
        assert c.state_of(sprite) == STATE_IDLE
    assert walk.start_count == 1                        # 移动素材未被重播/换绑
    assert sprite.pos.x() - START_X == pytest.approx(MOVE_DISTANCE_PX, abs=0.5)


def test_move_tail_interrupted_by_drag_never_snaps_back_to_target():
    """末帧等待期拖拽接管：立即进 DRAG、计划与窗口作废，松手绝不 snap 回旧终点。"""
    _lib, sprite, c, walk, runner, st = _move_scene(drag="hang")
    sprite.drag_physics = False                         # 原地放下（不估甩出初速）
    target_x = START_X + MOVE_DISTANCE_PX
    runner.run_until(lambda: st.elapsed >= st.duration
                     or c.state_of(sprite) != STATE_MOVE)
    assert c.state_of(sprite) == STATE_MOVE
    assert st.move_tail_grace is not None

    press = QPointF(sprite.pos)
    sprite.on_press(press)                              # overlay 接线点：按下
    sprite.begin_drag()                                 # 过阈值升级（真拖拽序列）
    c.on_drag_started(sprite)
    assert c.state_of(sprite) == STATE_DRAG             # 即时打断
    assert st.move_target is None                       # 计划作废：不会有到点 snap
    assert st.move_tail_grace is None
    assert st.move_clip_finished is False
    assert sprite._clip_name == "hang"

    sprite.on_move(press + QPointF(-300, 0))            # 拖到旧终点左侧 300px
    for _ in range(60):                                 # 0.96s：自主位移不得继续
        runner.step()
        assert c.state_of(sprite) == STATE_DRAG

    walk.reach_loop_end()                               # 旧 clip 的圈末信号（已断开）
    assert st.move_clip_finished is False

    sprite.on_release(press + QPointF(-300, 0))         # 原地放下
    dropped = QPointF(sprite.pos)
    c.on_drag_released(sprite)
    assert c.state_of(sprite) == STATE_IDLE
    for _ in range(40):                                 # 越过原宽限窗口
        runner.step()
        assert c.state_of(sprite) == STATE_IDLE
    assert sprite.pos == dropped                        # 绝不 snap 回旧移动终点
    assert abs(sprite.pos.x() - target_x) > 100
    assert walk.start_count == 1                        # 移动素材未被重播/换绑
