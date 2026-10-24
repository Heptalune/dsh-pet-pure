# -*- coding: utf-8 -*-
"""per-sprite visible 回归（DS 审查 M14 / PHASE4_DESIGN.md:248-250 4.1b 验收项）。

设计：PetSprite 加 ``visible`` 字段（默认 True）；隐藏的 sprite 必须从**三处**
排除——逐像素命中（``sprite_at``，穿透判据同源）、位置监听 fanout、绘制
（paintEvent）；壳层 ``set_pet_visible`` 的整窗语义不变，托盘逐只子菜单接上
逐只显隐。

纪律：offscreen；同步直调事件处理器/handler，不起真实 QTimer、不 sleep。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

from PySide6.QtCore import QPoint, QPointF
from PySide6.QtWidgets import QApplication

import tests.test_sprite_menu_facade as fac
from tests.test_overlay_dead_switches import _make_shell
from tests.test_overlay_window import FakeSprite

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- PetSprite
def _wait_until(predicate, timeout: float = 3.0) -> bool:
    """宽预算轮询等托盘命令落地（托盘条目一律「菜单先关、回调延后」）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _make_pet_sprite():
    from pet.pet_sprite import PetSprite

    lib = fac.RichLibrary()
    sprite = PetSprite(lib, pos=QPointF(50, 50), scale=0.5)
    sprite.bind_clip("idle1")
    sprite._rebuild_pixmap()
    return sprite


def test_pet_sprite_visible_defaults_true_and_toggle_reports_dirty():
    """默认可见；切隐藏走既有脏矩形通道（sprite 不是窗口，得自己擦残留）。"""
    sprite = _make_pet_sprite()
    assert sprite.visible is True

    reports: list = []
    sprite._dirty_cb = lambda old, new: reports.append((old, new))
    sprite.set_visible(False)

    assert sprite.visible is False
    assert len(reports) == 1
    old, new = reports[0]
    assert old == new == sprite.paint_bounds()   # 原地区域上报 = 擦除

    sprite.set_visible(False)                    # 幂等：不再上报
    assert len(reports) == 1
    sprite.set_visible(True)
    assert sprite.visible is True
    assert len(reports) == 2


# ---------------------------------------------------------------- OverlayWindow 三处排除
def test_hidden_sprite_excluded_from_hit_and_click_through():
    """命中与穿透判据：隐藏 sprite 不参与逐像素联合命中。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (64, 64))
    overlay.add_sprite(sprite)

    assert overlay.sprite_at(QPoint(4, 4)) is sprite
    assert overlay._is_transparent_at(QPoint(4, 4)) is False

    sprite.visible = False
    assert overlay.sprite_at(QPoint(4, 4)) is None
    assert overlay._is_transparent_at(QPoint(4, 4)) is True

    sprite.visible = True
    assert overlay.sprite_at(QPoint(4, 4)) is sprite


def test_hidden_sprite_excluded_from_position_fanout():
    """位置 fanout：隐藏 sprite 的 rect 变化不再触发跟随回调。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    sprite = FakeSprite((10, 10), (40, 40), movable=True)
    sprite.velocity = QPointF(100, 0)
    overlay.add_sprite(sprite)
    seen: list = []
    overlay.add_position_listener(sprite, seen.append)

    overlay._on_tick(dt=0.1)
    assert seen == [sprite]

    sprite.visible = False
    seen.clear()
    sprite.frame_dirty = True
    overlay._on_tick(dt=0.1)
    assert seen == [], "隐藏 sprite 不得进位置 fanout"


def test_hidden_sprite_excluded_from_paint():
    """绘制：隐藏 sprite 不画（脏区域照旧被擦除）。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (64, 64))
    sprite.visible = False
    overlay.add_sprite(sprite)

    overlay.grab()
    assert sprite.paint_calls == 0

    sprite.visible = True
    overlay.grab()
    assert sprite.paint_calls == 1


# ---------------------------------------------------------------- 壳：整窗语义与逐只
def test_shell_set_sprite_visible_keeps_window_semantics(tmp_path):
    """逐只显隐只改 sprite 标志；整窗显隐（set_pet_visible）语义不变。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        sprite = shell.sprite
        assert shell._sprite_visible(sprite) is True

        shell.set_sprite_visible(sprite, False)
        assert sprite.visible is False
        assert shell._sprite_visible(sprite) is False

        shell.toggle_sprite_visible(sprite)
        assert sprite.visible is True

        # 整窗显隐：不动 per-sprite 标志
        shell.overlay.show()
        shell.set_pet_visible(False)
        assert shell.overlay.isVisible() is False
        assert sprite.visible is True
        shell.set_pet_visible(True)
        assert shell.overlay.isVisible() is True
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_tray_menu_has_per_sprite_visibility_toggle(tmp_path):
    """托盘逐只子菜单：勾选态 = 该 sprite 当前可见性；重开菜单同步刷新。"""
    shell, lib = _make_shell(tmp_path)
    try:
        second = shell._sprite_factory(lib, QPointF(0, 0), 1.0)
        shell.overlay.add_sprite(second)
        shell._spawned.append(second)
        shell._spawned_slots[second] = 3
        shell._refresh_tray_menu()

        menu = shell._tray_menu
        assert menu is not None
        sub = next(a.menu() for a in menu.actions()
                   if a.text().startswith("小肥鱼"))
        action = next(a for a in sub.actions() if a.text() == "显示这只")
        assert action.isCheckable() is True
        assert action.isChecked() is True
        assert shell._sprite_visible(second) is True

        action.trigger()                       # 取消勾选 = 隐藏这只
        assert shell._sprite_visible(second) is True, \
            "菜单还开着，逐只显隐不许内联执行"
        assert _wait_until(lambda: second.visible is False), \
            "菜单关闭后必须把这只藏起来"
        assert shell.sprite.visible is True    # 只影响被点的那只

        # 弹出前同步：外部改了标志也能反映到勾选态
        second.visible = True
        shell._sync_tray_pet_visibility()
        assert action.isChecked() is True
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_click_route_ignores_hidden_sprite(tmp_path):
    """隐藏 sprite 不参与鼠标路由（按下不建立 grab）。"""
    from PySide6.QtCore import QEvent

    from tests.test_overlay_dead_switches import _hit_pos, _mouse_event

    shell, _lib = _make_shell(tmp_path)
    try:
        sprite = shell.sprite
        pos = _hit_pos(sprite)
        shell.set_sprite_visible(sprite, False)
        press = _mouse_event(QEvent.Type.MouseButtonPress, pos)
        shell.overlay.mousePressEvent(press)
        assert shell.overlay._mouse_grab is None
        assert not press.isAccepted()
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_sprite_without_visible_field_treated_as_visible():
    """鸭式 sprite（无 visible 字段）沿用既有语义：恒可见（灰假件不被打红）。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (64, 64))     # test_overlay_window.FakeSprite 无 visible
    assert not hasattr(sprite, "visible")
    overlay.add_sprite(sprite)
    assert overlay.sprite_at(QPoint(4, 4)) is sprite
    overlay.grab()
    assert sprite.paint_calls == 1


# ---------------------------------------------------------------- O3 隐藏期停播放节拍
class _FakeClock:
    """可推进的假钟（档位滞回用；壳的驱动器不起真表、不 sleep 赌时序）。

    起点取 ``time.monotonic()``：驱动器在 ``_build``/``showEvent`` 里已经用真钟
    记过 kinetic 时间戳，注入一个从 0 起的小值会让"kinetic 尾巴是否过期"的
    算术失真。
    """

    def __init__(self, now=None):
        self.now = time.monotonic() if now is None else float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class PausableStubSprite(FakeSprite):
    """鸭子 sprite + 可见性/播放节拍记录面（壳层接线的观测点）。"""

    def __init__(self, pos=(0, 0), size=(32, 32)):
        super().__init__(pos, size)
        self.visible = True
        self.pause_calls = 0
        self.resume_calls = 0

    def set_visible(self, visible):
        self.visible = bool(visible)

    def pause_clip(self):
        self.pause_calls += 1

    def resume_clip(self):
        self.resume_calls += 1


class WarmRecorder:
    """记录 pause_warm/resume_warm 的库替身（成对配对的观测点）。"""

    def __init__(self):
        self.pauses = 0
        self.resumes = 0

    def pause_warm(self):
        self.pauses += 1

    def resume_warm(self):
        self.resumes += 1


def _real_frameseq_sprite(tmp_path, name="idle1", count=12):
    """真 PetSprite + 真 FrameSeqClip（现场生成的 webp 帧素材）。"""
    from tests.test_frameseq_clip import _make_frames

    from pet.frameseq_clip import FrameSeqClip
    from pet.pet_sprite import PetSprite

    frames_dir = tmp_path / name
    _make_frames(frames_dir, count=count)
    clip = FrameSeqClip(frames_dir)
    lib = fac.RichLibrary()
    lib._clips[name] = clip
    sprite = PetSprite(lib, pos=QPointF(50, 50), scale=0.5)
    return sprite, clip


def _drive_frames(clip, count):
    """同步推 count 帧（真 clip 的 _advance + 事件泵等在途预取交付）。"""
    from tests.test_frameseq_clip import _pump_until

    for _ in range(count):
        target = clip.currentFrameNumber() + 1
        clip._advance()
        _pump_until(lambda: clip.currentFrameNumber() == target)


def test_pause_clip_stops_timer_and_resume_continues_without_rewind(tmp_path):
    """pause_clip 只停播放节拍；resume_clip 从暂停处续播（绝不回第 0 帧）。"""
    sprite, clip = _real_frameseq_sprite(tmp_path)
    try:
        assert sprite.bind_clip("idle1") is True
        from tests.test_frameseq_clip import _pump_until
        _pump_until(lambda: clip.currentImage() is not None)
        clip._timer.stop()                     # 定序：测试自己推帧，不赌真走时
        _drive_frames(clip, 3)
        assert clip.currentFrameNumber() == 3

        jumps: list = []
        real_jump = clip.jumpToFrame
        clip.jumpToFrame = lambda frame: (jumps.append(frame), real_jump(frame))[1]

        sprite.pause_clip()
        assert clip._timer.isActive() is False, "暂停 = 停 clip 自身定时器"
        assert clip.currentFrameNumber() == 3, "暂停不得清播放位置"
        assert clip.currentImage() is not None, "暂停不得清显示图"

        sprite.resume_clip()
        assert clip._timer.isActive() is True, "恢复 = 重开定时器"
        assert clip.currentFrameNumber() == 3, "恢复不得回落第 0 帧"
        assert jumps == [], "恢复绝不许 jumpToFrame(0)/restart_clip"

        clip._timer.stop()                     # 继续定序推帧
        _drive_frames(clip, 1)
        assert clip.currentFrameNumber() == 4, "续播：从暂停帧往下一帧走，不是从 0 重放"
    finally:
        clip.close()


def test_pause_clip_is_idempotent_and_noop_without_clip(tmp_path):
    """无 clip / 重复暂停：幂等 no-op（隐藏与恢复可能被重复触发）。"""
    from pet.pet_sprite import PetSprite

    bare = PetSprite(fac.RichLibrary(), pos=QPointF(0, 0), scale=0.5)
    bare.pause_clip()                          # 无 clip：不许抛
    bare.resume_clip()

    sprite, clip = _real_frameseq_sprite(tmp_path)
    try:
        sprite.bind_clip("idle1")
        sprite.pause_clip()
        sprite.pause_clip()
        assert clip._timer.isActive() is False
        sprite.resume_clip()
        sprite.resume_clip()
        assert clip._timer.isActive() is True
    finally:
        clip.close()


def test_rebind_while_paused_stays_paused(tmp_path):
    """隐藏中行为链换绑 clip：新 clip 必须立刻回到暂停（隐藏期零推进）。"""
    sprite, clip = _real_frameseq_sprite(tmp_path)
    try:
        sprite.bind_clip("idle1")
        sprite.pause_clip()
        assert clip._timer.isActive() is False

        sprite.bind_clip("idle1")              # 行为链换绑（隐藏中照常发生）

        assert clip._timer.isActive() is False, "隐藏中换绑不得把播放节拍放行"
        sprite.resume_clip()
        assert clip._timer.isActive() is True
    finally:
        clip.close()


def test_unbind_while_paused_does_not_poison_clip_for_next_bind(tmp_path):
    """暂停期被换绑掉的 clip 不得把暂停标记滞留到下一次绑定（实机冻结根因）。

    实机链：隐藏/挂起 → ``pause_clip``（当前 clip 暂停）→ 隐藏期行为链换绑
    （``bind_clip`` 只 stop 旧 clip，它的 ``_paused`` 滞留库缓存）→ 恢复可见
    只续当前 clip → 旧 clip 下次 ``start()`` 看到滞留 ``_paused`` 不起定时器
    = 画面永久冻在首帧（拖拽悬空/走路动画变静态图、挂机回来三只全冻但还在
    移动，全是这一条链）。
    """
    from tests.test_frameseq_clip import _make_frames

    from pet.frameseq_clip import FrameSeqClip

    sprite, clip_a = _real_frameseq_sprite(tmp_path, name="idle1")
    frames_b = tmp_path / "idle2"
    _make_frames(frames_b, count=12)
    clip_b = FrameSeqClip(frames_b)
    sprite.library._clips["idle2"] = clip_b
    try:
        assert sprite.bind_clip("idle1") is True
        sprite.pause_clip()                    # 隐藏/挂起：当前 clip 暂停
        sprite.bind_clip("idle2")              # 隐藏期换绑 → idle1 的 _paused 滞留
        sprite.resume_clip()                   # 恢复可见：只续当前 clip（idle2）
        assert clip_b._timer.isActive() is True

        assert sprite.bind_clip("idle1") is True   # 日后轮转回 idle1
        assert clip_a._timer.isActive() is True, (
            "被换绑时滞留暂停的 clip 再绑定必须正常起播（否则画面冻在首帧）")
    finally:
        clip_a.close()
        clip_b.close()


def test_restart_clip_clears_stale_pause_so_timer_revives(tmp_path):
    """restart_clip 与 bind_clip 同款对称收口：滞留的 ``_paused`` 必须清掉。

    毒化链：``pause()`` 置 ``_paused`` 并停表 → ``stop()`` 只停表、**不清**
    ``_paused``（FrameSeqClip 两个入口的语义差）→ ``restart_clip`` 的 ``start()``
    看到滞留标记不起定时器 = 圈末续播之后画面冻在首帧。sprite 未暂停 = 暂停
    契约不成立，重启路径同样要清掉（未暂停的 clip 上 ``resume()`` 是 no-op）。
    """
    from tests.test_frameseq_clip import _pump_until

    sprite, clip = _real_frameseq_sprite(tmp_path)
    try:
        assert sprite.bind_clip("idle1") is True
        _pump_until(lambda: clip.currentImage() is not None)
        assert clip._timer.isActive() is True

        clip.pause()                           # 毒化：暂停标记滞留
        clip.stop()                            # stop 不清 _paused（半暂停态）
        assert clip._paused is True
        assert clip._timer.isActive() is False

        assert sprite.restart_clip() is True

        assert clip._timer.isActive() is True, "滞留暂停必须清掉，播放节拍要复活"
    finally:
        clip.close()


def test_shell_set_sprite_visible_pauses_and_resumes_that_sprite_clip(tmp_path):
    """壳层逐只显隐：隐藏 → 该只 pause_clip；恢复 → resume_clip（只影响这一只）。

    "恢复"以**窗口真的可见**为前提（恢复路径的有效可播闸门）：本测试先 show，
    否则它验的是"整窗没显示也在解码"这条反例。
    """
    shell, _lib = _make_shell(tmp_path)
    try:
        other = PausableStubSprite()
        shell.overlay.add_sprite(other)
        shell.overlay.show()

        shell.set_sprite_visible(other, False)

        assert other.pause_calls == 1
        assert other.resume_calls == 0

        shell.set_sprite_visible(other, True)

        assert other.resume_calls == 1
        assert other.pause_calls == 1
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_window_hide_pauses_every_sprite_clip_and_show_resumes(tmp_path):
    """整窗隐藏/全屏避让：逐 sprite pause_clip；显示时逐 sprite resume_clip。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        first = PausableStubSprite()
        second = PausableStubSprite()
        shell.overlay.add_sprite(first)
        shell.overlay.add_sprite(second)
        shell.overlay.show()

        shell.set_pet_visible(False)

        assert (first.pause_calls, second.pause_calls) == (1, 1)
        assert not shell.overlay.isVisible()

        shell.set_pet_visible(True)

        assert (first.resume_calls, second.resume_calls) == (1, 1)

        # 全屏避让走同一条契约（隐藏 → 恢复）
        shell._on_fullscreen_changed(True)
        assert (first.pause_calls, second.pause_calls) == (2, 2)
        shell._on_fullscreen_changed(False)
        assert (first.resume_calls, second.resume_calls) == (2, 2)
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_window_show_does_not_resume_individually_hidden_sprite(tmp_path):
    """整窗显示不等于逐只隐藏的那只该醒：它的播放节拍保持暂停。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        hidden = PausableStubSprite()
        shown = PausableStubSprite()
        shell.overlay.add_sprite(hidden)
        shell.overlay.add_sprite(shown)

        shell.set_sprite_visible(hidden, False)
        assert hidden.pause_calls == 1

        shell.set_pet_visible(False)
        shell.set_pet_visible(True)

        assert shown.resume_calls == 1
        assert hidden.resume_calls == 0, "隐藏那只不因整窗显示而恢复"
        assert shell._sprite_visible(hidden) is False
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 恢复路径的暂停所有权
def test_sprite_show_while_window_hidden_keeps_real_clip_paused(tmp_path):
    """整窗隐藏中「显示这只」不得放行：另一个暂停所有者（整窗）还没放手。

    真 FrameSeqClip 观测点：定时器在不可见期重新跑起来 = 按帧率白烧 CPU
    （O3 契约破坏）。
    """
    shell, _lib = _make_shell(tmp_path)
    try:
        sprite, clip = _real_frameseq_sprite(tmp_path)
        shell.overlay.add_sprite(sprite)
        assert sprite.bind_clip("idle1") is True
        shell.overlay.show()

        shell.set_sprite_visible(sprite, False)     # 托盘先逐只藏起
        shell.set_pet_visible(False)                # 再整窗隐藏
        assert clip._timer.isActive() is False

        shell.set_sprite_visible(sprite, True)      # 托盘勾回「显示这只」
        assert sprite.visible is True, "逻辑上仍是可见宠物（只有窗口不可见）"
        assert clip._timer.isActive() is False, "整窗隐藏中不许放行播放节拍"

        shell.set_pet_visible(True)                 # 整窗恢复：照旧放行
        assert clip._timer.isActive() is True
    finally:
        clip.close()
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_all_clips_resume_denied_while_suspended(tmp_path):
    """挂起中整窗恢复路径不得放行：锁屏未解除时 clip 不许重新解码。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        sprite = PausableStubSprite()
        shell.overlay.add_sprite(sprite)
        shell.overlay.show()

        shell._on_suspend_changed(True, "session_lock")
        assert shell.driver.suspended is True

        shell._set_all_clips_paused(False)          # 全屏避让解除 / 整窗显示路径
        assert sprite.resume_calls == 0, "挂起未解除：不许放行播放节拍"

        shell._on_suspend_changed(False, "session_unlock")
        assert sprite.resume_calls == 1, "正常解锁照旧放行"
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_fullscreen_release_while_suspended_keeps_clips_paused(tmp_path):
    """锁屏中全屏避让解除（overlay.show() 照走）同样不得放行。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        sprite = PausableStubSprite()
        shell.overlay.add_sprite(sprite)
        shell.overlay.show()

        shell._on_fullscreen_changed(True)          # 全屏避让：隐藏 + 停节拍
        assert sprite.pause_calls == 1
        shell._on_suspend_changed(True, "session_lock")

        shell._on_fullscreen_changed(False)         # 全屏退出：窗口已 show
        assert shell.overlay.isVisible() is True
        assert sprite.resume_calls == 0, "锁屏未解除：全屏避让解除不得放行"

        shell._on_suspend_changed(False, "session_unlock")
        assert sprite.resume_calls == 1
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_window_visibility_pairs_pause_and_resume_warm_for_all_libraries(tmp_path):
    """整窗隐藏/显示 → 主库与全部子宠库 pause_warm/resume_warm 成对。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        main, spawned = WarmRecorder(), WarmRecorder()
        spawned_sprite = PausableStubSprite()
        shell.lib = main
        shell._spawned_libs[spawned_sprite] = spawned

        shell.overlay.show()
        shell.set_pet_visible(False)
        assert (main.pauses, spawned.pauses) == (1, 1), "隐藏必须逐库停预热"
        shell.set_pet_visible(True)
        assert (main.resumes, spawned.resumes) == (1, 1), "显示必须逐库补预热"

        # 入口二：全屏避让
        shell._on_fullscreen_changed(True)
        assert (main.pauses, spawned.pauses) == (2, 2)
        shell._on_fullscreen_changed(False)
        assert (main.resumes, spawned.resumes) == (2, 2)

        # 入口三：锁屏/挂起（只降档，不隐藏窗口）
        shell._on_suspend_changed(True, "session_lock")
        assert (main.pauses, spawned.pauses) == (3, 3)
        assert shell.overlay.isVisible() is True, "锁屏不 hide overlay"
        shell._on_suspend_changed(False, "session_unlock")
        assert (main.resumes, spawned.resumes) == (3, 3)
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_shell_set_sprite_visible_notifies_driver_tier(tmp_path):
    """逐只藏光：壳层必须通知驱动器（零可见 sprite → 目标 T3）。"""
    from pet.tick_governor import TIER_OCCLUDED

    shell, _lib = _make_shell(tmp_path)
    try:
        shell.overlay.show()
        clock = _FakeClock()
        shell.driver._governor._clock = clock
        clock.advance(1.0)                     # 先过 start/show 的 kinetic 尾巴
        shell.set_sprite_visible(shell.sprite, False)
        clock.advance(1.0)                     # 过降档滞回（DOWNGRADE_HOLD_MS=800）
        shell.set_sprite_visible(shell.sprite, False)

        assert shell.driver.applied_tier == TIER_OCCLUDED
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_hiding_one_of_two_sprites_keeps_tier_running(tmp_path):
    """只剩一只可见时不许降档（零可见 sprite 才是 T3 的判据）。"""
    from pet.tick_governor import TIER_OCCLUDED

    shell, _lib = _make_shell(tmp_path)
    try:
        shell.overlay.show()
        clock = _FakeClock()
        shell.driver._governor._clock = clock
        other = PausableStubSprite()
        shell.overlay.add_sprite(other)
        clock.advance(1.0)

        shell.set_sprite_visible(other, False)
        clock.advance(2.0)
        shell.set_sprite_visible(shell.sprite, True)   # 触发一次重评

        assert shell.driver.applied_tier != TIER_OCCLUDED
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()
