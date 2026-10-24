# -*- coding: utf-8 -*-
"""M2 回归：右键菜单树用完即释放（overlay 路径不得累积 QMenu/QAction）。

菜单以长命窗口为 parent，``menu.exec()`` 返回后只丢 Python 引用的话，整棵树
（QMenu/QAction/动画图标解码池/图标 pixmap）会随每次右键永久累积——根因是
``apply_modern_menu_style`` / ``install_modern_check_indicators`` 的
``aboutToShow.connect(lambda menu=menu: ...)`` 自引用连接让 Python 侧也永不释放。

量化（offscreen，经 facade 构建 modern 全量菜单，10 轮；探针
``.scratch/windows-parity-20260926-a/fix-20260928-M1M2/probe-menu-counts.py``）：

- 不释放：+153 QMenu / +780 QAction；
- 走 ``release_menu_tree``：增量 0。

本文件锁的是**产品调用点**：``ShellOverlayWindow.contextMenuEvent`` 与
``_exec_full_menu_at``（模板切换原位重开）两条 exec 路径返回后整棵树都必须被
释放，且收口前先 ``_animation_icon_pool.clear()``、收口不阻塞 GUI 线程。
"""
from __future__ import annotations

import gc
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu

import tests.test_sprite_menu_facade as fac
from pet.context_menus.shared import release_menu_tree

app = QApplication.instance() or QApplication([])

HOT_NAMES = ("QMenu", "QAction")
ROUNDS = 15


# ---------------------------------------------------------------- 探测工具
def _qt_census() -> dict:
    """进程内 QMenu/QAction 对象数（deleteLater 后 wrapper 会随 C++ 一起消失）。"""
    gc.collect()
    counts: dict[str, int] = {}
    for obj in gc.get_objects():
        try:
            name = type(obj).__name__
        except Exception:
            continue
        if name in HOT_NAMES:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _flush_deferred_delete() -> None:
    """冲刷 deleteLater（offscreen 下 processEvents 不会处理 DeferredDelete）。"""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _menu_ready_shell(tmp_path):
    """命中的主 sprite（右键落在它身体中心）。"""
    shell, _lib = fac._make_shell(tmp_path)
    sprite = shell.sprite
    sprite.bind_clip("idle1")
    sprite._rebuild_pixmap()          # 命中图（alpha 判定）惰性重建
    return shell


def _non_blocking(menu):
    """实例级 exec 桩：offscreen 下真 ``QMenu.exec`` 会进模态嵌套循环（挂死）。"""
    menu.exec = lambda *args: 0
    return menu


def _right_click_at_center(shell) -> None:
    center = shell.sprite.rect().center()
    shell.overlay.contextMenuEvent(
        QContextMenuEvent(QContextMenuEvent.Reason.Mouse, center, QPoint(300, 300)))


class _RecordingPool:
    """图标解码池替身：记录 clear()，waitForDone(0) 立即回报"已收工"。"""

    def __init__(self, finish: bool = True) -> None:
        self.finish = finish
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1

    def waitForDone(self, _ms: int) -> bool:  # noqa: N802 - Qt API
        return self.finish


# ---------------------------------------------------------------- 用例
def test_repeated_context_menu_rounds_leave_no_qt_residue(tmp_path):
    """M2 红线：连续 N 轮右键菜单后 QMenu/QAction 零增长（改前每轮 +15/+78）。"""
    shell = _menu_ready_shell(tmp_path)
    try:
        builder = shell.overlay._full_menu_builder
        shell.overlay._full_menu_builder = (
            lambda target: _non_blocking(builder(target)))

        _right_click_at_center(shell)          # 预热：首次构建的一次性缓存
        _flush_deferred_delete()
        base = _qt_census()

        for _ in range(ROUNDS):
            _right_click_at_center(shell)
            _flush_deferred_delete()

        after = _qt_census()
        assert base["QMenu"] > 0 and base["QAction"] > 0, "普查口径失效"
        assert after == base, f"菜单树残留：{after} != 基线 {base}"
    finally:
        shell._delete_runtime_marker()


def test_context_menu_event_releases_the_menu_tree(tmp_path):
    """exec 返回后整棵树真的被销毁（不是只丢 Python 引用）。"""
    shell = _menu_ready_shell(tmp_path)
    try:
        builder = shell.overlay._full_menu_builder
        made: list[QMenu] = []

        def build(target):
            menu = _non_blocking(builder(target))
            made.append((menu, len(menu.findChildren(QMenu))))
            return menu

        shell.overlay._full_menu_builder = build
        _right_click_at_center(shell)
        _flush_deferred_delete()
        assert made, "右键菜单必须真的建出来（否则用例退化）"
        menu, submenu_count = made[0]
        assert submenu_count >= 10, "全量菜单应有多层子菜单（否则用例退化）"
        assert not shiboken6.isValid(menu), "菜单树必须被释放"
    finally:
        shell._delete_runtime_marker()


def test_reopen_path_releases_the_menu_tree(tmp_path):
    """模板切换原位重开（``_exec_full_menu_at``）同样要收口。"""
    shell = _menu_ready_shell(tmp_path)
    try:
        builder = shell.overlay._full_menu_builder
        made: list[QMenu] = []

        def build(target):
            menu = _non_blocking(builder(target))
            made.append(menu)
            return menu

        shell.overlay._full_menu_builder = build
        center = shell.sprite.rect().center()
        shell.overlay._exec_full_menu_at(shell.overlay.mapToGlobal(center))
        _flush_deferred_delete()
        assert made, "原位重开路径必须真的建了菜单（否则用例退化）"
        assert not shiboken6.isValid(made[-1]), "菜单树必须被释放"
    finally:
        shell._delete_runtime_marker()


def test_deferred_callbacks_still_dispatch_after_release(tmp_path):
    """收口不得吃掉挂起命令：exec 返回后 0ms 定时器照常派发（语义不变）。"""
    shell = _menu_ready_shell(tmp_path)
    calls: list[str] = []
    try:
        builder = shell.overlay._full_menu_builder
        made: list[QMenu] = []

        def build(target):
            menu = _non_blocking(builder(target))
            menu._deferred_callbacks = [lambda: calls.append("hidden")]
            made.append(menu)
            return menu

        shell.overlay._full_menu_builder = build
        _right_click_at_center(shell)
        assert calls == [], "挂起命令必须晚于 exec 返回（0ms 定时器）"
        app.processEvents()
        assert calls == ["hidden"], "菜单树释放不得吃掉挂起命令"
        _flush_deferred_delete()
        assert made and not shiboken6.isValid(made[0])
    finally:
        shell._delete_runtime_marker()


def test_release_menu_tree_clears_animation_icon_pools(tmp_path):
    """收口前先清掉尚未启动的图标解码任务，再释放菜单树。"""
    menu = QMenu()
    submenu = menu.addMenu("动画 · 待机")
    pool = _RecordingPool()
    submenu._animation_icon_pool = pool
    try:
        release_menu_tree(menu)
        assert pool.cleared == 1, "必须先 pool.clear()（否则析构在 GUI 线程等 worker）"
        _flush_deferred_delete()
        assert not shiboken6.isValid(menu)
    finally:
        if shiboken6.isValid(menu):
            menu.deleteLater()
        _flush_deferred_delete()


def test_release_menu_tree_never_blocks_gui_thread(tmp_path):
    """worker 不结束时按 50ms 轮询等待（不阻塞事件循环），树暂不销毁。"""
    menu = QMenu()
    submenu = menu.addMenu("动画 · 待机")
    pool = _RecordingPool(finish=False)
    submenu._animation_icon_pool = pool
    try:
        started = time.monotonic()
        release_menu_tree(menu)
        elapsed = time.monotonic() - started
        # 阻塞式 waitForDone(3000) 会耗 3s；轮询路径立即返回
        assert elapsed < 0.5, f"收口阻塞了 GUI 线程 {elapsed:.2f}s"
        _flush_deferred_delete()
        assert shiboken6.isValid(menu), "worker 未收工时树不能提前销毁"
    finally:
        if shiboken6.isValid(menu):
            menu.deleteLater()
        _flush_deferred_delete()
