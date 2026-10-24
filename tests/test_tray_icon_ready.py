# -*- coding: utf-8 -*-
"""托盘图标可用性（v4.2.1 用户反馈「托盘图标消失了」）回归。

根因：``_build_tray`` 在 ``win.show()`` 之后立刻取 ``QIcon(win.icon_pixmap())``，
而 4.2.1 的 da8f291 把「jumpToFrame 冷路径 GUI 同步解码首帧」删掉了
（webm_clip：「GUI 线程永不同步解码首帧」）——此刻 ``_frame_pixmap`` 还是 None、
clip 的 ``currentPixmap()`` 也是 None，``icon_pixmap()`` 返回空 QPixmap，于是托盘
条目没有图标；全仓除 ``_build_tray`` 外再没有任何地方 setIcon，图标永久缺失。

修法：空图时先用占位图标（既有矢量图标语言）保证托盘可见，并订阅
``PetWindow.frame_ready``（首帧就绪的一次性信号）把图标换成角色头像。
"""
from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from pet.app import AppShell
from pet.config import Config
from pet.library import MovieLibrary
from pet.window import PetWindow

CHARACTER_RED = 200


def _simulate_no_frame_yet(win):
    """把窗口重置回「一帧都拿不到」的现场。

    本分支帧序列 B 档让真实素材的首帧在窗口构造期即可同步就绪（并发过
    frame_ready），托盘图标 bug 的「首帧未就绪」前提已无法自然复现；
    这两个用例关心的是该前提下 icon_pixmap/frame_ready 的契约行为，
    因此显式清状态模拟。

    fix 1 起「切动画不清显示槽」（FrameSeqClip.start 保留预热/同步跳帧的
    首帧直到新帧到货），只清窗口缓存已不足以模拟「一帧都拿不到」——
    ``icon_pixmap`` 在 ``_frame_pixmap`` 为空时会回退读 idle clip 的
    ``currentPixmap()``，clip 侧显示槽也必须清空（FrameSeqClip/WebMClip
    都提供 clear_display_frame；无该能力的替身 clip 用 getattr 跳过）。
    """
    win._frame_pixmap = None
    win._frame_ready_emitted = False
    idle = getattr(win, "idle", None)
    movies = [getattr(win, "movie", None)]
    if idle:
        movies.append(win.lib.movie(idle))
    for movie in movies:
        clear = getattr(movie, "clear_display_frame", None)
        if callable(clear):
            clear()


class _FakeSignal:
    """PetWindow.frame_ready 的最小替身：记录连接，可手动触发。

    用它而不是真信号，是为了能直接断言「有没有连」——只有占位分支才该连，
    首帧已就绪时连上就是多余的一次回调。
    """

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self):
        for slot in list(self.slots):
            slot()


class _TrayWin:
    """建托盘所需的最小窗口桩：只要 cfg / icon_pixmap / frame_ready。

    刻意保持非 QWidget：_build_tray 的调用方本来就可能是替身窗口，占位图标
    不该因此依赖窗口的 QSS/DPR 能力。
    """

    def __init__(self, config, pixmap):
        self.config = config
        self.frame_ready = _FakeSignal()
        self.icon_requests = 0
        self._pixmap = pixmap
        self._speech_bubble = QWidget()

    def icon_pixmap(self, _size: int = 64) -> QPixmap:
        self.icon_requests += 1
        return self._pixmap() if callable(self._pixmap) else self._pixmap

    def set_mouse_through(self, on):  # pragma: no cover - 菜单回调，本测试不触发
        self.config.set("mouse_through", bool(on))

    def go_default_corner(self):  # pragma: no cover - 菜单回调，本测试不触发
        pass


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _character_pixmap() -> QPixmap:
    """自有画面：整块纯色、角落像素不透明——与占位矢量图标可区分。"""
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor(CHARACTER_RED, 0, 0))
    return pixmap


def _corner_color(icon: QIcon) -> QColor:
    return icon.pixmap(16, 16).toImage().pixelColor(0, 0)


def _build(config, pixmap):
    shell = AppShell(QApplication.instance(), config, enable_chat=True)
    win = _TrayWin(config, pixmap)
    return shell, win, shell._build_tray(win)


def test_tray_icon_is_visible_when_first_frame_not_ready(app, tmp_path):
    """首帧未就绪（4.2.1 现状）：图标不能是空的——托盘条目必须看得见。"""
    _shell, win, tray = _build(Config(tmp_path), QPixmap())

    assert not tray.icon().isNull()
    assert win.frame_ready.slots, "占位分支必须订阅首帧就绪信号"
    tray.hide()


def test_tray_icon_switches_to_character_frame_when_ready(app, tmp_path):
    """首帧到了以后换成角色头像（占位分支才有这次回调）。"""
    frame = [QPixmap()]

    _shell, win, tray = _build(Config(tmp_path), lambda: frame[0])
    assert not tray.icon().isNull()

    frame[0] = _character_pixmap()
    win.frame_ready.emit()

    assert _corner_color(tray.icon()).red() == CHARACTER_RED
    win.frame_ready.emit()  # 幂等：真信号本身只发一次，重复触发也不能出错
    assert _corner_color(tray.icon()).red() == CHARACTER_RED
    tray.hide()


def test_tray_icon_uses_character_frame_when_already_available(app, tmp_path):
    """首帧已就绪（热切换角色后立刻建托盘等）：直接用角色帧，不连信号。"""
    _shell, win, tray = _build(Config(tmp_path), _character_pixmap())

    assert _corner_color(tray.icon()).red() == CHARACTER_RED
    assert win.frame_ready.slots == []
    tray.hide()


def test_tray_build_survives_window_without_frame_ready(app, tmp_path):
    """替身/旧窗口没有 frame_ready 时也不能炸（getattr 兜底）。"""

    class _LegacyWin(_TrayWin):
        def __init__(self, config, pixmap):
            super().__init__(config, pixmap)
            del self.frame_ready

    shell = AppShell(QApplication.instance(), Config(tmp_path), enable_chat=True)
    tray = shell._build_tray(_LegacyWin(Config(tmp_path), QPixmap()))

    assert not tray.icon().isNull()
    tray.hide()


def test_placeholder_icon_uses_existing_vector_language(app, tmp_path):
    """占位图标走既有矢量图标语言（同一套 renderer），不是新素材。"""
    shell, _win, tray = _build(Config(tmp_path), QPixmap())

    expected = shell._tray_placeholder_icon()
    assert not expected.isNull()
    assert not tray.icon().isNull()
    assert tray.icon().pixmap(64, 64).size() == expected.pixmap(64, 64).size()
    tray.hide()


def test_rebuild_frame_emits_frame_ready_once_per_window(app, tmp_path):
    """窗口侧契约：首个可显示帧到达时发一次 frame_ready，此后不再发。"""

    class _StubMovie:
        """合成帧替身：currentPixmap 直接给图，不依赖 ffmpeg 解码。"""

        def __init__(self, pixmap):
            self._pixmap = pixmap

        def currentFrameNumber(self):
            return 0

        def currentPixmap(self):
            return self._pixmap

        def stop(self):  # closeEvent 会调
            pass

    # 帧序列 B 档下构造期即同步出首帧并发过 frame_ready；重置回「首帧
    # 未就绪」现场再验证契约（见 _simulate_no_frame_yet）。
    win = PetWindow(MovieLibrary(), Config(base=tmp_path))
    _simulate_no_frame_yet(win)
    hits = []
    win.frame_ready.connect(lambda: hits.append(1))

    win.movie = _StubMovie(_character_pixmap())
    win._rebuild_frame()

    assert hits == [1]
    assert not win.icon_pixmap(64).isNull()

    win._rebuild_frame()  # 快路径：同签名直接跳过
    assert hits == [1]

    win.movie = _StubMovie(QPixmap(16, 16))  # 换素材重建：仍然只发过一次
    win._rebuild_frame()
    assert hits == [1]
    win.close()


def test_first_frame_pixmap_is_null_before_any_frame(app, tmp_path):
    """根因留证：首帧未就绪时 icon_pixmap() 就是空图（帧序列 B 档下该
    前提无法自然复现，显式模拟，见 _simulate_no_frame_yet）。"""
    win = PetWindow(MovieLibrary(), Config(base=tmp_path))
    win.show()
    QApplication.instance().processEvents()
    _simulate_no_frame_yet(win)

    assert win._frame_pixmap is None
    assert win.icon_pixmap(64).isNull()
    assert QIcon(win.icon_pixmap(64)).isNull()
    win.close()


def test_tray_menu_keeps_two_flat_harness_launch_items(app, tmp_path):
    """托盘里的 Harness 入口是**两个平铺启动项**，不是右键那个四项子菜单。

    对齐依据（2026-10 双目标）：`README.md` 现行段落写「右键菜单提供 DeepSeek
    Harness 子菜单（四项），托盘菜单是两个平铺启动项」——托盘侧此前没有守卫，
    菜单改名或把子菜单搬进托盘都不会被发现。重启/停止只在右键子菜单里提供。
    """
    _shell, _win, tray = _build(Config(tmp_path), QPixmap())
    try:
        menu = tray.contextMenu()
        assert menu is not None, "托盘必须挂了上下文菜单"
        labels = [action.text() for action in menu.actions() if not action.isSeparator()]
        assert "启动 dsh web 界面" in labels
        assert "启动桌面端界面" in labels
        assert "DeepSeek Harness" not in labels, "托盘不给 Harness 子菜单，只给两个平铺启动项"
        for label in ("重启服务", "停止服务"):
            assert label not in labels, f"托盘不提供「{label}」（那是右键子菜单的四项之一）"
    finally:
        tray.hide()
