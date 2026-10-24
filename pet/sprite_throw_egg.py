# -*- coding: utf-8 -*-
"""彩蛋（overlay/sprite 架构）：边缘探头状态的桌宠被撞飞时，飞行中整帧旋转
使头部跟随速度方向；落地停稳 / 低速触碰边界或桌宠后回正。

语义蓝本是旧窗口机 ``pet/throw_egg.py``（只读参考；**阈值常量直接 import，
不复制数值**）。旧机把彩蛋实现为「窗口级整帧旋转 + 每物理 tick 更新角度」；
overlay 架构下窗口 = 整屏 overlay、宠物 = sprite，于是同一控制器按 sprite
语义重写：

- 状态：``sprite -> 当前角度``（会话表本身就是激活标记；空表时 tick 只做
  一次 dict 判空即早退）；
- 角度写入：``sprite.set_throw_rotation(角度)``——渲染侧走既有
  ``window_effects.begin_rotation/end_rotation`` 管线（与边缘探头同管线、
  同旋转中心 = 帧绘制矩形中心），并经既有 ``_notify_dirty`` 通道报脏，
  角度 0 时零绘制成本；
- 时间基：角度只依赖速度与触界，不依赖 dt/墙钟——**不设飞行时间硬上限**
  （用户明确要求），对齐旧机 ``update(vx, vy, touching_boundary)`` 口径。

arm 条件严格对齐旧机（批 A/批 D 通用约束）：只有「边缘探头会话存在且被
真撞击 ``cancel(reason="collision_throw")``」才 arm；普通抛掷（非探头状态
被击飞）不 arm，维持批 A 现状。取消了会话这个事实由探头世界
``on_sprite_collision_hit`` 的返回值显式传给本模块（对齐旧机
``edge_probe.py:283-287`` 的 was_active 原子绑定），不读探头侧残留倒计时。
彩蛋常开，不加配置开关。

判定口径逐条对齐旧机 ``window.py`` 的接线（``:4275`` 落地兜底 /
``:4453-4455`` 每 tick 驱动）：

1. **触界**（``touching_boundary``）：sprite 左上角回到「身体框贴 bounds 四边」
   的活动范围端点即算触界，与 ``ThrowPhysicsController._sprite_bounds``
   （``sprite_physics.py:69-84``，物理反弹/落地口径）逐式同源——该口径目前
   是私有方法、无只读状态可取，故本模块按同式重算并注释出处；旧机只把
   左右缘与地面算触界，本移植照抄（顶边反弹后速度通常仍高，空中阈值继续
   跟随即可）。
2. **跟随分档**：触界阈值 ``THROW_EGG_RECOVER_SPEED``（贴地弹跳防抖），
   空中阈值 ``THROW_EGG_AIR_FOLLOW_SPEED``（只防近零速 atan2 抖动）；
   速度 >= 分档阈值才写角度，角度 = ``90 + degrees(atan2(vy, vx))``
   （屏幕坐标 y 朝下：向右=90°、向下=180°、向上=0°、向左=270°）。
3. **回正**：速度 < 触界阈值且触界 → 回正结束；飞行中再次撞到其它桌宠
   （``on_probe_collision_throw`` 复入 → ``on_pet_contact``）而速度低于
   该阈值 → 回正；落地停稳（physics 把 ``interaction_state`` 切回 normal）
   → tick 无条件兜底回正（旧 ``_stop_physics`` 的 ``_te.end()``）；拖拽
   抓住飞行中的 sprite 同样回正（任何离开 thrown 态都是回正条件）。

壳层接线（主人侧；本模块不 import overlay/shell，bounds 与探头世界由集成层
传入，offscreen 可测）：

1. ``egg = create_throw_egg_world(bounds, probe=self._probe)``——传入真探头
   世界，入口在显式事实缺省时据此复核「探头会话仍存在」（见
   ``_probe_hit_confirmed``）；
2. ``driver.add_extra_controller(egg)``——tick 挂在抛掷物理**之后**：本世界
   读物理结算后的速度，且落地兜底依赖 physics 已把状态切回 normal；
3. 碰撞真撞击回调（``_on_collision_probe``）里，取
   ``cancelled = probe.on_sprite_collision_hit(sprite)`` 的返回值并紧接着
   ``egg.on_probe_collision_throw(sprite, probe_cancelled=cancelled)``——
   同一入口承担两种语义：「探头被撞 arm」与「已在彩蛋飞行中再次被撞 →
   低速回正」。arm 只认这次的显式取消事实，不读探头侧的残留倒计时；
4. sprite 移除监听接 ``egg.forget``；角色切换/停机接 ``egg.cancel_all``。
"""
from __future__ import annotations

import logging
import math
from typing import Any

from PySide6.QtCore import QRect, QRectF

from .pet_sprite import INTERACTION_NORMAL, INTERACTION_THROWN
from .throw_egg import THROW_EGG_AIR_FOLLOW_SPEED, THROW_EGG_RECOVER_SPEED

log = logging.getLogger(__name__)

# 触界容差（px）：物理 throw_step 在贴边/落地时把坐标精确定到活动范围端点，
# 浮点比较取小容差即可——与旧机 _tick_throw_physics 的 1e-6 同值。
THROW_EGG_BOUNDARY_EPS = 1e-6


def sprite_activity_bounds(sprite: Any, bounds: QRectF) -> tuple[float, float, float, float]:
    """sprite 左上角的活动范围（身体框贴 bounds 四边）。

    与 ``ThrowPhysicsController._sprite_bounds``（``sprite_physics.py:69-84``）
    逐式同源：物理反弹与落地判定都发生在「身体框碰到 bounds 边」的位置，
    本世界据此判触界才能与反弹语义一致。私有方法无只读状态可取，故重算。
    """
    rect = sprite.rect()
    body = sprite.body_rect() if hasattr(sprite, "body_rect") else rect
    off_x, off_y = body.x() - rect.x(), body.y() - rect.y()
    left = bounds.left() - off_x
    top = bounds.top() - off_y
    # sprite 比区域还大时 right/bottom 会小于 left/top：钳回（同物理控制器）
    right = max(left, bounds.right() - off_x - body.width())
    bottom = max(top, bounds.bottom() - off_y - body.height())
    return float(left), float(top), float(right), float(bottom)


def touching_boundary(sprite: Any, bounds: QRect | QRectF,
                      *, eps: float = THROW_EGG_BOUNDARY_EPS) -> bool:
    """sprite 是否贴住 bounds 的左右缘或地面（旧机触界判定的 sprite 版）。

    只用「身体框贴边界」的活动范围端点判定：``set_pos`` 的钳制域与物理反弹
    同源，贴边/落地后坐标恰好等于端点。顶边（天花板）刻意不算触界——旧机
    ``px <= left or px >= right or py >= bottom`` 就没有顶边，本移植逐条对齐。
    """
    left, _top, right, bottom = sprite_activity_bounds(sprite, QRectF(bounds))
    pos = sprite.pos
    x, y = float(pos.x()), float(pos.y())
    return x <= left + eps or x >= right - eps or y >= bottom - eps


class SpriteThrowEggWorld:
    """一组 sprite 的「被撞飞头部跟随速度方向」彩蛋世界：``tick(sprites, dt)``。

    与三控制器（BehaviorController / SpriteCollisionWorld /
    ThrowPhysicsController）及 SpriteEdgeProbeWorld 同构：纯 tick + 事件入口，
    bounds 由集成层传入，不查 QScreen。会话表为空时每一步都是近似零成本早退；
    多 sprite 各自独立（表按 sprite 键，互不影响）。
    """

    def __init__(self, bounds: QRect | QRectF, *, probe: Any = None) -> None:
        self._bounds = QRectF(bounds)
        # 探头世界引用（可空）：显式事实（probe_cancelled）缺省时据此复核
        # 「探头会话仍存在」。不 import sprite_edge_probe（鸭子类型消费
        # is_probing），也不反向依赖 overlay/shell——offscreen 测试可注入替身。
        self._probe = probe
        # sprite -> 当前角度（度）。在表 = 彩蛋会话激活；回正即出表。
        self._angles: dict = {}

    # ---------------------------------------------------------------- 查询
    @property
    def bounds(self) -> QRectF:
        return QRectF(self._bounds)

    def set_bounds(self, bounds: QRect | QRectF) -> None:
        """更新活动边界（屏幕/工作区变化时由集成层调用）；不重算在途角度。"""
        self._bounds = QRectF(bounds)

    @property
    def active(self) -> bool:
        """是否有 sprite 正处于彩蛋会话（供壳层动画门禁/诊断）。"""
        return bool(self._angles)

    def is_active(self, sprite: Any) -> bool:
        return sprite in self._angles

    def angle_of(self, sprite: Any) -> float:
        """当前角度（度）；无会话返回 0.0（渲染侧回正口径）。"""
        return float(self._angles.get(sprite, 0.0))

    # ---------------------------------------------------------------- tick
    def tick(self, sprites, dt: float) -> None:
        """推进一 tick：对每个在会话中的 sprite 更新角度或回正。

        ``dt`` 不参与推进（对齐旧机 update：角度只依赖速度与触界，无时间基，
        不设飞行时间硬上限）。会话表为空时立即返回。
        """
        del dt  # 见 docstring：本世界没有时间基
        if not self._angles:
            return
        for sprite in list(sprites):
            if sprite in self._angles:
                self._tick_sprite(sprite)

    def _tick_sprite(self, sprite: Any) -> None:
        if getattr(sprite, "interaction_state", INTERACTION_NORMAL) != INTERACTION_THROWN:
            # 落地停稳：抛掷物理把状态切回 normal（sprite_physics._advance_thrown
            # 的 is_at_rest 分支）→ 无条件兜底回正（旧机 _stop_physics 的
            # _te.end()）；拖拽打断飞行（状态 = drag）同样在这里回正。
            self.end(sprite, "settled")
            return
        vx, vy = self._velocity_of(sprite)
        speed = math.hypot(vx, vy)
        touching = touching_boundary(sprite, self._bounds)
        follow_threshold = (
            THROW_EGG_RECOVER_SPEED if touching else THROW_EGG_AIR_FOLLOW_SPEED
        )
        if speed >= follow_threshold:
            # 屏幕坐标 y 朝下：向右飞=90°、向下=180°、向上=0°、向左=270°。
            self._set_angle(sprite, 90.0 + math.degrees(math.atan2(vy, vx)))
        if speed < THROW_EGG_RECOVER_SPEED and touching:
            # 贴地/贴边低速段：一碰就回正（旧机防「鱼头快速来回变向」的定案）
            self.end(sprite, "touching_boundary")

    @staticmethod
    def _velocity_of(sprite: Any) -> tuple[float, float]:
        velocity = getattr(sprite, "velocity", None)
        if velocity is None:
            return 0.0, 0.0
        return float(velocity.x()), float(velocity.y())

    # ---------------------------------------------------------------- 事件入口
    def on_probe_collision_throw(self, sprite: Any, *,
                                 probe_cancelled: bool | None = None) -> None:
        """碰撞真撞击入口（壳层从 ``_on_collision_probe`` 调）。

        两种语义按会话表分流：

        - **已在彩蛋会话中**（飞行中再次撞到其它桌宠）：走旧机
          ``on_pet_contact``——用碰撞结算后的速度判定，低速回正；
        - **不在会话中**：只有确认「本次撞击真取消了活跃探头会话」才 arm。
          ``probe_cancelled`` 是探头世界 ``on_sprite_collision_hit`` 的返回
          值（显式事实传递，见旧机 ``edge_probe.py:283-287`` 的 was_active
          原子绑定）：True = arm；False = 本次没取消任何会话，绝不 arm。
          缺省 None 时回退 ``_probe_hit_confirmed``（兼容反向接线顺序：先
          egg 后 probe，此时会话仍在）。
        """
        if sprite in self._angles:
            self.on_pet_contact(sprite)
            return
        if probe_cancelled is False:
            return
        if probe_cancelled is not True and not self._probe_hit_confirmed(sprite):
            return
        self.arm(sprite)

    def on_pet_contact(self, sprite: Any, speed: float | None = None) -> None:
        """飞行中再次撞到其它桌宠：低速则立刻回正（旧机同语义）。

        ``speed`` 缺省取 ``sprite.velocity``（碰撞世界在触发 listener 前已写回
        冲量，读到的是撞击后的速度）。
        """
        if sprite not in self._angles:
            return
        if speed is None:
            speed = math.hypot(*self._velocity_of(sprite))
        if float(speed) < THROW_EGG_RECOVER_SPEED:
            self.end(sprite, "pet_contact")

    def arm(self, sprite: Any) -> None:
        """开始头部跟随速度：角度归零（旧机 arm 同式）；幂等。"""
        self._angles[sprite] = 0.0
        setter = getattr(sprite, "set_throw_rotation", None)
        if callable(setter):
            setter(0.0)
        log.info("[抛掷彩蛋] arm 角度跟随开始")

    def end(self, sprite: Any, reason: str = "") -> None:
        """回正（角度归零）并结束会话；幂等（旧机 end 同式）。"""
        if sprite not in self._angles:
            return
        self._angles.pop(sprite, None)
        clear = getattr(sprite, "clear_throw_rotation", None)
        if callable(clear):
            clear()
        log.info("[抛掷彩蛋] 结束 reason=%s", reason)

    def cancel_all(self, reason: str = "") -> None:
        """结束全部会话（角色切换/停机收口）；幂等。"""
        for sprite in list(self._angles):
            self.end(sprite, reason or "cancel_all")

    def forget(self, sprite: Any) -> None:
        """sprite 移除时清理其会话（overlay sprite-removed 监听接线入口）。"""
        self.end(sprite, "removed")
        self._angles.pop(sprite, None)

    # ---------------------------------------------------------------- 内部
    def _set_angle(self, sprite: Any, angle: float) -> None:
        """写角度：未变化零成本；变化走 sprite 既有脏矩形通道。"""
        angle = float(angle)
        if self._angles.get(sprite) == angle:
            return
        self._angles[sprite] = angle
        setter = getattr(sprite, "set_throw_rotation", None)
        if callable(setter):
            setter(angle)

    def _probe_hit_confirmed(self, sprite: Any) -> bool:
        """复核「探头会话仍存在」（``probe_cancelled`` 缺省时的回退口径）。

        只在显式事实缺省（None）时使用，两种接线顺序都成立：

        - 壳层先调本入口（反向接线）：会话还在，``is_probing`` 为真；
        - 未注入探头世界时退化为 sprite 姿态口径（曝光 < 1 或已有探头角）；
          该退化路径需要调用方在姿态仍生效时调用（探头 cancel 会清姿态），
          因此推荐注入探头世界 + 显式传 ``probe_cancelled``。

        **刻意不读** ``reentry_remaining_of``：那是 cancel 时 arm 的 5 秒
        重进倒计时，探头会话结束/彩蛋 end() 都不会清它——把它当成「本次
        撞击真取消了会话」的证明，会在 5s 窗口内再被撞时误 re-arm（实机
        arm 30 次 vs 探头真实退出仅 11 次的根因）。
        """
        probe = self._probe
        if probe is not None:
            is_probing = getattr(probe, "is_probing", None)
            if callable(is_probing):
                try:
                    if is_probing(sprite):
                        return True
                except Exception:  # 探头替身异常不拖垮碰撞链
                    log.debug("[抛掷彩蛋] is_probing 调用失败", exc_info=True)
            return False
        if bool(getattr(sprite, "probe_active", False)):
            return True
        return abs(float(getattr(sprite, "probe_angle", 0.0) or 0.0)) >= 1e-6


def create_throw_egg_world(bounds: QRect | QRectF, *, probe: Any = None
                           ) -> SpriteThrowEggWorld:
    """工厂函数（壳层接线入口，与三控制器/探头世界同构）。

    ``bounds``：overlay 局部逻辑坐标的工作区（同 BehaviorController 口径）；
    ``probe``：边缘探头世界（可选但推荐——arm 条件复核的权威来源）。
    """
    return SpriteThrowEggWorld(bounds, probe=probe)


# 公开接口清单（壳层接线检索用）。
__all__ = [
    "THROW_EGG_BOUNDARY_EPS",
    "SpriteThrowEggWorld",
    "create_throw_egg_world",
    "sprite_activity_bounds",
    "touching_boundary",
]
