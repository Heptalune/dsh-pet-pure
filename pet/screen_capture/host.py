# -*- coding: utf-8 -*-
"""「快速识屏」宿主侧：隐藏自家顶层窗口并在事后逐位还原。

采集前必须让自家窗口（桌宠、气泡、灵动岛、设置页……）从屏幕上消失，否则冻
结帧里会拍到自家的脸。这里只做"藏"和"还"两件事，不判断该藏谁——名单由调用方
（``controller`` 取当前可见的顶层窗口）给出。

两条纪律：

1. **只动原本可见的窗口**，且``restore_all()`` 只还原自己藏过的那些。用户手动
   藏起来的桌宠（右键"隐藏"）不许因为一次截图被意外唤出——还原必须逐位对称。
2. ``hide(notify=...)`` 形式的窗口（``PetWindow``）一律按 ``notify=False`` 藏，
   与 ``pet/window_screen.py`` 的全屏自动隐藏同口径：内部语义不弹"桌宠已隐藏"
   托盘通知。

第三条是 2026-10-13 交叉审查 m8 补的**激活态**（见 :meth:`WindowHider.restore_all`）：
隐藏会顺带把焦点从自家窗口上拿走，还回去时得把"原先是前台"这件事一起还回去。
**z-order 不做**（比"忘了做"更值得写清楚）：Qt 没有跨平台读取窗口堆叠次序的接口，
而桌宠这一类窗口的叠放本来就由产品自己的 `on_top` 策略（``WindowStaysOnTopHint`` /
岛的分层）持续决定——在这里硬 `raise_()` 会与那条策略打架，反而是新的不一致。
"""

from __future__ import annotations

import logging

import shiboken6

log = logging.getLogger("dsh-pet-standalone")

__all__ = ["WindowHider", "visible_top_level_windows"]


def visible_top_level_windows() -> list:
    """当前可见的顶层窗口（Qt 返回的列表顺序即还原顺序）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return []
    windows = []
    for widget in app.topLevelWidgets():
        if not shiboken6.isValid(widget):
            continue
        if not widget.isWindow():
            continue
        if widget.isVisible():
            windows.append(widget)
    return windows


def _hide_internal(window) -> None:
    """藏一个窗口；``PetWindow`` 风格带 ``notify`` 关键字的走 notify=False。"""
    try:
        window.hide(notify=False)
    except TypeError:
        # 普通 QWidget / 其它宿主没有 notify 关键字：退回无参 hide()。
        window.hide()


class WindowHider:
    """一次采集的隐藏账本：藏过谁、还原谁，逐位对称。

    典型用法（闭环里只有这两个调用点）::

        hider = WindowHider(visible_top_level_windows())
        hider.hide_all()
        ...
        hider.restore_all()

    任何一步都不抛异常：隐藏失败（窗口在调用间隙被销毁等）只记日志，绝不阻断
    其余窗口；否则一个坏窗口就等于采集闭环塌掉，还会把已藏好的窗口留在隐藏态。

    账本是 ``(窗口, 藏之前是不是前台窗口)`` 的列表：那一列布尔值不是装饰——
    ``hide()`` 会把焦点从自家窗口上拿走，恢复时不还回去，用户"刚点过桌宠、
    整完一张截图回来"就会发现键盘焦点跑到别的窗口上了。
    """

    def __init__(self, windows) -> None:
        self._candidates = list(windows)
        self._hidden: list[tuple] = []

    def hide_all(self) -> int:
        """藏掉当前所有可见的候选窗口，返回实际藏起来的数量。"""
        for window in self._candidates:
            if not shiboken6.isValid(window):
                continue
            try:
                if not window.isVisible():
                    continue
                ask_active = getattr(window, "isActiveWindow", None)
                was_active = bool(ask_active()) if callable(ask_active) else False
                _hide_internal(window)
            except RuntimeError as exc:  # 窗口在本轮之间被销毁
                log.debug("快速识屏：跳过无法隐藏的窗口 (%s)", exc)
                continue
            self._hidden.append((window, was_active))
        return len(self._hidden)

    def restore_all(self) -> int:
        """还原本次藏过的窗口，返回实际还原的数量；二次调用为空操作。

        还原顺序＝隐藏顺序（``hide_all`` 的候选列表顺序），激活态逐个还：藏之前是
        前台的那个窗口，恢复后重新拿回前台（``activateWindow``）。不硬调 ``raise_()``
        的理由见模块说明（叠放次序由产品自己的 on_top 策略管，这里抢一下只会打架）。
        """
        hidden, self._hidden = self._hidden, []
        restored = 0
        for window, was_active in hidden:
            if not shiboken6.isValid(window):
                continue
            try:
                window.show()
                if was_active:
                    window.activateWindow()
            except RuntimeError as exc:  # 窗口在隐藏期间被销毁
                log.debug("快速识屏：跳过已销毁的窗口 (%s)", exc)
                continue
            restored += 1
        return restored
