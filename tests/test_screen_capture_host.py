# -*- coding: utf-8 -*-
"""「快速识屏」P0 宿主侧隐藏/恢复（真实 QWidget，offscreen）。

隐藏-恢复对称性是本片最高风险点：**藏了没还** = 用户的桌宠消失。所以这里
不只测"藏住了"，还测：
- 只动原本可见的窗口（用户手动隐藏的桌宠不被意外唤出）；
- 恢复逐位还原（藏了几个还几个，返回计数就是账）；
- PetWindow 风格的 ``hide(notify=...)`` 收到 ``notify=False``（自动隐藏是内部
  语义，不许弹"桌宠已隐藏"托盘通知——对齐 ``pet/window_screen.py`` 先例）；
- 窗口在隐藏期间被销毁时不崩、不阻断其余窗口恢复。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import shiboken6  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from pet.screen_capture.host import WindowHider, visible_top_level_windows  # noqa: E402


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def widgets(qapp):
    """两个顶层窗口：默认都可见，由一个用例显式摆布。"""
    made = []
    for _ in range(2):
        w = QWidget()
        w.show()
        made.append(w)
    yield made
    for w in made:
        if shiboken6.isValid(w):
            shiboken6.delete(w)


class _PetLikeWindow(QWidget):
    """模拟 PetWindow：``hide()`` 带 notify 关键字（触发托盘通知的那一支）。"""

    def __init__(self):
        super().__init__()
        self.hide_notify_args = []
        self.show_calls = 0

    def hide(self, *, notify: bool = True) -> None:  # noqa: D102
        self.hide_notify_args.append(notify)
        super().hide()

    def show(self) -> None:  # noqa: D102
        self.show_calls += 1
        super().show()


def test_visible_top_level_windows_lists_only_visible_ones(qapp, widgets):
    visible, hidden = widgets
    hidden.hide()

    listed = visible_top_level_windows()

    assert visible in listed
    assert hidden not in listed


def test_hide_all_covers_only_visible_windows(qapp, widgets):
    visible, hidden = widgets
    hidden.hide()

    hider = WindowHider([visible, hidden])
    assert hider.hide_all() == 1
    assert not visible.isVisible()
    assert not hidden.isVisible()


def test_restore_returns_exactly_what_was_hidden(qapp, widgets):
    visible, hidden = widgets
    hidden.hide()

    hider = WindowHider([visible, hidden])
    hider.hide_all()
    assert hider.restore_all() == 1

    assert visible.isVisible()          # 还原
    assert not hidden.isVisible()       # 用户自己隐藏的，不许被唤出
    assert hider.restore_all() == 0     # 二次恢复是空操作


def test_pet_like_window_is_hidden_without_tray_notification(qapp):
    win = _PetLikeWindow()
    win.show()
    baseline_shows = win.show_calls

    hider = WindowHider([win])
    hider.hide_all()
    assert win.hide_notify_args == [False]

    hider.restore_all()
    assert win.show_calls == baseline_shows + 1
    assert win.isVisible()
    shiboken6.delete(win)


def test_plain_widget_without_notify_kwarg_still_hides(qapp):
    """普通 QWidget 的 hide() 不接受 notify，不得因此漏藏。"""
    win = QWidget()
    win.show()

    hider = WindowHider([win])
    assert hider.hide_all() == 1
    assert not win.isVisible()
    assert hider.restore_all() == 1
    assert win.isVisible()


def test_destroyed_window_does_not_break_restore(qapp, widgets):
    doomed, survivor = widgets
    hider = WindowHider([doomed, survivor])
    assert hider.hide_all() == 2

    shiboken6.delete(doomed)
    assert hider.restore_all() == 1  # 只还活着的那个
    assert survivor.isVisible()


def test_destroyed_window_does_not_break_hide(qapp, widgets):
    doomed, survivor = widgets
    shiboken6.delete(doomed)

    hider = WindowHider([doomed, survivor])
    assert hider.hide_all() == 1
    assert not survivor.isVisible()
    assert hider.restore_all() == 1


def test_hidden_window_list_is_not_shared_between_instances(qapp, widgets):
    visible, _ = widgets
    first = WindowHider([visible])
    second = WindowHider([visible])

    first.hide_all()
    assert second.hide_all() == 0   # 第二次看到的是"已不可见"，不重复记账

    assert first.restore_all() == 1
    assert visible.isVisible()
    assert second.restore_all() == 0


# ------------------------------------------------- 激活态还原（交叉审查 m8）


class _FocusableWindow(QWidget):
    """能伪造"我是不是前台"的窗口：offscreen 下真窗口拿不到激活态，只能造一个。"""

    def __init__(self, *, active: bool) -> None:
        super().__init__()
        self._active = active
        self.activations = 0

    def isActiveWindow(self) -> bool:  # noqa: N802 - 对齐 Qt 命名
        return self._active

    def activateWindow(self) -> None:  # noqa: N802 - 对齐 Qt 命名
        self.activations += 1


def test_restore_gives_the_front_window_its_focus_back(qapp):
    """藏之前是前台的窗口，恢复后要重新拿回前台。

    隐藏会把焦点从自家窗口上拿走（``hide()`` 之后它不再是前台）；只 ``show()``
    的话用户"点过桌宠 → 截一张图 → 回来"会发现键盘焦点落在别人身上。
    """
    front = _FocusableWindow(active=True)
    back = _FocusableWindow(active=False)
    front.show()
    back.show()

    hider = WindowHider([front, back])
    assert hider.hide_all() == 2
    assert hider.restore_all() == 2

    assert front.activations == 1
    assert front.isVisible() is True
    assert back.activations == 0, "原本不是前台的不许被这次截图抢前台"


def test_activation_is_recorded_at_hide_time(qapp):
    """账本记的是"藏之前"那一刻：隐藏本身就会把前台状态改掉，读晚了就永远是假。"""
    window = _FocusableWindow(active=True)
    window.show()

    hider = WindowHider([window])
    hider.hide_all()
    window._active = False        # 真实 Qt 里 hide() 之后就是这个样子
    hider.restore_all()

    assert window.activations == 1


def test_restore_never_raises_when_the_window_dies_before_activation(qapp):
    """激活那一手也在 try 里：窗口在隐藏期间被销毁时不许连累其余窗口。"""
    doomed = _FocusableWindow(active=True)
    survivor = _FocusableWindow(active=True)
    doomed.show()
    survivor.show()

    hider = WindowHider([doomed, survivor])
    assert hider.hide_all() == 2
    shiboken6.delete(doomed)

    assert hider.restore_all() == 1        # 只还活着的那个
    assert survivor.activations == 1
