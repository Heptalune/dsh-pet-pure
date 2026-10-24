# -*- coding: utf-8 -*-
"""菜单动画缩略图 worker 的生命周期与失败收口（真 QThreadPool + 真 QRunnable）。

回归背景：``pet/context_menus/shared.py::_AnimationIconWorker.run`` 原本只有一行
``self.signals.ready.emit(self.loader(self.animation_name))``。loader 抛异常时异常从
QRunnable 逃出，由 shiboken 打印 ``Error calling Python override of QRunnable::run():``
后吞掉（实测**不走** ``sys.unraisablehook``，只进 stderr）：
① applier 永不触发 ⇒ 该 worker 永远留在 ``submenu._animation_icon_workers`` 里，
② 泵按"在岗 worker < 2"放行 ⇒ 两只失败后队列**永久卡死**，同一菜单里后面的图标再也
不解码，
③ 失败原因只剩一行被测试捕获吞掉的 shiboken 输出。
全量日志（``.scratch/product-integrated-regression-20261010/logs/53-final2-main-suite.txt``）
里那两条正是这个签名。

本文件全部走**真线程池 + 真事件循环**（不直接 call ``run()``），覆盖：
1. 失败与成功走同一条收口：摘在岗记录、续泵、其余图标照常解码（队列不卡）；
2. 失败日志有原因且不按图标刷屏（同菜单第一条 warning，其余 debug）；
3. 宿主先销毁（信号源已删 / 菜单树已释放）时 worker 不逃异常、不写 Qt 死对象；
4. 真进程 stderr 里不再出现 shiboken 的 ``Error calling Python override ...``
   （子进程 + 无捕获，与全量日志同一可见口径）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMenu

from pet.context_menus.shared import (
    _AnimationIconApplier,
    _AnimationIconWorker,
    _populate_animation_category,
    release_menu_tree,
)

app = QApplication.instance() or QApplication([])

# 宽预算：CI 慢 runner 是新开机的，解码线程调度可能明显慢于本地（AGENTS.md
# §CI cost discipline：事件同步 + 宽预算，不用固定 sleep 猜时序）。
_BUDGET_S = 30.0


def _wait_until(predicate, *, timeout: float = _BUDGET_S) -> bool:
    """轮询状态直到成立（期间照常跑事件循环），超时返回最后的判定值。"""
    deadline = time.monotonic() + timeout
    while True:
        app.processEvents()
        result = predicate()
        if result or time.monotonic() >= deadline:
            return bool(result)
        time.sleep(0.01)


def _flush_deferred_delete() -> None:
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


class _RaisingPet:
    """素材面替身：缩略图解码一律失败（模拟库已销毁 / C++ 对象已删）。"""

    def __init__(self, error: BaseException, names: list) -> None:
        self._error = error
        self.calls = names

    def animation_icon_cached_image(self, name):
        return QImage()

    def animation_icon_image(self, name):
        self.calls.append(name)
        raise self._error


class _OkPet(_RaisingPet):
    """素材面替身：解码成功，用于证明失败之后**其它图标照常继续**。"""

    def animation_icon_image(self, name):
        self.calls.append(name)
        image = QImage(8, 8, QImage.Format.Format_ARGB32)
        image.fill(0xFF3366FF)
        return image


def _menu(tmp_path) -> QMenu:
    menu = QMenu()
    menu.setObjectName(f"icon-worker-test-{tmp_path.name}")
    return menu


# ---------------------------------------------------------------- 1. 队列不卡
def test_loader_failure_does_not_stall_the_thumbnail_queue(tmp_path):
    """失败也必须收口续泵：6 项全解码过（不是卡在头两只），在岗记录清零。

    红：今天头两只 worker 失败即静默死掉，``calls`` 停在 2、``_animation_icon_workers``
    永远留着两只、``_animation_icon_pending`` 还有 4 项 —— 队列永久停摆。
    """
    names = [f"动作{index}" for index in range(6)]
    calls: list = []
    pet = _RaisingPet(RuntimeError("Internal C++ object (MovieLibrary) already deleted"), calls)
    menu = _menu(tmp_path)
    submenu = menu.addMenu("随机动作")
    try:
        _populate_animation_category(
            submenu, pet, names, lambda _name: None, True, False)

        assert _wait_until(lambda: len(calls) == len(names)), (
            f"失败后队列卡住：只解码了 {calls}，在岗 "
            f"{len(getattr(submenu, '_animation_icon_workers', []))}，"
            f"待处理 {len(getattr(submenu, '_animation_icon_pending', []))}")
        assert _wait_until(lambda: not submenu._animation_icon_workers), \
            "收口必须摘掉在岗记录（否则泵永远按满员放行）"
        assert list(submenu._animation_icon_pending) == [], "待处理队列必须排空"
        assert set(submenu._animation_icon_requested) == set(names), "每项只排一次"
    finally:
        release_menu_tree(menu)
        _flush_deferred_delete()


def test_success_still_decodes_every_icon(tmp_path):
    """对照臂：成功路径的收口语义不变（6 项全解、在岗清零、队列排空）。"""
    names = [f"动作{index}" for index in range(6)]
    calls: list = []
    pet = _OkPet(RuntimeError("unused"), calls)
    menu = _menu(tmp_path)
    submenu = menu.addMenu("随机动作")
    try:
        _populate_animation_category(
            submenu, pet, names, lambda _name: None, True, False)
        assert _wait_until(lambda: len(calls) == len(names)), calls
        assert _wait_until(lambda: not submenu._animation_icon_workers)
        assert list(submenu._animation_icon_pending) == []
    finally:
        release_menu_tree(menu)
        _flush_deferred_delete()


def test_failure_then_success_keeps_decoding(tmp_path):
    """一只失败不许拖停后面的图标：失败项之后必须还有成功解码的项。"""
    names = ["坏动作", "好动作1", "好动作2", "好动作3"]
    calls: list = []

    class _MixedPet:
        def animation_icon_cached_image(self, name):
            return QImage()

        def animation_icon_image(self, name):
            calls.append(name)
            if name == "坏动作":
                raise RuntimeError("库已销毁")
            image = QImage(8, 8, QImage.Format.Format_ARGB32)
            image.fill(0xFF22AA44)
            return image

    menu = _menu(tmp_path)
    submenu = menu.addMenu("随机动作")
    try:
        _populate_animation_category(
            submenu, _MixedPet(), names, lambda _name: None, True, False)
        assert _wait_until(lambda: len(calls) == len(names)), calls
        assert calls[-1] != "坏动作", "失败项之后必须继续解码其它图标"
    finally:
        release_menu_tree(menu)
        _flush_deferred_delete()


# ---------------------------------------------------------------- 2. 真池上的收口
def test_failed_worker_completes_through_the_applier_on_a_real_pool(tmp_path):
    """真 QThreadPool 跑一只必失败的 worker：applier 照常收口并续泵。

    这里刻意在断言时 worker 的 C++ 侧已被池自动删除（``shiboken6.isValid`` 为假）——
    收口路径必须能在"只剩 Python 壳"的状态下工作。
    """
    menu = _menu(tmp_path)
    submenu = menu.addMenu("随机动作")
    submenu._animation_icon_workers = []
    action = submenu.addAction("测试动画")
    pump_calls: list = []
    pool = QThreadPool()
    pool.setMaxThreadCount(2)

    def loader(_name):
        raise AttributeError("'MovieLibrary' object has no attribute 'clip_path'")

    worker = _AnimationIconWorker(loader, "测试动画")
    applier = _AnimationIconApplier(
        submenu, action, worker, lambda: pump_calls.append(1), parent=submenu)
    worker.signals.ready.connect(applier.on_ready)
    submenu._animation_icon_workers.append(worker)
    try:
        pool.start(worker)
        assert _wait_until(lambda: pump_calls), "失败也必须触发收口续泵（今天永不触发）"
        assert not submenu._animation_icon_workers, "在岗记录必须摘掉"
        assert action.icon().isNull(), "失败不上图标（保持占位图标，不乱写 QAction）"
        assert _wait_until(lambda: not shiboken6.isValid(worker)), \
            "池按 autoDelete 收掉 C++ 侧（worker 的 C++ owner 是池）"
    finally:
        pool.clear()
        pool.waitForDone(5000)
        release_menu_tree(menu)
        _flush_deferred_delete()


# ---------------------------------------------------------------- 3. 日志
def test_failure_log_has_a_reason_and_does_not_flood(tmp_path, caplog):
    """失败日志：第一条 warning 带动画名与原因，其余降 debug —— 不按图标刷屏。

    红：今天 applier 根本不跑，一条日志都没有。
    """
    names = [f"动作{index}" for index in range(6)]
    calls: list = []
    pet = _RaisingPet(AttributeError("'MovieLibrary' object has no attribute 'clip_path'"), calls)
    menu = _menu(tmp_path)
    submenu = menu.addMenu("随机动作")
    try:
        with caplog.at_level("DEBUG", logger="pet.context_menus.shared"):
            _populate_animation_category(
                submenu, pet, names, lambda _name: None, True, False)
            assert _wait_until(lambda: not submenu._animation_icon_workers), calls

        records = [r for r in caplog.records if r.name == "pet.context_menus.shared"]
        warnings = [r for r in records if r.levelno >= 30]
        assert len(warnings) == 1, [r.getMessage() for r in records]
        text = warnings[0].getMessage()
        assert "clip_path" in text, text
        assert any(name in text for name in names), f"日志必须点出出错的动画：{text}"
        assert warnings[0].exc_info, "第一条失败要带堆栈（原因可追）"

        debugs = [r for r in records if r.levelno < 30]
        assert debugs, "后续失败必须保留（降级为 debug，不是静默）"
        assert all("clip_path" in r.getMessage() for r in debugs), \
            [r.getMessage() for r in debugs]
    finally:
        release_menu_tree(menu)
        _flush_deferred_delete()


# ---------------------------------------------------------------- 4. 真 stderr
_CHILD_SCRIPT = textwrap.dedent(
    '''
    # -*- coding: utf-8 -*-
    """子进程：真 QRunnable + 真 QThreadPool，三种失败现场各跑一遍。"""
    import os
    import shiboken6
    import sys
    import time

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QEvent, QThreadPool, QTimer
    from PySide6.QtWidgets import QApplication, QMenu

    from pet.context_menus.shared import _AnimationIconApplier, _AnimationIconWorker

    app = QApplication([])
    pools = []

    def make_pool():
        pool = QThreadPool()
        pool.setMaxThreadCount(2)
        pools.append(pool)
        return pool

    def attach(submenu, name, loader):
        pool = make_pool()
        action = submenu.addAction(name)
        worker = _AnimationIconWorker(loader, name)
        applier = _AnimationIconApplier(
            submenu, action, worker, lambda: pump_calls.append(name), parent=submenu)
        worker.signals.ready.connect(applier.on_ready)
        submenu._animation_icon_workers.append(worker)
        pool.start(worker)
        return worker

    pump_calls = []

    # 现场 A：loader 抛异常（假库缺 clip_path / 库已销毁）
    menu_a = QMenu()
    submenu_a = menu_a.addMenu("随机动作")
    submenu_a._animation_icon_workers = []

    def raising(name):
        raise AttributeError("'MovieLibrary' object has no attribute 'clip_path'")

    for index in range(2):
        attach(submenu_a, "动作%d" % index, raising)

    # 现场 B：worker 的信号源已被销毁就开跑（宿主先收口）
    menu_b = QMenu()
    submenu_b = menu_b.addMenu("随机动作")
    submenu_b._animation_icon_workers = []
    worker_b = _AnimationIconWorker(raising, "已删信号")
    shiboken6.delete(worker_b.signals)
    pool_b = make_pool()
    pool_b.start(worker_b)

    # 现场 C：worker 在途时宿主（菜单树 + applier）整棵释放
    menu_c = QMenu()
    submenu_c = menu_c.addMenu("随机动作")
    submenu_c._animation_icon_workers = []

    def slow(name):
        time.sleep(0.3)
        return None

    attach(submenu_c, "慢动作", slow)
    submenu_c.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()

    started = time.monotonic()
    while time.monotonic() - started < 10.0 and len(pump_calls) < 2:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    for pool in pools:
        pool.waitForDone(5000)
    app.processEvents()

    print("CHILD_PUMP_CALLS %d" % len(pump_calls))
    print("CHILD_WORKERS_LEFT %d" % len(submenu_a._animation_icon_workers))
    print("CHILD_END")
    sys.stdout.flush()
    '''
)


def test_real_qrunnable_failure_never_reaches_qt_stderr(tmp_path):
    """子进程 + 无捕获跑真 QRunnable：stderr 不许出现 shiboken 的 QRunnable 异常签名。

    这是全量日志里那两条 `Error calling Python override of QRunnable::run():` 的同一
    可见口径：pytest 的捕获会把 shiboken 打到 fd2 的这行留在终端、把 Python 堆栈吞掉，
    子进程里两者都拿得到，所以"stderr 干净 + 收口计数齐"才算真的收口。

    红：三个现场都往 stderr 打 `Error calling Python override ...`，且 CHILD_PUMP_CALLS 为 0
    （收口从未发生）。
    """
    repo_root = Path(__file__).resolve().parents[1]
    script = tmp_path / "icon_worker_child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=180, encoding="utf-8", errors="replace",
    )
    out = proc.stdout or ""
    err = proc.stderr or ""
    assert proc.returncode == 0, f"stdout={out}\nstderr={err}"
    assert "CHILD_END" in out, f"子进程没跑完：stdout={out}\nstderr={err}"
    assert "Error calling Python override" not in (out + err), (
        "worker 里逃出的异常仍被 shiboken 吞到 stderr：\n" + err)
    # 现实场 A 的两只失败必须走完收口（applier 跑过 → 续泵），现场 C 无消费者不计数
    assert "CHILD_PUMP_CALLS 2" in out, f"失败未收口续泵：{out}"
    assert "CHILD_WORKERS_LEFT 0" in out, f"在岗记录未摘：{out}"


# ---------------------------------------------------------------- 5. 宿主先销毁
def test_released_host_is_not_written_by_late_worker(tmp_path):
    """worker 在途时整棵菜单树被释放：释放不卡死，迟到 ready 不写已删 C++ 对象。

    现场：真线程里 loader 先卡一下再失败，宿主（菜单 + applier + 池）在它收工前释放。
    """
    import threading

    started = threading.Event()
    calls: list = []

    class _SlowFailingPet:
        def animation_icon_cached_image(self, name):
            return QImage()

        def animation_icon_image(self, name):
            calls.append(name)
            started.set()
            time.sleep(0.3)
            raise RuntimeError("库已销毁")

    menu = _menu(tmp_path)
    submenu = menu.addMenu("随机动作")
    _populate_animation_category(
        submenu, _SlowFailingPet(), ["慢动作"], lambda _name: None, True, False)
    assert started.wait(_BUDGET_S), "worker 必须真的在跑（否则用例退化）"
    assert submenu._animation_icon_workers, "未收工前在岗记录必须还在"

    # 在途释放：新路径必须把池里在跑的 worker 等干净后销毁，不阻塞、不提前销毁
    release_menu_tree(menu)
    assert _wait_until(lambda: not shiboken6.isValid(submenu)), \
        "在途 worker 收工后菜单树必须被释放（释放路径不得卡死）"

    # 释放之后再给事件循环几轮机会：迟到的 ready 不许炸在任何已删对象上
    for _ in range(20):
        app.processEvents()
        time.sleep(0.01)


def test_applier_payload_contract():
    """载荷契约：失败载荷是独立类型、可带原因；None/有效图仍按旧语义处理。"""
    from pet.context_menus.shared import _AnimationIconFailure

    error = RuntimeError("库已销毁")
    failure = _AnimationIconFailure("动作", error)
    assert failure.animation_name == "动作" and failure.error is error

    menu = QMenu()
    submenu = menu.addMenu("随机动作")
    submenu._animation_icon_workers = []
    action = submenu.addAction("测试动画")
    pump_calls: list = []
    worker = object()
    applier = _AnimationIconApplier(
        submenu, action, worker, lambda: pump_calls.append(1), parent=submenu)

    submenu._animation_icon_workers.append(worker)
    applier.on_ready(None)                      # 空图：不 setIcon 但必须收口
    assert submenu._animation_icon_workers == [] and pump_calls == [1]
    assert action.icon().isNull()
    menu.deleteLater()
    _flush_deferred_delete()
