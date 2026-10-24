# -*- coding: utf-8 -*-
"""SpriteBubbleFollower offscreen 单测（4.1c 气泡）。

覆盖：锚点 = 身体框全局换算（非整画布）、30Hz 节流 + 尾部补发、
say 走真实 PetSpeechBubble.show_text 形参、show_image 配图通道、close 静默；
close 生命周期（G8）：pending timer 立即停 + pending 复位 + 不迟到补发、
重复 close 幂等不抛、overlay 存活时反复建/关跟随器后 QTimer 子对象数回基线
且旧 follower 可被回收（真实 QTimer + Qt 事件循环，不 mock）、
overlay 已销毁（wrapper 失效）后 close 仍不抛。

P1/P2（B6 气泡跟随）：拖拽起止关键帧强制同步（``follow_now``，真 overlay 真
PetSprite + 合成鼠标事件，不等 tick/33ms 窗口）、缩放变更重排（``reflow`` 传入
新 pet_scale）、``refresh_anchor`` 重定位、隐态/已关闭一律无操作。
"""
from __future__ import annotations

import gc
import os
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
)
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

import tests.test_sprite_menu_facade as fac
from pet.sprite_bubble import SpriteBubbleFollower, sprite_anchor_rect_global

app = QApplication.instance() or QApplication([])


class FakeBubble:
    """记录型气泡替身（可见态可控：forced sync 只在可见时动作）。"""

    def __init__(self, *, visible=True):
        self.moves: list[QRect] = []
        self.texts: list = []
        self.reflows: list = []
        self.closed = 0
        self._visible = bool(visible)

    def isVisible(self):
        return self._visible

    def set_visible(self, on):
        self._visible = bool(on)

    def reposition(self, anchor):
        self.moves.append(QRect(anchor))

    def reflow(self, anchor, *, pet_scale=None):
        self.reflows.append((QRect(anchor), pet_scale))

    def show_text(self, text, anchor, duration_ms, **kw):
        self.texts.append((text, QRect(anchor), duration_ms, kw))

    def hide(self):
        pass

    def close(self):
        self.closed += 1


def _make(tmp_path):
    shell, lib = fac._make_shell(tmp_path)
    shell.sprite.bind_clip("idle1")
    shell.sprite._rebuild_pixmap()
    follower = SpriteBubbleFollower(shell.overlay, shell.sprite)
    follower.bubble = FakeBubble()
    return shell, follower


def test_anchor_uses_body_rect_global(tmp_path):
    shell, follower = _make(tmp_path)
    try:
        anchor = follower.anchor()
        origin = shell.overlay.geometry().topLeft()
        # CapSprite 无 body_rect → 回退整 rect；这里直接用真 PetSprite（fac 壳）
        body = shell.sprite.body_rect() if hasattr(shell.sprite, "body_rect") else shell.sprite.rect()
        assert anchor == QRect(origin + body.topLeft(), body.size())
    finally:
        shell._delete_runtime_marker()


def test_follow_throttle_and_flush(tmp_path):
    shell, follower = _make(tmp_path)
    try:
        follower._on_sprite_moved(shell.sprite)   # 第一发立即
        assert len(follower.bubble.moves) == 1
        follower._on_sprite_moved(shell.sprite)   # 30Hz 窗口内 → 节流滞留
        assert len(follower.bubble.moves) == 1
        assert follower._follow_pending is True
        follower._flush_follow()                  # 尾部补发
        assert len(follower.bubble.moves) == 2
        assert follower._follow_pending is False
    finally:
        shell._delete_runtime_marker()


def test_say_passes_anchor_and_kwargs(tmp_path):
    shell, follower = _make(tmp_path)
    try:
        assert follower.say("你好", 2500, subtitle="sub") is True
        text, anchor, dur, kw = follower.bubble.texts[0]
        assert text == "你好" and dur == 2500 and kw["subtitle"] == "sub"
        assert anchor.width() > 0
        assert follower.say("") is False
        follower.close()
        assert follower.bubble is None
    finally:
        shell._delete_runtime_marker()


def test_show_image_delegates_anchor_and_scale(tmp_path):
    """配图自言自语：形参面对齐 PetSpeechBubble.show_image，锚点/缩放由本层补齐。"""
    shell, follower = _make(tmp_path)
    try:
        calls = []

        def _show_image(path, anchor, duration_ms, **kw):
            calls.append((path, QRect(anchor), duration_ms, kw))
            return True

        follower.bubble.show_image = _show_image
        assert follower.show_image("cat.png", 1500, image_scale=1.6) is True
        path, anchor, duration_ms, kw = calls[0]
        assert path == "cat.png" and duration_ms == 1500
        assert kw["image_scale"] == 1.6
        assert kw["pet_scale"] == shell.sprite.scale
        body = (shell.sprite.body_rect() if hasattr(shell.sprite, "body_rect")
                else shell.sprite.rect())
        assert anchor == QRect(shell.overlay.geometry().topLeft() + body.topLeft(),
                               body.size())
    finally:
        shell._delete_runtime_marker()


def test_show_image_degrades_quietly(tmp_path):
    """气泡不可用 / 底层不支持配图 / 底层抛异常 → False，绝不抛到调用方。"""
    shell, follower = _make(tmp_path)
    try:
        follower.bubble = None
        assert follower.show_image("cat.png", 1000) is False

        follower.bubble = FakeBubble()  # 无 show_image：按失败降级
        assert follower.show_image("cat.png", 1000) is False

        def _boom(*_args, **_kwargs):
            raise RuntimeError("pixmap decode failed")

        follower.bubble.show_image = _boom
        assert follower.show_image("cat.png", 1000) is False
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- close 生命周期（G8）
class _OverlayHost(QWidget):
    """最小真实 overlay 替身（QWidget）：只为"父对象被 deleteLater 后 close 不抛"用。

    ``add/remove_position_listener`` 走真实 dict 记账（不 mock QTimer：
    follower 里的 QTimer 仍是真 QTimer、父对象是真 QWidget）。
    """

    def __init__(self):
        super().__init__()
        self._position_listeners: dict = {}

    def add_position_listener(self, sprite, cb):
        self._position_listeners.setdefault(sprite, []).append(cb)

    def remove_position_listener(self, sprite, cb):
        listeners = self._position_listeners.get(sprite)
        if listeners and cb in listeners:
            listeners.remove(cb)


class _Sprite:
    """只有 rect/body_rect 的最小 sprite（锚点用）。"""

    def rect(self):
        return QRect(10, 20, 120, 80)


def _deliver_deletes(*objects) -> None:
    """逐对象定向派发 DeferredDelete（对象已销毁时静默跳过）。

    仓库纪律（`tests/conftest.py:223-229`）：本仓**禁用**共享 QApplication 的全局
    `processEvents()` 与进程级 `sendPostedEvents`——全局冲刷会把其他测试遗留的
    排队事件派发到已销毁对象上，是全量套件偶发 access violation 的既定机制。
    """
    for obj in objects:
        if obj is None:
            continue
        try:
            QCoreApplication.sendPostedEvents(obj, QEvent.Type.DeferredDelete)
        except RuntimeError:
            pass  # wrapper 已失效（C++ 侧已销毁）


def _arm_pending_follow(follower) -> QTimer:
    """把跟随器放进"节流窗口 + 已挂尾部补发"的真实状态（真 QTimer.start()）。"""
    follower._follow_pending = True
    follower._follow_timer.start()
    assert follower._follow_timer.isActive() is True
    return follower._follow_timer


def test_close_stops_pending_timer_and_drops_pending_flag(tmp_path):
    """close 当场：尾部补发 timer 不再 active、pending 复位。"""
    shell, follower = _make(tmp_path)
    try:
        timer = _arm_pending_follow(follower)
        follower.close()
        assert timer.isActive() is False, "close 必须停掉待触发的尾部补发"
        assert follower._follow_pending is False
        assert follower.bubble is None
    finally:
        shell._delete_runtime_marker()


def test_close_sends_no_late_reposition(tmp_path):
    """close 后即使事件循环继续跑，也不得再有迟到的 reposition。"""
    overlay_host = _OverlayHost()
    sprite = _Sprite()
    follower = SpriteBubbleFollower(overlay_host, sprite)
    bubble = FakeBubble()
    follower.bubble = bubble
    try:
        timer = _arm_pending_follow(follower)
        follower.close()
        _deliver_deletes(timer)            # 让被删除的 timer 真的消失
        assert bubble.moves == [], "close 后不得补发最后一帧"
        # 已关闭 = 不可复用：位置回调与手动补发都必须早退
        follower._on_sprite_moved(sprite)
        follower._flush_follow()
        assert bubble.moves == []
        assert bubble.closed == 1
    finally:
        overlay_host.deleteLater()
        _deliver_deletes(overlay_host)


def test_close_is_idempotent_including_after_timer_deleted(tmp_path):
    """重复 close（含 timer 已被删除后）幂等且不抛。"""
    overlay_host = _OverlayHost()
    follower = SpriteBubbleFollower(overlay_host, _Sprite())
    follower.bubble = FakeBubble()
    try:
        timer = _arm_pending_follow(follower)
        follower.close()
        follower.close()                   # timer 仍活着时重复
        _deliver_deletes(timer)            # timer 的 C++ 对象被销毁
        follower.close()                   # wrapper 失效后重复：不得抛
        follower.close()
        assert follower.bubble is None
    finally:
        overlay_host.deleteLater()
        _deliver_deletes(overlay_host)


def test_close_releases_timer_and_follower_on_live_overlay(tmp_path):
    """overlay 存活期反复建/关跟随器：QTimer 子对象数回基线 + 旧 follower 可回收。

    「主宠提升」等路径会在同一个 overlay 上换跟随器：若 close 只 stop 不删除
    timer，Qt 信号连接会一直握着旧 follower（连带旧 sprite），逐次累积。
    """
    shell, _follower = _make(tmp_path)
    overlay = shell.overlay
    try:
        baseline = len(overlay.findChildren(QTimer))
        refs, timers = [], []
        for _ in range(3):
            f = SpriteBubbleFollower(overlay, shell.sprite)
            f.bubble = FakeBubble()
            timers.append(_arm_pending_follow(f))
            refs.append(weakref.ref(f))
            f.close()
        del f                                     # 去掉循环变量这最后一个强引用
        _deliver_deletes(*timers)
        gc.collect()

        assert len(overlay.findChildren(QTimer)) == baseline, "被删除的 timer 必须真的消失"
        assert all(ref() is None for ref in refs), "旧 follower 不得被 Qt 连接钉住"
    finally:
        shell._delete_runtime_marker()


def test_close_survives_deleted_overlay_wrapper(tmp_path):
    """overlay 已销毁（wrapper 失效）时 close 仍不抛（真实 deleteLater 路径）。"""
    overlay_host = _OverlayHost()
    follower = SpriteBubbleFollower(overlay_host, _Sprite())
    follower.bubble = FakeBubble()
    _arm_pending_follow(follower)

    overlay_host.deleteLater()
    _deliver_deletes(overlay_host)                # C++ widget 真被销毁（含其子 QTimer）

    follower.close()                              # 不得抛 RuntimeError
    follower.close()
    assert follower.bubble is None


# ---------------------------------------------------------------- P1：拖拽起止关键帧立即同步
def _mouse_event(etype, pos):
    """overlay 铺满屏幕（原点 0,0）：局部坐标 == 全局坐标，与事件构造同参。"""
    return QMouseEvent(etype, QPointF(*pos), QPointF(*pos),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)


def _drag_env(tmp_path):
    """真壳（真 OverlayWindow + 真 PetSprite）+ 记录型跟随器；假钟固定松手判定。"""
    shell, follower = _make(tmp_path)
    shell.sprite._clock = lambda: 1000.0  # 轨迹时间戳相同 → 松手静止放下（不赌真实时间）
    return shell, follower


def _anchor_of(shell, sprite) -> QRect:
    return sprite_anchor_rect_global(sprite, shell.overlay.geometry().topLeft())


def test_drag_start_forces_immediate_bubble_sync(tmp_path):
    """拖拽起步那一拍必须立即同步气泡（旧 window.py:3330 拖拽第一帧）。

    新版位置通知只在 tick 扇出里发 + follower 30Hz 限频：拖拽起止若不做任何
    动作，抓起的首帧气泡要等一次 tick + 可能一次节流窗口（最坏约 50ms）才动。
    """
    shell, follower = _drag_env(tmp_path)
    sprite, overlay = shell.sprite, shell.overlay
    try:
        bubble = follower.bubble
        # 建立扇出基线：_last_reported_rect 只在 advance 返回时写，先让 sprite
        # 真位移一 tick（走真扇出，不手工塞私有字段）
        sprite.set_velocity(QPointF(60.0, 0.0))
        overlay.tick_advance(1 / 60.0)
        sprite.set_velocity(QPointF(0.0, 0.0))
        follower._last_follow = 0.0   # 复位节流窗口（不 sleep 赌 30Hz，同几何用例）
        baseline = len(bubble.moves)

        cx, cy = sprite.rect().center().x(), sprite.rect().center().y()
        overlay.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, (cx, cy)))
        assert sprite.dragging is False, "夹具前提：按下只是点击候选"
        assert len(bubble.moves) == baseline, "点击候选期气泡不动"

        # 位移 60px ≫ DRAG_THRESHOLD×scale：这一拍升级真拖拽（朝屏内自由区拖，避开边界钳制）
        overlay.mouseMoveEvent(_mouse_event(QEvent.Type.MouseMove, (cx - 60, cy)))
        assert sprite.dragging is True, "夹具前提：已升级真拖拽"
        assert len(bubble.moves) == baseline + 1, "拖拽起步那一拍必须立即同步（不等 tick/33ms）"

        # 同步发生在 on_move 之前（本拍锚点 = 已绘制的上一帧位置）→ 下一次真实
        # 扇出必须立刻补上拖拽位，不得被强制同步重新盖上 30Hz 窗口
        overlay.tick_advance(1 / 60.0)
        assert len(bubble.moves) == baseline + 2, "下一次 tick 扇出必须立即补上拖拽位"
        assert bubble.moves[-1] == _anchor_of(shell, sprite)
        assert bubble.moves[-1] == QRect(
            overlay.geometry().topLeft() + sprite.body_rect().topLeft(),
            sprite.body_rect().size())
    finally:
        shell._delete_runtime_marker()


def test_release_forces_immediate_final_sync(tmp_path):
    """松手终位立即同步（旧 window.py:3401）——不等 tick、不等节流补发。"""
    shell, follower = _drag_env(tmp_path)
    sprite, overlay = shell.sprite, shell.overlay
    try:
        bubble = follower.bubble
        cx, cy = sprite.rect().center().x(), sprite.rect().center().y()
        overlay.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, (cx, cy)))
        # 朝屏内拖动（主宠默认停在右下角，往外拖会被 body 钳制吃掉位移）
        overlay.mouseMoveEvent(_mouse_event(QEvent.Type.MouseMove, (cx - 80, cy - 40)))
        before = _anchor_of(shell, sprite)
        moves_before = len(bubble.moves)

        overlay.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, (cx - 140, cy - 90)))
        assert sprite.dragging is False
        after = _anchor_of(shell, sprite)
        assert after != before, "夹具前提：松手把 sprite 挪到了新位置"
        assert len(bubble.moves) == moves_before + 1, "松手终位必须立即同步（不等 tick）"
        assert bubble.moves[-1] == after, "同步的必须是松手后的最终位置"
    finally:
        shell._delete_runtime_marker()


def test_click_without_drag_does_not_force_sync(tmp_path):
    """没升级真拖拽的单击不得触发强制同步（旧机同步只在拖拽分支里）。"""
    shell, follower = _drag_env(tmp_path)
    sprite, overlay = shell.sprite, shell.overlay
    try:
        bubble = follower.bubble
        cx, cy = sprite.rect().center().x(), sprite.rect().center().y()
        overlay.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, (cx, cy)))
        overlay.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, (cx + 1, cy)))
        assert sprite.dragging is False
        assert bubble.moves == []
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- P2：缩放重排 / 锚点刷新
def test_reflow_passes_new_pet_scale(tmp_path):
    """可见气泡在缩放变更后按**新** pet_scale 重排（旧 window.py:1000-1004）。

    调用点在壳层/菜单侧：``sprite_menu_facade`` 改大小（经
    ``overlay_shell.on_sprite_scale_changed``）、``overlay_shell.switch_character``
    换角色后各调一次（B6 只加方法，B7b 起两边都已接线）。
    """
    shell, follower = _drag_env(tmp_path)
    try:
        bubble = follower.bubble
        new_scale = shell.sprite.scale * 1.5
        shell.sprite.scale = new_scale

        assert follower.reflow() is True
        assert len(bubble.reflows) == 1
        anchor, pet_scale = bubble.reflows[0]
        assert pet_scale == new_scale, "必须传新 scale（泡泡画布/内边距按它重算）"
        assert anchor == _anchor_of(shell, shell.sprite)
        assert bubble.moves == [], "reflow 内部自行落位，不额外走一次 reposition"
    finally:
        shell._delete_runtime_marker()


def test_refresh_anchor_repositions_visible_bubble(tmp_path):
    """体框/角色变更后按当前锚点重定位一次（切角色换 body_box 的调用入口）。"""
    shell, follower = _drag_env(tmp_path)
    try:
        bubble = follower.bubble
        shell.sprite.set_pos(shell.sprite.pos + QPointF(37, 21))
        assert follower.refresh_anchor() is None
        assert bubble.moves == [_anchor_of(shell, shell.sprite)]
    finally:
        shell._delete_runtime_marker()


def test_forced_syncs_noop_when_hidden_or_closed(tmp_path):
    """隐态不动作（不隐态意外落位/重排）；close 后三个入口一律无操作且不抛。"""
    shell, follower = _drag_env(tmp_path)
    try:
        bubble = follower.bubble
        bubble.set_visible(False)
        follower.follow_now()
        follower.refresh_anchor()
        assert follower.reflow() is False
        assert bubble.moves == [] and bubble.reflows == []
        assert follower._follow_pending is False

        follower.close()
        follower.follow_now()
        follower.refresh_anchor()
        assert follower.reflow() is False
        assert bubble.moves == [] and bubble.reflows == []
        assert follower.bubble is None
    finally:
        shell._delete_runtime_marker()
