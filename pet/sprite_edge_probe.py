# -*- coding: utf-8 -*-
"""边缘探头（overlay/sprite 架构）：sprite 静止贴到屏幕左右缘后自动 45° 窥视。

语义蓝本是旧窗口机 pet/edge_probe.py（只读参考，本模块不 import 其控制器，
仅 import 纯几何函数 probe_body_bounds 做口径对拍测试）。旧实现把探头实现
为「移顶层窗口 + GNOME 虚拟坐标 + 窗口级旋转 + 绘制偏移」；overlay 架构下
窗口 = 整屏 overlay、宠物 = sprite，于是同一状态机按 sprite 语义重写：

- 位置：``sprite.set_pos``（唯一位置写入口）直接写探头姿态的目标 x，不需要
  虚拟坐标/绘制偏移换算；
- 旋转：``sprite.set_probe_pose(角度, 曝光)``，渲染侧绕帧绘制矩形中心旋转
  （render 口径同旧 ``_apply_pose`` 的 pivot = ``_frame_draw_rect`` 中心）；
- 出屏：探头期间 sprite 自身钳制域放宽（``probe_body_bounds`` 语义：左右各
  放宽一个身体宽），身体按曝光比例藏出屏幕缘；取消即清姿态、恢复常规钳制。

状态机逐条对齐旧机：``OFF/ENTERING/PEEKING/STRAIGHTENING/STRAIGHTENED/
RETURNING``；进入条件 = 边缘 + 持续静止（``EDGE_REST_SECONDS``，「拖着经过
边缘」不算）；过渡动画是角度/曝光 tween，**时间基是注入时钟而不是 tick
计数**；点击真实角色 → 拉直（0°/``EDGE_ENGAGE_EXPOSURE``）→
``EDGE_IDLE_SECONDS`` 后自动退回探头姿态；拖拽开始/碰撞真撞击/抛掷态 →
``cancel``；多 sprite 各自独立；隐藏时 ``pause`` 冻结进度、``resume`` 从原处
继续（同旧机）。

与旧机的两处刻意差异（sprite 世界无对应物，已在测试里固化）：

1. **曝光分母用稳定身体框**（``sprite.body_rect()``）而不是逐帧可见像素包围
   盒（旧 ``character_local_region()``）：sprite 世界没有廉价的 per-frame
   mask bbox，而 body_box 本就是 manifest 声明的贴边/钳制锚点（AGENTS.md）；
   旧机注释也自陈像素口径会被 alpha 羽化过渡带留出判定间隙。
2. **没有窗口 x 恢复值**：``cancel`` 清掉探头姿态即按常规钳制把身体钳回屏内
   （旧 ``_restore_x`` 的等价物），不需要记账进入前的坐标。

tick 链挂载建议（壳层接线）：行为控制器之后（探头姿态是位置/角度的最终写
入口）、抛掷物理之前；``collision.add_collision_listener`` 里把命中事件翻译
成 ``on_sprite_collision_hit``，overlay 鼠标路由接
``on_sprite_drag_started``/``on_sprite_drag_released``/``on_sprite_clicked``
（点击返回值 True 表示被探头消费，壳层据此抑制点击音效），sprite 移除监听
接 ``forget``。本模块不 import overlay/shell，bounds 由集成层传入，offscreen
可测。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from PySide6.QtCore import QEasingCurve, QPointF, QRect

from .pet_sprite import INTERACTION_NORMAL, INTERACTION_THROWN
from .window_effects import eased_progress, rotated_region_bounds

# 日志锚点口径同旧机：定位「探出太多/按框探」需要姿态与分母框数据。
log = logging.getLogger(__name__)

EDGE_PROBE_ANGLE = 45.0
# 露出比例的分母 = 当前姿态（含 ±45° 旋转）投影 bbox 宽度；0.55 使常驻探头
# 只露头/脸并贴住屏幕边缘（0.70 曾导致整个角色探出过多）。与旧机同值。
EDGE_PEEK_EXPOSURE = 0.55
# 点击拉直后基本全出（短暂查看完整桌宠）。
EDGE_ENGAGE_EXPOSURE = 0.82
EDGE_ENTER_MS = 300
EDGE_STRAIGHTEN_MS = 250
EDGE_RETURN_MS = 300
EDGE_IDLE_SECONDS = 5.0
# 碰撞撞飞/抛掷取消后允许重新进入探头吸附前的等待秒数（旧批 A 语义）。
EDGE_REENTRY_SECONDS = 5.0
# 进入前的持续静止时长（sprite 版「停在边缘」判据）：拖着经过边缘不算静止。
EDGE_REST_SECONDS = 0.35
# 静止判定的速度上限（px/s）：行为控制器的步态速度远高于此。
EDGE_REST_SPEED_EPS = 1.0
# 贴边判定容差（px）：身体框被 set_pos 钳到边缘时与 bounds 边界相等，留 1px
# 吸收整数取整（对齐旧机 tolerance=0 的宽松版，sprite 的 rect 是整数化口径）。
EDGE_TOLERANCE_PX = 1

OFF = "OFF"
ENTERING = "ENTERING"
PEEKING = "PEEKING"
STRAIGHTENING = "STRAIGHTENING"
STRAIGHTENED = "STRAIGHTENED"
RETURNING = "RETURNING"

_TRANSITION_MODES = frozenset({ENTERING, STRAIGHTENING, RETURNING})
# 触发「等待落地停稳后重新进入」的取消原因（旧机只有碰撞撞飞 arm；抛掷是
# sprite 世界的独立触发源，同样给 5 秒缓冲，避免刚摔回来又被吸住）。
_REENTRY_REASONS = frozenset({"collision_throw", "throw"})


def edge_side_at_rest(sprite: Any, bounds: QRect) -> str | None:
    """sprite 是否贴在 bounds 的左/右缘（稳定身体框口径）。

    与贴边钳制同源：sprite.body_rect() 必须完整落在 bounds 内，所以钳到缘
    时身体边界与 bounds 边界相等——用身体框而不是像素 mask 判定，不受素材
    羽化边缘影响（旧机 GNOME 上曾因像素口径静默失效）。
    """
    body = sprite.body_rect() if hasattr(sprite, "body_rect") else sprite.rect()
    if body.width() <= 0:
        return None
    if body.left() <= bounds.left() + EDGE_TOLERANCE_PX:
        return "left"
    if body.right() >= bounds.right() - EDGE_TOLERANCE_PX:
        return "right"
    return None


def probe_target_x(side: str, exposure: float, projected_local: QRect,
                   bounds: QRect) -> float:
    """探头姿态下 sprite 左上角的目标 x（旧 probe_window_x 的 sprite 版）。

    ``projected_local`` = 身体框（sprite 局部坐标）绕帧绘制矩形中心旋转后的
    投影外接矩形——露出比例的分母是"当前姿态的投影宽度"，与旧机
    ``probe_window_x`` 的 ``vis_local`` 位置一一对应（旧机那是虚拟窗口坐标下
    的可见像素包围盒，这里是 sprite 局部坐标下的身体框投影）。

    left：按曝光比例把投影框推到左缘外（保留从中心往右的 exposure）；
    right：右缘外，保留从中心往左的 exposure。
    """
    exposure = max(0.0, min(1.0, float(exposure)))
    offscreen = (1.0 - exposure) * projected_local.width()
    if side == "left":
        return float(bounds.left() - offscreen - projected_local.left())
    if side == "right":
        return float(bounds.right() + offscreen - projected_local.right())
    raise ValueError(f"unknown edge side: {side!r}")


class _ProbeState:
    """单个 sprite 的探头会话状态（控制器私有，不落在 sprite 上）。

    ``mode == OFF`` 时该条目只承担两件事之一：进入前的静止累计，或碰撞/抛掷
    取消后的重进倒计时（``reentry_remaining > 0``）。两种都不是"会话"。
    """

    __slots__ = ("mode", "side", "angle", "exposure", "transition_start",
                 "transition_duration_ms", "from_angle", "to_angle",
                 "from_exposure", "to_exposure", "last_tick_time",
                 "idle_remaining", "rest_seconds", "reentry_remaining")

    def __init__(self, now: float) -> None:
        self.mode = OFF
        self.side: str | None = None
        self.angle = 0.0
        self.exposure = 1.0
        self.transition_start = now
        self.transition_duration_ms = 0
        self.from_angle = 0.0
        self.to_angle = 0.0
        self.from_exposure = 1.0
        self.to_exposure = 1.0
        self.last_tick_time = now
        self.idle_remaining = 0.0
        self.rest_seconds = 0.0
        self.reentry_remaining = 0.0


class SpriteEdgeProbeWorld:
    """一组 sprite 的边缘探头世界：``tick(sprites, dt)`` 推进状态机。

    与现有三控制器（BehaviorController / SpriteCollisionWorld /
    ThrowPhysicsController）同构：纯 tick + 事件入口，bounds 由集成层传入，
    不查 QScreen。``config`` 每次进入判定前热读 ``edge_probe_enabled``（设置页
    热切换生效）；关闭且无会话时整条 tick 只做一次 dict 取值即返回。

    ``clock`` 可注入（默认 ``time.monotonic``）：过渡动画/倒计时全部以墙钟为
    时间基，测试注入假钟即可确定性推进，不需要固定 sleep。
    """

    def __init__(self, config: Any, bounds: QRect, *, clock=None) -> None:
        self._config = config
        self._bounds = QRect(bounds)
        self._clock = clock if callable(clock) else time.monotonic
        # sprite -> _ProbeState。会话结束即删除；预进入状态在离开边缘/动起来
        # 时也删除，保证关闭功能后本表为空（下一 tick 零成本早退）。
        self._states: dict = {}
        self._hidden = False
        self._paused_at = 0.0

    # ---------------------------------------------------------------- 查询
    @property
    def bounds(self) -> QRect:
        return QRect(self._bounds)

    def set_bounds(self, bounds: QRect) -> None:
        """更新活动边界（屏幕/工作区变化时由集成层调用）；不重算在途姿态。"""
        self._bounds = QRect(bounds)

    @property
    def active(self) -> bool:
        """是否有 sprite 正处于探头会话（供壳层做点击音效/移动门禁）。"""
        return any(st.mode != OFF for st in self._states.values())

    def is_probing(self, sprite: Any) -> bool:
        return self.mode_of(sprite) != OFF

    def mode_of(self, sprite: Any) -> str:
        st = self._states.get(sprite)
        return st.mode if st is not None else OFF

    def side_of(self, sprite: Any) -> str | None:
        st = self._states.get(sprite)
        return st.side if st is not None and st.mode != OFF else None

    def angle_of(self, sprite: Any) -> float:
        st = self._states.get(sprite)
        return st.angle if st is not None and st.mode != OFF else 0.0

    def exposure_of(self, sprite: Any) -> float:
        st = self._states.get(sprite)
        return st.exposure if st is not None and st.mode != OFF else 1.0

    def reentry_remaining_of(self, sprite: Any) -> float:
        """剩余重进倒计时秒数（无倒计时为 0.0）。"""
        st = self._states.get(sprite)
        return st.reentry_remaining if st is not None else 0.0

    def _enabled(self) -> bool:
        """热读 config（每次进入判定前调用）：缺 key/无 config 一律视为关。"""
        getter = getattr(self._config, "get", None)
        if not callable(getter):
            return False
        try:
            return bool(getter("edge_probe_enabled", False))
        except Exception:  # 配置对象异常不拖垮 tick 链
            log.debug("[边缘探头] 读取 edge_probe_enabled 失败", exc_info=True)
            return False

    # ---------------------------------------------------------------- tick
    def tick(self, sprites, dt: float) -> None:
        """推进一 tick（dt 不参与推进：时间基是注入时钟，对齐旧机 timer 口径）。"""
        if self._hidden:
            return
        if not self._enabled():
            # 关闭功能：在场会话立即退回（清姿态 → 常规钳制），随后整表清空
            # ——之后每 tick 只做一次 config 取值即早退。
            if self._states:
                self.cancel_all("feature_off")
            return
        now = self._clock()
        for sprite in list(sprites):
            st = self._states.get(sprite)
            if st is not None and st.mode != OFF:
                self._tick_session(sprite, st, now)
            elif st is not None and st.reentry_remaining > 0.0:
                self._tick_reentry(sprite, st, now)
            else:
                self._tick_pre_entry(sprite, now)

    # ---------------------------------------------------------------- 事件入口
    def on_sprite_drag_started(self, sprite: Any) -> None:
        """拖拽开始：取消会话，并作废预进入累计/重进倒计时（旧机同语义）。"""
        if self._hidden or not self._enabled():
            return
        st = self._states.get(sprite)
        if st is None:
            return
        if st.mode == OFF:
            self._states.pop(sprite, None)
            return
        self.cancel(sprite, "drag_away")

    def on_sprite_drag_released(self, sprite: Any) -> None:
        """拖拽释放：取消残留会话；是否重新进入由 tick 的静止判定评估。"""
        if self._hidden or not self._enabled():
            return
        st = self._states.get(sprite)
        if st is not None and st.mode != OFF:
            self.cancel(sprite, "drag_away")

    def on_sprite_clicked(self, sprite: Any) -> bool:
        """点击真实角色：PEEKING/RETURNING → 拉直；STRAIGHTENED → 重置倒计时。

        返回 True = 本次点击被探头消费（壳层据此抑制点击音效/点击反应）。
        ENTERING/STRAIGHTENING 期间不重复打断（旧机同语义）。
        """
        if self._hidden or not self._enabled():
            return False
        st = self._states.get(sprite)
        if st is None or st.mode == OFF:
            return False
        now = self._clock()
        if st.mode == PEEKING or st.mode == RETURNING:
            self._start_straighten(sprite, st, now)
        elif st.mode == STRAIGHTENED:
            st.idle_remaining = EDGE_IDLE_SECONDS
            st.last_tick_time = now
        return True

    def on_sprite_collision_hit(self, sprite: Any) -> bool:
        """碰撞真撞击（碰撞世界 listener 接线入口）：取消会话并 arm 重进倒计时。

        返回是否**真的取消了本次活跃探头会话**：True = 执行了
        ``cancel(sprite, "collision_throw")``；未在探头/功能关/隐藏等早退路径
        返回 False。壳层把该返回值显式转给彩蛋 arm 条件——重进倒计时是
        cancel 的产物、会残留 5 秒，不能反过来当作「本次撞击取消了会话」的
        证据（实机 arm 30 次 vs 探头退出仅 11 次的根因）。
        """
        if self._hidden or not self._enabled():
            return False
        st = self._states.get(sprite)
        if st is None or st.mode == OFF:
            return False  # 未在探头：普通碰撞不需要重进倒计时
        self.cancel(sprite, "collision_throw")
        return True

    def cancel(self, sprite: Any, reason: str = "", restore: bool = False) -> None:
        """取消会话（幂等）：清探头姿态（角度 0/曝光 1）→ 常规钳制把身体钳回屏内。

        ``restore`` 保留只为对齐旧机签名：sprite 世界没有窗口 x 恢复值，清姿态
        本身就是恢复（旧 ``_restore_x`` 的等价物）。碰撞/抛掷取消会 arm
        ``EDGE_REENTRY_SECONDS`` 重进倒计时（旧批 A 语义）。
        """
        del restore  # 见 docstring：sprite 世界不需要窗口坐标恢复
        st = self._states.pop(sprite, None)
        if st is None or st.mode == OFF:
            return
        clear = getattr(sprite, "clear_probe_pose", None)
        if callable(clear):
            clear()
        log.info("[边缘探头] 退出 reason=%s side=%s", reason, st.side)
        if reason in _REENTRY_REASONS:
            armed = _ProbeState(self._clock())
            armed.reentry_remaining = EDGE_REENTRY_SECONDS
            self._states[sprite] = armed

    def cancel_all(self, reason: str = "") -> None:
        """取消全部会话并清空状态表（功能关闭/角色切换收口；幂等）。"""
        for sprite in list(self._states):
            st = self._states.get(sprite)
            if st is not None and st.mode != OFF:
                self.cancel(sprite, reason)
        self._states.clear()

    def forget(self, sprite: Any) -> None:
        """sprite 移除时清理其状态（overlay sprite-removed 监听接线入口）。"""
        if sprite not in self._states:
            return
        self.cancel(sprite, "removed")
        self._states.pop(sprite, None)

    # ---------------------------------------------------------------- 隐藏冻结
    def pause(self) -> None:
        """隐藏：冻结状态与倒计时（tick 变 no-op），恢复后从原处继续。"""
        if self._hidden:
            return
        self._hidden = True
        self._paused_at = self._clock()

    def resume(self) -> None:
        """显示：把冻结时长从墙钟基线上抹掉，过渡进度不被隐藏时长吞掉。"""
        if not self._hidden:
            return
        self._hidden = False
        now = self._clock()
        shift = max(0.0, now - self._paused_at)
        for st in self._states.values():
            if st.mode in _TRANSITION_MODES:
                st.transition_start += shift
            st.last_tick_time += shift

    # ---------------------------------------------------------------- 状态机
    @staticmethod
    def _at_rest(sprite: Any) -> bool:
        """静止判据：normal 交互态、未拖拽、速度低于阈值（旧机没有速度口径，
        它靠"事件驱动的入场时机"隐含静止；sprite 世界是持续 tick，必须显式判）。"""
        if getattr(sprite, "interaction_state", INTERACTION_NORMAL) != INTERACTION_NORMAL:
            return False
        if getattr(sprite, "dragging", False):
            return False
        velocity = getattr(sprite, "velocity", None)
        if velocity is None:
            return True
        return math.hypot(velocity.x(), velocity.y()) <= EDGE_REST_SPEED_EPS

    def _tick_pre_entry(self, sprite: Any, now: float) -> None:
        """未会话：累计"贴边静止"时长，达到阈值即进入。"""
        st = self._states.get(sprite)
        if not self._at_rest(sprite) or edge_side_at_rest(sprite, self._bounds) is None:
            if st is not None:
                self._states.pop(sprite, None)  # 离开边缘/动起来：累计作废
            return
        if st is None:
            st = _ProbeState(now)
            self._states[sprite] = st
        dt = max(0.0, now - st.last_tick_time)
        st.last_tick_time = now
        st.rest_seconds += dt
        if st.rest_seconds >= EDGE_REST_SECONDS:
            self._enter(sprite, st, now)

    def _tick_reentry(self, sprite: Any, st: _ProbeState, now: float) -> None:
        """碰撞/抛掷取消后的重进倒计时：到点且仍静止贴边才重新进入。"""
        dt = max(0.0, now - st.last_tick_time)
        st.last_tick_time = now
        st.reentry_remaining = max(0.0, st.reentry_remaining - dt)
        if st.reentry_remaining > 0.0:
            return
        if self._at_rest(sprite) and edge_side_at_rest(sprite, self._bounds) is not None:
            self._enter(sprite, st, now)
        else:
            self._states.pop(sprite, None)

    def _tick_session(self, sprite: Any, st: _ProbeState, now: float) -> None:
        # 兜底：拖拽/抛掷即使没走事件入口（壳层漏接线/事件丢失）也必须打断会话
        interaction = getattr(sprite, "interaction_state", INTERACTION_NORMAL)
        if interaction == INTERACTION_THROWN:
            self.cancel(sprite, "throw")
            return
        if interaction != INTERACTION_NORMAL or getattr(sprite, "dragging", False):
            self.cancel(sprite, "drag_away")
            return
        if st.mode in _TRANSITION_MODES:
            self._tick_transition(sprite, st, now)
        elif st.mode == STRAIGHTENED:
            dt = max(0.0, now - st.last_tick_time)
            st.last_tick_time = now
            st.idle_remaining = max(0.0, st.idle_remaining - dt)
            if st.idle_remaining <= 0.0:
                self._begin_return(sprite, st, now)
        # PEEKING 稳态：不推进、不需要 timer（等点击/取消），同旧机

    def _enter(self, sprite: Any, st: _ProbeState, now: float) -> None:
        side = edge_side_at_rest(sprite, self._bounds) or "left"
        st.side = side
        st.rest_seconds = 0.0
        st.reentry_remaining = 0.0
        st.idle_remaining = 0.0
        set_velocity = getattr(sprite, "set_velocity", None)
        if callable(set_velocity):
            set_velocity(QPointF(0, 0))  # 会话期间不允许位移（旧机 cancel_move 语义）
        sign = 1.0 if side == "left" else -1.0
        self._begin_transition(sprite, st, ENTERING, EDGE_ENTER_MS,
                               to_angle=sign * EDGE_PROBE_ANGLE,
                               to_exposure=EDGE_PEEK_EXPOSURE, now=now)
        log.info("[边缘探头] 入场 side=%s pos=(%.1f,%.1f) 身体框=%s 可用区=%s",
                 side, sprite.pos.x(), sprite.pos.y(),
                 sprite.body_rect().getRect() if hasattr(sprite, "body_rect") else None,
                 self._bounds.getRect())

    def _begin_transition(self, sprite: Any, st: _ProbeState, mode: str,
                          duration_ms: int, *, to_angle: float, to_exposure: float,
                          now: float) -> None:
        st.mode = mode
        st.transition_start = now
        st.transition_duration_ms = int(duration_ms)
        st.last_tick_time = now
        st.from_angle = st.angle
        st.to_angle = float(to_angle)
        st.from_exposure = st.exposure
        st.to_exposure = float(to_exposure)
        self._states[sprite] = st

    def _start_straighten(self, sprite: Any, st: _ProbeState, now: float) -> None:
        """点击拉直：角度回 0、曝光提到 engage（露出更多，方便看全角色）。"""
        self._begin_transition(sprite, st, STRAIGHTENING, EDGE_STRAIGHTEN_MS,
                               to_angle=0.0, to_exposure=EDGE_ENGAGE_EXPOSURE,
                               now=now)

    def _begin_return(self, sprite: Any, st: _ProbeState, now: float) -> None:
        """拉直倒计时到点：退回常驻探头姿态（角度 ±45、曝光 peek）。"""
        sign = 1.0 if st.side == "left" else -1.0
        self._begin_transition(sprite, st, RETURNING, EDGE_RETURN_MS,
                               to_angle=sign * EDGE_PROBE_ANGLE,
                               to_exposure=EDGE_PEEK_EXPOSURE, now=now)

    def _tick_transition(self, sprite: Any, st: _ProbeState, now: float) -> None:
        elapsed_ms = max(0.0, (now - st.transition_start) * 1000.0)
        progress = eased_progress(elapsed_ms, st.transition_duration_ms,
                                  QEasingCurve.Type.OutCubic)
        st.angle = st.from_angle + (st.to_angle - st.from_angle) * progress
        st.exposure = (st.from_exposure
                       + (st.to_exposure - st.from_exposure) * progress)
        self._apply_pose(sprite, st)
        if progress < 1.0:
            return
        if st.mode == ENTERING:
            st.mode = PEEKING
        elif st.mode == STRAIGHTENING:
            st.mode = STRAIGHTENED
            st.idle_remaining = EDGE_IDLE_SECONDS
            st.last_tick_time = now
        elif st.mode == RETURNING:
            st.mode = PEEKING

    def _apply_pose(self, sprite: Any, st: _ProbeState) -> None:
        """把当前 tween 的姿态写到 sprite：先放宽钳制（曝光<1），再写目标 x。"""
        if st.side is None:
            return
        angle = float(st.angle)
        exposure = max(0.0, min(1.0, float(st.exposure)))
        set_pose = getattr(sprite, "set_probe_pose", None)
        if callable(set_pose):
            # 顺序要紧：曝光 < 1 先把该 sprite 的钳制放宽，随后 set_pos 才允许
            # 身体藏出屏幕缘（其它 sprite 的钳制不受影响）
            set_pose(angle, exposure)
        rect = sprite.rect()
        body = sprite.body_rect() if hasattr(sprite, "body_rect") else rect
        # 投影算在 sprite 局部坐标（相对 pos）里：旋转中心 = 帧绘制矩形中心，
        # 与渲染侧同一个窗口（QRect(0,0,w,h)）与同一个旋转数学。
        projected = rotated_region_bounds(
            body.translated(-rect.x(), -rect.y()),
            QRect(0, 0, rect.width(), rect.height()),
            angle)
        target_x = probe_target_x(st.side, exposure, projected, self._bounds)
        if abs(target_x - sprite.pos.x()) > 1e-9:
            sprite.set_pos(QPointF(target_x, sprite.pos.y()))


def create_edge_probe_world(config: Any, bounds: QRect, *, clock=None) -> SpriteEdgeProbeWorld:
    """工厂函数（壳层接线入口，与三控制器同构）。

    ``config``：PetInstance 的 Config（只读 ``edge_probe_enabled``）；
    ``bounds``：overlay 局部逻辑坐标的工作区（同 BehaviorController 口径）。
    """
    return SpriteEdgeProbeWorld(config, bounds, clock=clock)


# 公开接口清单（壳层接线检索用）。
__all__ = [
    "EDGE_ENGAGE_EXPOSURE", "EDGE_ENTER_MS", "EDGE_IDLE_SECONDS",
    "EDGE_PEEK_EXPOSURE", "EDGE_PROBE_ANGLE", "EDGE_REENTRY_SECONDS",
    "EDGE_REST_SECONDS", "EDGE_REST_SPEED_EPS", "EDGE_RETURN_MS",
    "EDGE_STRAIGHTEN_MS", "EDGE_TOLERANCE_PX",
    "ENTERING", "OFF", "PEEKING", "RETURNING", "STRAIGHTENED",
    "STRAIGHTENING", "SpriteEdgeProbeWorld", "create_edge_probe_world",
    "edge_side_at_rest", "probe_target_x",
]
