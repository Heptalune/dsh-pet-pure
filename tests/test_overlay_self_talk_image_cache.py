# -*- coding: utf-8 -*-
"""自言自语配图解码缓存：尺寸口径 + 重建口径（N2）聚焦回归。

背景（2026-09-29 审计）：overlay 壳的配图缓存把每张图固定缩到长边 ≤640，
但用户 ``self_talk_image_scale=146``、DPR=1.0 时实际只画 ~321×204
（``speech_bubble.py`` 的标准气泡盒 220×140 × scale）——多出来的像素纯占内存
（实测 24 张 36.8MB）。另外 ``refresh_settings → _load_self_talk_settings``
每次外部配置变更都清空并重新解码全部 24 张，哪怕图片清单和显示尺寸一个字没改。

覆盖：
1. ``self_talk_image_cache_edge`` 随「配图大小 × DPR」现算（含钳位）；
2. 预热缓存的实际长边 = 该现算值（不再是固定 640）；
3. 同签名单 refresh 不重新解码（计数桩）；尺寸签名变了才重建。

纪律：真实 QImage 解码 + 真实 OverlayShell（offscreen，假屏），只在
「数解码次数」这一 Qt 解码边界上打计数桩（返回真 QImage），不 mock 产品对象。
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QApplication

import pet.overlay_shell as overlay_shell_mod
import tests.test_overlay_self_talk as st
import tests.test_overlay_window_capabilities as cap
import tests.test_sprite_menu_facade as fac
from pet.overlay_shell import OverlayShell
from pet.pet_sprite import PetSprite

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 脚手架
def _make_shell(tmp_path, values, *, dpr=1.0):
    """st._make_shell 的 DPR 可配版本（假屏 DPR 决定缓存目标像素尺寸）。"""
    config = st.TalkConfig(tmp_path, values)
    instance = cap.CapInstance(config)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040), dpr=dpr)
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    lib = fac.RichLibrary()
    shell.lib = lib
    shell.sprite.library = lib
    shell.overlay.click_feedback = None
    return shell, config


def _write_big_png(directory, name="big.png"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    pixmap = QPixmap(2000, 1000)
    pixmap.fill(QColor(90, 120, 160))
    assert pixmap.save(str(path))
    return path


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    return predicate()


def _cached(shell, path):
    cached = shell._self_talk_image_cache.get(str(path))
    assert cached is not None and not cached.isNull(), "后台预热超时未完成"
    return cached


# ---------------------------------------------------------------- 1. 尺寸口径
def test_cache_edge_tracks_display_box_scale_and_dpr():
    """目标长边 = 显示盒长边 × 配图大小 × DPR × 余量（向上取整）。

    显示盒 = ``speech_bubble`` 的配图基准盒（220×140，用户配图大小再乘上去），
    两处共用同一份常量，避免"缓存改了显示没改"。
    """
    edge = overlay_shell_mod.self_talk_image_cache_edge
    from pet.speech_bubble import SELF_TALK_IMAGE_BOX_H, SELF_TALK_IMAGE_BOX_W

    box = max(SELF_TALK_IMAGE_BOX_W, SELF_TALK_IMAGE_BOX_H)

    assert box == 220, "基准盒常量变了：下面的期望值要跟着重算"
    assert edge(1.0, 1.0) == 242      # 220 × 1.0 × 1.0 × 1.1
    assert edge(1.5, 1.0) == 363      # 用户实测场景：只画 321×204
    assert edge(1.5, 2.0) == 726      # 2× 屏：物理像素翻倍
    assert edge(3.0, 1.0) == 726

    # 配图大小钳位 0.5..3.0（与 speech_bubble.show_image 同口径）
    assert edge(0.1, 1.0) == edge(0.5, 1.0) == 121
    assert edge(9.9, 1.0) == edge(3.0, 1.0)

    # DPR 钳位下限 1.0（读不到 DPR 时绝不按"更省内存"裁掉物理像素）
    assert edge(1.0, 0.5) == edge(1.0, 1.0)

    # 单调：两个维度都只能放大目标
    assert edge(1.0, 2.0) > edge(1.0, 1.0)
    assert edge(2.0, 1.0) > edge(1.0, 1.0)


# ---------------------------------------------------------------- 2. 预热尺寸
def test_warm_builds_cache_at_computed_edge(tmp_path):
    """2000×1000 原图按现算长边落缓存：scale=150 + DPR=2 → 726×363（非 640）。"""
    image_dir = tmp_path / "talk-images"
    path = _write_big_png(image_dir)
    shell, _config = _make_shell(
        tmp_path,
        st._talk_values(self_talk_image_dir=str(image_dir),
                        self_talk_image_scale=150),
        dpr=2.0,
    )
    try:
        assert _wait_for(lambda: shell._self_talk_image_cache)
        cached = _cached(shell, path)
        assert (cached.width(), cached.height()) == (726, 363)
    finally:
        st._cleanup(shell)


def test_warm_target_shrinks_with_scale_on_low_dpr_screen(tmp_path):
    """DPR=1 + scale=100 → 242×121：比旧固定 640 小一个数量级。"""
    image_dir = tmp_path / "talk-images"
    path = _write_big_png(image_dir)
    shell, _config = _make_shell(
        tmp_path,
        st._talk_values(self_talk_image_dir=str(image_dir),
                        self_talk_image_scale=100),
        dpr=1.0,
    )
    try:
        assert _wait_for(lambda: shell._self_talk_image_cache)
        cached = _cached(shell, path)
        assert (cached.width(), cached.height()) == (242, 121)
    finally:
        st._cleanup(shell)


def test_dpr_change_rebuilds_cache_without_config_refresh(tmp_path):
    """换屏/改缩放（几何事件重喂 DPR）后配图缓存按新物理像素重建。

    这条路径不经过 refresh_settings：DPR 变了就得重解，DPR 没变则一步不动
    （几何事件可能每拍都来，不能白刷 24 张图）。
    """
    image_dir = tmp_path / "talk-images"
    path = _write_big_png(image_dir)
    shell, _config = _make_shell(
        tmp_path,
        st._talk_values(self_talk_image_dir=str(image_dir),
                        self_talk_image_scale=100),
        dpr=1.0,
    )
    try:
        assert _wait_for(lambda: shell._self_talk_image_cache)
        assert _cached(shell, path).width() == 242
        cache_before = shell._self_talk_image_cache

        # DPR 没变：几何事件重进一次不得重建
        shell._refresh_self_talk_image_cache_for_dpr()
        assert shell._self_talk_image_cache is cache_before

        # 换到 2× 屏：目标长边翻倍，缓存必须重建
        # 屏信息归宿主持有：换屏就是换宿主那块屏（``_screen`` 是只读视图）
        shell._main_host.screen = cap.FakeScreen(
            (0, 0, 1920, 1080), (0, 0, 1920, 1040), dpr=2.0)
        shell._refresh_self_talk_image_cache_for_dpr()
        assert shell._self_talk_image_cache is not cache_before
        assert _wait_for(lambda: shell._self_talk_image_cache)
        assert _cached(shell, path).width() == 484  # 220 × 1.0 × 2.0 × 1.1
    finally:
        st._cleanup(shell)


# ---------------------------------------------------------------- 3. 重建口径
def test_same_signature_refresh_skips_redecode(tmp_path, monkeypatch):
    """同签名（清单 mtime/size + 目标尺寸都没变）refresh 不重新解码。"""
    image_dir = tmp_path / "talk-images"
    path = _write_big_png(image_dir)
    shell, config = _make_shell(
        tmp_path,
        st._talk_values(self_talk_image_dir=str(image_dir),
                        self_talk_image_scale=150),
        dpr=1.0,
    )
    try:
        assert _wait_for(lambda: shell._self_talk_image_cache)
        cache_before = shell._self_talk_image_cache

        decodes: list = []

        class CountingImage(QImage):
            def __init__(self, *args, **kwargs):
                decodes.append(args[0] if args else None)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(overlay_shell_mod, "QImage", CountingImage)

        # 与配图无关的配置变更（气泡风格/开关）：签名不变
        config.set("self_talk_bubble_style", "soft")
        shell.refresh_settings()
        assert decodes == [], "同签名的 refresh 不得重新解码配图"
        assert shell._self_talk_image_cache is cache_before

        # 目标尺寸变了（配图大小热改 150 → 250）：必须按新尺寸重建
        config.set("self_talk_image_scale", 250)
        shell.refresh_settings()
        assert shell._self_talk_image_cache is not cache_before
        assert _wait_for(lambda: decodes), "尺寸签名变化必须重新解码"
        assert _wait_for(lambda: shell._self_talk_image_cache)
        cached = _cached(shell, path)
        assert (cached.width(), cached.height()) == (605, 302)  # 220 × 2.5 × 1.1
    finally:
        st._cleanup(shell)
