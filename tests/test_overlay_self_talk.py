# -*- coding: utf-8 -*-
"""overlay 拓扑「自言自语族」聚焦回归（周期气泡 / 点击台词 / 配图）。

背景：这套功能在旧架构里的唯一宿主是 ``PetWindow``（self_talk 状态装配 +
周期定时器 + 点击分支 + 朗读注入）。overlay 拓扑下 ``PetWindow`` 不构造，
``pet/overlay_shell.py`` 的 sprite 壳成了唯一宿主——不接线就整族静默失效
（气泡跟随器本身能出泡，见本文件判别实验结论）。

覆盖：
1. ``_init_self_talk`` 状态装配 + 周期定时器：timeout → 气泡收到文本，
   并按 ``after_display`` 重排（+显示时长）；
2. 点击 sprite → ``click_show_self_talk`` 出泡且重排定时器；
3. 逐动画台词：``click_talk_texts_for`` 绑定过的动画名出绑定文本，
   并把实际显示的文本交给朗读通道；
4. 显隐对称：隐藏期 timer 停、恢复重排；
5. 配图分支：命中图片时走 ``SpriteBubbleFollower.show_image``（刀 1）。

纪律：同步直调（信号 emit / handler 直调，不 sleep 赌时序）；假屏 + 真实
PetSprite 复用既有测试件；全部 offscreen。
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

import tests.test_overlay_window_capabilities as cap
import tests.test_sprite_menu_facade as fac
from pet.overlay_shell import OverlayShell
from pet.pet_sprite import PetSprite

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 脚手架
class TalkConfig(cap.CapConfig):
    """CapConfig + 点击动画台词绑定表（``Config.click_talk_texts_for`` 的最小等价物）。"""

    def __init__(self, tmp_path, values=None, bindings=None):
        super().__init__(tmp_path, values)
        self._bindings = dict(bindings or {})

    def click_talk_texts_for(self, character_id, click_name):
        return list(self._bindings.get(str(click_name)) or [])


def _talk_values(**over):
    """自言自语最小确定性配置：间隔钉死 5s、时长 1s（after_display 可算准）。"""
    values = {
        "self_talk_enabled": True,
        "self_talk_texts": ["自言自语台词"],
        "self_talk_duration_seconds": 1.0,
        "self_talk_min_interval": 5.0,
        "self_talk_max_interval": 5.0,
        "self_talk_image_chance": 0,
    }
    values.update(over)
    return values


def _make_shell(tmp_path, values=None, *, bindings=None):
    """真实 PetSprite + RichLibrary 的壳（点击链路需要 click 素材池）。"""
    config = TalkConfig(tmp_path, values, bindings)
    instance = cap.CapInstance(config)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    lib = fac.RichLibrary()
    shell.lib = lib
    shell.sprite.library = lib
    # 点击音效有独立用例（test_sprite_sound.py 的 click_feedback 接线）；这里关掉，
    # 否则点击路径投递的 0ms 延迟播放队列会跨用例泄漏（被下一条用例的 play_sound
    # 打桩收走，变成伪失败）。
    shell.overlay.click_feedback = None
    return shell, config


def _cleanup(shell) -> None:
    shell._self_talk_timer.stop()
    shell._delete_runtime_marker()


# ---------------------------------------------------------------- 1. 周期气泡
def test_periodic_timer_timeout_shows_bubble_and_reschedules(tmp_path):
    """周期定时器到点 → 气泡出文本；显示后按 after_display 重排（+时长）。"""
    shell, _config = _make_shell(tmp_path, _talk_values())
    try:
        shell.overlay.show()
        # _init_self_talk 末尾即排程（window.py:740）；5s 间隔钉死可精确断言
        assert shell._self_talk_timer.isActive() is True
        assert shell._self_talk_timer.interval() == 5000

        shell._self_talk_timer.timeout.emit()  # 真实信号 → on_self_talk_timeout

        assert shell._speech_bubble is not None
        assert shell._speech_bubble._raw_text == "自言自语台词"
        assert shell._last_self_talk_text == "自言自语台词"
        # 已展示 → 下一次排在 max(1000, (5 + 1) * 1000)
        assert shell._self_talk_timer.interval() == 6000
    finally:
        _cleanup(shell)


def test_self_talk_disabled_does_not_arm_timer(tmp_path):
    """总开关关闭时不排程（周期气泡是独立开关，不能只看文本池非空）。"""
    shell, _config = _make_shell(
        tmp_path, _talk_values(self_talk_enabled=False))
    try:
        shell.overlay.show()
        assert shell._self_talk_timer.isActive() is False
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- 4. 显隐对称
def test_hide_stops_timer_and_show_reschedules(tmp_path):
    """隐藏期 timer 停（省电、防止对不可见壳冒泡）；恢复显示重排。"""
    shell, _config = _make_shell(tmp_path, _talk_values())
    try:
        shell.overlay.show()
        assert shell._self_talk_timer.isActive() is True

        shell.set_pet_visible(False)
        assert shell._self_talk_timer.isActive() is False

        shell.set_pet_visible(True)
        assert shell._self_talk_timer.isActive() is True
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- 5. 配图分支
def _write_png(directory, name="angry.png"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    pixmap = QPixmap(48, 32)
    pixmap.fill(QColor("red"))
    assert pixmap.save(str(path))
    return path


def test_image_branch_reaches_follower_show_image(tmp_path):
    """出图概率 100% + 缓存已热 → 走刀 1 的 ``show_image``（而不是文本分支）。

    慢帧修复后配图只走 worker 预热缓存（GUI 零磁盘税）；冷缓存回退文本
    是设计行为（另有专测）。这里先等后台预热把缓存填上（条件等待，不赌
    固定 sleep），再断言稳态出图路径。
    """
    image_dir = tmp_path / "talk-images"
    _write_png(image_dir)
    shell, _config = _make_shell(tmp_path, _talk_values(
        self_talk_image_dir=str(image_dir),
        self_talk_image_chance=100,
        self_talk_image_scale=150,
    ))
    try:
        shell.overlay.show()
        assert shell._self_talk_images, "配置的图片目录必须装进载荷"
        assert shell._self_talk_image_scale == 1.5

        deadline = time.monotonic() + 10.0
        while (not getattr(shell, "_self_talk_image_cache", {})
                and time.monotonic() < deadline):
            QApplication.instance().processEvents()
            time.sleep(0.02)
        assert shell._self_talk_image_cache, "后台配图预热超时未完成"

        assert shell._show_random_self_talk() is True
        assert shell._speech_bubble is not None
        assert shell._speech_bubble._content_kind == "image"
        assert shell._speech_bubble._image_scale == 1.5
        # 图片没有可朗读文本：显式记 None（点击路径据此保持安静）
        assert shell._last_self_talk_text is None
    finally:
        _cleanup(shell)


def test_deleted_absolute_image_dir_degrades_to_text(tmp_path):
    """用户删掉的外部图片目录不再回退内置彩蛋池（window.py:123 语义）。"""
    shell, _config = _make_shell(tmp_path, _talk_values(
        self_talk_image_dir=str(tmp_path / "gone"),
        self_talk_image_chance=100,
    ))
    try:
        assert shell._self_talk_images == []
        shell.overlay.show()
        assert shell._show_random_self_talk() is True
        assert shell._speech_bubble._raw_text == "自言自语台词"
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- 2/3. 点击链路
def _click_sprite(shell):
    """走真实 overlay 鼠标路由：同一位置 press→release = 单击 sprite。"""
    sprite = shell.sprite
    center = QPointF(sprite.rect().center())
    shell.overlay._mouse_grab = sprite
    shell.overlay._press_pos = QPointF(center)
    event = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(center), QPointF(center),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier)
    shell.overlay.mouseReleaseEvent(event)


def test_click_shows_self_talk_and_reschedules(tmp_path):
    """点击 sprite → 出气泡 + after_display 重排定时器（window.py:3370-3377 语义）。"""
    shell, _config = _make_shell(
        tmp_path, _talk_values(click_show_self_talk=True))
    try:
        shell.overlay.show()
        assert shell.behavior.anim_of(shell.sprite) is None  # 尚未接管

        _click_sprite(shell)

        assert shell._speech_bubble is not None
        assert shell._speech_bubble._raw_text == "自言自语台词"
        assert shell._self_talk_timer.interval() == 6000  # after_display=True
    finally:
        _cleanup(shell)


def test_click_uses_bound_talk_text_for_current_anim_and_speaks(tmp_path):
    """逐动画台词：绑定过的 click 动画名出绑定文本，并把同一句交给朗读通道。"""
    shell, _config = _make_shell(
        tmp_path,
        _talk_values(click_show_self_talk=True, self_talk_speak_enabled=True),
        bindings={"click1": ["绑定台词"]},
    )
    spoken: list[str] = []
    shell.on_self_talk_speak = spoken.append
    try:
        shell.overlay.show()
        _click_sprite(shell)

        assert shell.behavior.anim_of(shell.sprite) == "click1"  # 点击 clip 已绑定
        assert shell._speech_bubble._raw_text == "绑定台词"
        assert spoken == ["绑定台词"]  # 听到的 == 看到的
    finally:
        _cleanup(shell)


def test_click_without_binding_falls_back_to_random_self_talk(tmp_path):
    """未绑定当前动画 → 回退全局随机自言自语（window_alerts.py:474-477）。"""
    shell, _config = _make_shell(
        tmp_path, _talk_values(click_show_self_talk=True))
    try:
        shell.overlay.show()
        _click_sprite(shell)
        assert shell._speech_bubble._raw_text == "自言自语台词"
    finally:
        _cleanup(shell)


def test_click_with_self_talk_off_keeps_bubble_quiet(tmp_path):
    """点击自言自语关闭 → 点击只出动画，不出气泡也不排程。"""
    shell, _config = _make_shell(
        tmp_path, _talk_values(self_talk_enabled=False, click_show_self_talk=False))
    try:
        shell.overlay.show()
        _click_sprite(shell)
        assert shell._self_talk_timer.isActive() is False
        assert getattr(shell._speech_bubble, "_raw_text", "") != "自言自语台词"
    finally:
        _cleanup(shell)


def test_click_show_balance_routes_to_shell_show_balance(tmp_path):
    """click_show_balance 优先：转 ``AppShell.show_balance``（window.py:3370-3371）。"""
    shell, _config = _make_shell(
        tmp_path, _talk_values(click_show_balance=True, click_show_self_talk=True))
    calls: list = []
    shell._instance.shell = type(
        "_Shell", (), {"show_balance": lambda self, parent=None: calls.append(parent)})()
    try:
        shell.overlay.show()
        _click_sprite(shell)
        # 余额落点 = **被点 sprite 的 pet 形宿主**（绑定发起槽，不是壳本身）：
        # slot0 关闭后迟到的余额不会再贴到子宠身上；宿主把 show_bubble 转给
        # 这一只自己的气泡链（旧逐窗架构里"谁被点谁冒泡"的等价物）。
        assert calls == [shell._sprite_host(shell.sprite)]
        assert calls[0] is not shell
        assert shell._self_talk_timer.interval() == 5000  # 走余额分支，不重排自言自语
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- 7. 配置热改
def test_refresh_settings_hot_reloads_self_talk_fields(tmp_path):
    """运行期改 self_talk 开关经 refresh_settings 即生效（不等重启）。"""
    shell, config = _make_shell(tmp_path, _talk_values())
    try:
        shell.overlay.show()
        assert shell._self_talk_timer.isActive() is True

        config.set("self_talk_enabled", False)
        shell.refresh_settings()
        assert shell._self_talk_enabled is False
        assert shell._self_talk_timer.isActive() is False  # 关掉即停表

        config.set("self_talk_enabled", True)
        shell.refresh_settings()
        assert shell._self_talk_timer.isActive() is True  # 打开即重排

        config.set("click_show_self_talk", True)
        shell.refresh_settings()
        assert shell.click_show_self_talk is True
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- 8. 图片路径慢帧归因
def test_image_branch_uses_warm_cache_no_gui_disk_work(tmp_path):
    """配图走 worker 预热缓存（GUI 零磁盘/解码税）；is_file 剔除有 60s TTL。"""
    import time as _time

    shell, _config = _make_shell(tmp_path, _talk_values(self_talk_image_chance=100))
    try:
        img_path = _write_png(tmp_path / "imgs")
        shell._self_talk_images = [img_path]
        shell._self_talk_image_cache = {str(img_path): QPixmap(str(img_path)).toImage()}
        shell._self_talk_images_checked_at = _time.monotonic()  # TTL 内不再 stat

        calls: dict = {}
        follower = shell._bubble_follower
        original = follower.show_image
        follower.show_image = lambda path, ms, image_scale=1.0, pixmap=None: (
            calls.update(path=path, pixmap=pixmap), True)[1]

        assert shell._show_random_self_talk() is True
        assert calls["path"] == img_path
        assert calls["pixmap"] is not None and not calls["pixmap"].isNull()

        follower.show_image = original
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_image_branch_cache_miss_falls_back_to_text(tmp_path):
    """缓存未命中（decode 在飞）→ 回退文本气泡，绝不在 GUI 同步读图。"""
    shell, _config = _make_shell(tmp_path, _talk_values(self_talk_image_chance=100))
    try:
        img_path = _write_png(tmp_path / "imgs")
        shell._self_talk_images = [img_path]
        shell._self_talk_image_cache = {}  # 未命中

        calls: dict = {}
        follower = shell._bubble_follower
        follower.show_image = lambda *a, **k: (calls.update(image=True), True)[1]
        shell._show_bubble_text = lambda text, ms: (
            calls.update(text=text), True)[1]

        assert shell._show_random_self_talk() is True
        assert "image" not in calls          # 没有同步出图
        assert calls.get("text") == "自言自语台词"
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_warm_self_talk_images_prescales_big_images(tmp_path):
    """配图缓存按**实际绘制盒**（显示盒 × 配图大小 × DPR × 余量）预缩放：
    24 张原图解码 = 114MB 的内存回压（任务管理器实锤）。旧实现固定长边
    ≤640，用户小尺寸/1× 屏时多存一倍以上（实测 36.8MB）；改成现算后只多留
    1.1 倍余量（N2）。"""
    import time as _time

    shell, _config = _make_shell(tmp_path, _talk_values(self_talk_image_chance=100))
    try:
        big = QPixmap(2000, 1000)
        big.fill(QColor(90, 120, 160))
        d = tmp_path / "big-imgs"
        d.mkdir()
        path = d / "big.png"
        assert big.save(str(path))
        shell._self_talk_images = [path]
        shell._self_talk_image_cache = {}

        shell._warm_self_talk_images()
        deadline = _time.monotonic() + 10.0
        while str(path) not in shell._self_talk_image_cache \
                and _time.monotonic() < deadline:
            QApplication.instance().processEvents()
            _time.sleep(0.02)

        cached = shell._self_talk_image_cache[str(path)]
        assert max(cached.width(), cached.height()) == shell._self_talk_image_cache_edge()
        assert not cached.isNull()
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()
