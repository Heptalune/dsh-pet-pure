# -*- coding: utf-8 -*-
"""overlay 外围窗口能力（Phase 4.1b）：全屏/光标监视器。

语义逐位对齐旧架构的每窗路径（pet/window_screen.py 的
on_fullscreen_changed / on_cursor_visibility_changed 与 multi_window_shared
的探测节拍），只是消费者从 PetWindow 换成 OverlayShell：

- 全屏探测 1Hz（platform_win._fg_fullscreen_probe）：前台出现全屏应用 →
  隐藏 overlay；退出 → 恢复。配置门 auto_hide_fullscreen（默认 True）。
- 光标可见性 20Hz（vision.get_cursor_visibility）：HIDDEN 持续 ≥0.2s →
  自动穿透（游戏/全屏应用抢走光标时让出鼠标）；SHOWING → 恢复。
  配置门 cursor_hidden_passthrough（默认 True）；**且**只在有可见 overlay
  时跑（O2：隐藏期窗口不收输入，探测出的穿透态不可观测，纯属空转——
  见 OverlayVisibilityGate）。
- 仅 Windows（旧语义同为 Windows 限定）；两扇门全关时停表省电。

探测函数可注入（测试喂假探针，不碰 Win32）。
"""
from __future__ import annotations

import logging
import sys
import time
import weakref

from PySide6.QtCore import QObject, QTimer, Signal

_FULLSCREEN_POLL_MS = 1000
_CURSOR_POLL_MS = 50
_CURSOR_HIDDEN_DEBOUNCE_S = 0.2


class OverlayVisibilityGate:
    """「本进程还有可见 overlay 吗」的登记处（O2）。

    隐藏的 overlay 不接收任何输入，光标可见性探测（20Hz 的
    ``vision.get_cursor_visibility``）探测出的穿透态不可观测，纯属空转；
    但全屏探测不能停——它是全屏避让后恢复显示的唯一路径。

    为什么用进程级登记而不是壳层直接接线：本批文件范围不含 ``overlay_shell``，
    且语义上「只要还有任一 overlay 可见，光标探测就不该停」本就是进程级的
    （多屏各一个 overlay 时，隐藏一块屏的窗口不该停掉另一块的探测）。
    ``OverlayWindow.showEvent/hideEvent`` 上报，``FullscreenCursorWatcher``
    订阅；两者都用弱引用，窗口/watcher 被回收即自动摘除，不留悬挂登记。
    """

    def __init__(self) -> None:
        self._visible: "weakref.WeakSet" = weakref.WeakSet()
        self._subscribers: set = set()

    def note(self, widget, visible: bool) -> None:
        """上报一个 overlay 的显隐；可见数 0↔非 0 翻转时通知订阅者。"""
        before = len(self._visible) > 0
        if visible:
            self._visible.add(widget)
        else:
            self._visible.discard(widget)
        if before != (len(self._visible) > 0):
            self._notify()

    def any_visible(self) -> bool:
        return len(self._visible) > 0

    def count(self) -> int:
        return len(self._visible)

    def subscribe(self, callback) -> None:
        """订阅显隐翻转（要求绑定方法：由 ``weakref.WeakMethod`` 弱持有）。"""
        self._subscribers.add(weakref.WeakMethod(callback))

    def unsubscribe(self, callback) -> None:
        self._subscribers.discard(weakref.WeakMethod(callback))

    def _notify(self) -> None:
        for ref in list(self._subscribers):
            callback = ref()
            if callback is None:
                self._subscribers.discard(ref)  # 对象已回收：顺手清表
                continue
            try:
                callback()
            except Exception:
                # 订阅方已销毁/半销毁（C++ 侧对象没了等）：登记表不因它崩掉——
                # 这条链挂在上报方的 showEvent/hideEvent 上，不能反过来打断显隐。
                logging.debug("overlay 可见性订阅者失败", exc_info=True)


_overlay_visibility = OverlayVisibilityGate()


def note_overlay_visibility(widget, visible: bool) -> None:
    """``OverlayWindow`` 显隐上报入口（showEvent/hideEvent 各一次）。"""
    _overlay_visibility.note(widget, visible)


def visible_overlay_count() -> int:
    """当前登记为可见的 overlay 数（诊断/测试用）。"""
    return _overlay_visibility.count()


class FullscreenCursorWatcher(QObject):
    """overlay 版全屏/光标监视（4.1b parity）。"""

    fullscreen_changed = Signal(bool)
    cursor_visibility_changed = Signal(str)

    def __init__(self, parent: QObject | None = None, *,
                 fullscreen_probe=None, cursor_probe=None, clock=time.monotonic,
                 overlay_visibility: OverlayVisibilityGate | None = None) -> None:
        super().__init__(parent)
        self._clock = clock
        if fullscreen_probe is None or cursor_probe is None:
            if sys.platform == "win32":
                from . import platform_win
                from . import vision
                fullscreen_probe = fullscreen_probe or (
                    lambda: platform_win._fg_fullscreen_probe()[0])
                cursor_probe = cursor_probe or vision.get_cursor_visibility
        self._fullscreen_probe = fullscreen_probe or (lambda: False)
        self._cursor_probe = cursor_probe or (lambda: "UNKNOWN")
        self._overlay_visibility = (
            overlay_visibility if overlay_visibility is not None
            else _overlay_visibility)
        self._fullscreen_enabled = False
        self._cursor_enabled = False
        self._fs_last = False
        self._cursor_hidden_since: float | None = None
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(_FULLSCREEN_POLL_MS)
        self._fs_timer.timeout.connect(self._poll_fullscreen)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(_CURSOR_POLL_MS)
        self._cursor_timer.timeout.connect(self._poll_cursor)
        self._overlay_visibility.subscribe(self.on_overlay_visibility_changed)

    # ---------------------------------------------------------------- 开关
    def set_fullscreen_enabled(self, on: bool) -> None:
        """auto_hide_fullscreen 配置门：关时停表（省电）并复位状态。

        全屏探测表**不**随 overlay 显隐停表（O2）：隐藏后它是唯一的恢复路径。
        """
        on = bool(on)
        if on == self._fullscreen_enabled:
            return
        self._fullscreen_enabled = on
        if on:
            self._fs_last = False
            self._fs_timer.start()
        else:
            self._fs_timer.stop()
            if self._fs_last:
                self._fs_last = False
                self.fullscreen_changed.emit(False)  # 关门即恢复（不滞留隐藏态）

    def set_cursor_enabled(self, on: bool) -> None:
        """cursor_hidden_passthrough 配置门：关时停表并恢复穿透态。"""
        on = bool(on)
        if on == self._cursor_enabled:
            return
        self._cursor_enabled = on
        if on:
            self._cursor_hidden_since = None
            self._sync_cursor_timer()
        else:
            self._cursor_timer.stop()
            self.cursor_visibility_changed.emit("SHOWING")

    def on_overlay_visibility_changed(self) -> None:
        """可见 overlay 数 0↔非 0 翻转 → 同步光标探测表（O2）。

        恢复时立即补一次探测：``_cursor_hidden_since`` 不清（停表对 0.2s
        去抖状态机透明——隐藏期光标状态没变，恢复后就该按原结论继续），
        所以首拍要么立即报 HIDDEN（停表前已过去抖），要么按 SHOWING 复位。
        """
        running = self._cursor_timer.isActive()
        self._sync_cursor_timer()
        if not running and self._cursor_timer.isActive():
            self._poll_cursor()

    def _sync_cursor_timer(self) -> None:
        """光标探测表：配置门开 **且** 至少一个 overlay 可见时才跑。"""
        should_run = self._cursor_enabled and self._overlay_visibility.any_visible()
        if should_run:
            if not self._cursor_timer.isActive():
                self._cursor_timer.start()
        elif self._cursor_timer.isActive():
            self._cursor_timer.stop()

    # ---------------------------------------------------------------- 探测（同步直调可测）
    def _poll_fullscreen(self) -> None:
        try:
            hit = bool(self._fullscreen_probe())
        except Exception:
            return  # 探测失败不翻转状态（旧路径同样静默）
        if hit != self._fs_last:
            self._fs_last = hit
            self.fullscreen_changed.emit(hit)

    def _poll_cursor(self) -> None:
        try:
            visibility = str(self._cursor_probe())
        except Exception:
            return
        now = self._clock()
        if visibility == "HIDDEN":
            if self._cursor_hidden_since is None:
                self._cursor_hidden_since = now
            elif now - self._cursor_hidden_since >= _CURSOR_HIDDEN_DEBOUNCE_S:
                self.cursor_visibility_changed.emit("HIDDEN")
        elif visibility == "SHOWING":
            self._cursor_hidden_since = None
            self.cursor_visibility_changed.emit("SHOWING")

    def stop(self) -> None:
        self._fs_timer.stop()
        self._cursor_timer.stop()
        self._overlay_visibility.unsubscribe(self.on_overlay_visibility_changed)
