# -*- coding: utf-8 -*-
"""CLICK / TURN 圈末收口：墙钟到点 ≠ 本圈播完（IDLE/ACTS/MOVE 之外的第四、五处同源缺口）。

旧架构的动画切换全部由末帧/finished 驱动（``old/pet/window.py:1900-1928``
``is_last`` → ``_end_move_or_anim`` → ``_on_anim_ended``），点击段也在真末帧
收口（``:2652-2667``）。sprite 世界给 ACTS/IDLE/MOVE 各自补了圈末闸门
（``sprite_behavior.py`` 的 ``_acts_tail_done`` / ``_idle_tail_done`` /
``_move_tail_done``），但 STATE_CLICK 与 STATE_TURN 仍是纯墙钟到点即切：

1. 墙钟比实际交付早 80-164ms / 2-4 帧（模块自述 ``sprite_behavior.py:131-148``
   的实机口径）⇒ 点击反应、转向动画的收尾 2-4 帧被截（"没播完就弹回待机 /
   直接翻面"）；
2. 随后 ``bind_clip`` 里旧 clip 的 ``stop()`` 落在「非圈末」⇒ webm_clip 的软停
   判据不真，走硬停杀 ffmpeg、``start()`` 换代 spawn 新进程（与 IDLE 已修的
   每圈一次进程 churn 同源）。

修法与 IDLE/MOVE 同一套证据纪律（有 ``finished`` 契约的 clip 只认 finished
登记或宽限到期；无该接口才退到末帧 + 一次绘制机会；坏 clip 由宽限有界兜底），
TURN 的 facing 翻转随之推迟到真正收口那一刻。

协议复用 ``tests/test_sprite_idle_tail.py`` 的确定性夹具：tick 与帧交付全部
显式驱动（播放器节拍模拟），不起真实 QTimer、不 sleep、不碰 ffmpeg 与真实素材。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF

from pet.pet_sprite import PetSprite
from pet.sprite_behavior import (
    _IDLE_TAIL_GRACE_S,
    STATE_CLICK,
    STATE_IDLE,
    STATE_TURN,
    BehaviorController,
)
from tests.test_sprite_behavior import BOUNDS, ScriptedRng
from tests.test_sprite_idle_tail import (
    FRAME_S_24,
    INTERVAL_24,
    TICK_S,
    ClipLibrary,
    IdleFeed,
    NoFrameApiClip,
    ParkClip,
    Runner,
    _total_starts,
)

FRAMES = 241                 # 实机素材帧数（241 帧 @24fps = 10.0417s 一圈）
WEBM_LAG = 2 * INTERVAL_24   # webm 冷启动首帧迟到（2 个帧间隔）
WAIT_TICK_LIMIT = 200        # 到点后的等待预算（tick）= 3.2s，远超任何合理宽限


def _scene(*, clip_cls=ParkClip, frames=FRAMES, lag=WEBM_LAG,
           pos=(800.0, 400.0), facing="left", rolls=()):
    """单宠现场：短 idle 起播（+ 一条 241 帧的点击/转向素材）。

    ``clip_cls`` = 点击/转向素材的播放器替身（ParkClip / NoFrameApiClip）。
    """
    clips = {
        "idle1": ParkClip("idle1", 24, FRAME_S_24),
        "click1": clip_cls("click1", frames, FRAME_S_24),
        "turn1": clip_cls("turn1", frames, FRAME_S_24),
    }
    lib = ClipLibrary(clips, idles=["idle1"], turns=["turn1"], clicks=["click1"])
    sprite = PetSprite(lib, pos=QPointF(*pos), facing=facing, scale=0.5)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=rolls))
    c.predict_enabled = False
    runner = Runner(c, sprite, IdleFeed(interval=INTERVAL_24, lag=lag), dt=TICK_S)
    runner.step()                                     # 首 tick：进待机 + 起播 idle1
    assert c.state_of(sprite) == STATE_IDLE
    return lib, sprite, c, runner


def _run_to_wallclock(st, clip, lib, runner):
    """跑到墙钟到点那一 tick（此刻本圈通常还差 2-4 帧没交付）。

    终止条件带上「已换绑」（任一 clip 被 start）：旧实现在到点那一 tick 就切换、
    elapsed 随之归零，只等 elapsed ≥ duration 会空转到超时——这条断言就是红点。
    """
    base = _total_starts(lib)
    runner.run_until(lambda: st.elapsed >= clip.duration() or _total_starts(lib) > base)
    assert _total_starts(lib) == base, (
        f"墙钟到点那一 tick 就换绑了（此时末帧 {runner.feed.delivered(clip) + 1}"
        f"/{clip.frames} 还没交付）")


# ---------------------------------------------------------------- CLICK
def test_click_expiry_keeps_clip_until_loop_end_then_returns_to_idle():
    """点击 clip 到点但末帧未交付 → 绝不切走；圈末证据到达才回待机。"""
    lib, sprite, c, runner = _scene()
    assert c.on_sprite_clicked(sprite) is True
    assert c.state_of(sprite) == STATE_CLICK
    st = c._states[sprite]
    clip = lib.clip("click1")

    _run_to_wallclock(st, clip, lib, runner)
    assert runner.feed.delivered(clip) < clip.frames - 1   # 前提：末帧确实还在路上
    assert c.state_of(sprite) == STATE_CLICK              # 红点：旧实现这里已回待机
    assert st.anim == "click1"

    t_open = runner.t
    runner.run_until(lambda: c.state_of(sprite) == STATE_IDLE, limit=WAIT_TICK_LIMIT)
    assert runner.t - t_open < _IDLE_TAIL_GRACE_S          # 收口落在证据上，不等满宽限
    assert runner.feed.delivered(clip) == clip.frames - 1  # 首圈一帧不少
    assert clip.hard_stops == 0                            # 圈末软停：不杀 ffmpeg 换代
    assert clip.soft_parked is True
    assert st.anim == "idle1"                              # 点击链照常回待机


def test_turn_expiry_keeps_clip_and_defers_facing_flip():
    """转向 clip 到点但末帧未交付 → 不切走、绝不提前翻朝向；圈末才翻。"""
    lib, sprite, c, runner = _scene(pos=(100.0, 400.0), facing="left", rolls=(0.35,))
    runner.run_until(lambda: c.state_of(sprite) == STATE_TURN, limit=400)
    st = c._states[sprite]
    clip = lib.clip("turn1")
    assert st.anim == "turn1"
    assert sprite.facing == "left"

    _run_to_wallclock(st, clip, lib, runner)
    assert runner.feed.delivered(clip) < clip.frames - 1   # 前提：末帧还在路上
    assert c.state_of(sprite) == STATE_TURN               # 红点：旧实现这里已回待机
    assert sprite.facing == "left"                        # 朝向绝不提前翻

    t_open = runner.t
    runner.run_until(lambda: c.state_of(sprite) == STATE_IDLE, limit=WAIT_TICK_LIMIT)
    assert runner.t - t_open < _IDLE_TAIL_GRACE_S
    assert runner.feed.delivered(clip) == clip.frames - 1
    assert clip.hard_stops == 0
    assert sprite.facing == "right"                       # 收口那一刻才翻朝向


def test_click_tail_fails_bounded_when_frames_stall():
    """帧停滞（预取断链，永不 finished）→ 宽限到期按旧墙钟语义收口，绝不挂起。

    有界失败是对新闸门最重要的一条边界：拿不到圈末证据时必须自己走出来，
    否则点击/转向会把整条动画链钉死在这一帧上。
    """
    lib, sprite, c, runner = _scene()
    assert c.on_sprite_clicked(sprite) is True
    clip = lib.clip("click1")
    runner.run_until(lambda: runner.feed.delivered(clip) >= 5)
    runner.feed.freeze()                     # 帧停在半途、圈末标记永不送达

    budget = (int(clip.duration() / TICK_S) + 5
              + int(_IDLE_TAIL_GRACE_S / TICK_S) + 10)
    for _ in range(budget):
        runner.step()
        if c.state_of(sprite) != STATE_CLICK:
            break

    assert c.state_of(sprite) == STATE_IDLE            # 有界失败：自己走出来
    assert c.anim_of(sprite) == "idle1"
    assert runner.feed.delivered(clip) < clip.frames - 1   # 确实是「等不到证据」那档


def test_click_without_frame_api_keeps_wallclock_return():
    """帧接口缺失的旧 clip：保持既有墙钟语义（到点即回待机，零额外等待）。

    这是有界失败的对照组——闸门只对能拿到帧证据的播放器生效，旧替身/降级
    播放器一秒都不多等（代价与改动前相同：非圈末 ``stop()`` 照旧硬停）。
    """
    lib, sprite, c, runner = _scene(clip_cls=NoFrameApiClip)
    assert c.on_sprite_clicked(sprite) is True
    clip = lib.clip("click1")
    base = _total_starts(lib)

    runner.run_until(lambda: _total_starts(lib) > base)

    assert c.state_of(sprite) == STATE_IDLE            # 到点即走，不等帧
    assert runner.feed.delivered(clip) < clip.frames - 1   # 末帧确实还在路上
    assert clip.hard_stops == 1                        # 老代价原样保留（未推迟 → 硬停）
