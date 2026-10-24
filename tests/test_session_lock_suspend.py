# -*- coding: utf-8 -*-
"""锁屏/挂起降档（O4）：WM_WTSSESSION_CHANGE + WM_POWERBROADCAST → driver.set_suspended。

缺口（审计原文）：会话探测器只认 WM_QUERYENDSESSION/WM_ENDSESSION，锁屏时
overlay 仍可见 → 档位停在 T1（AC 下满速）+ clip 24fps 解码跑整夜。

契约：
- 锁屏（WTS_SESSION_LOCK=7）/挂起（PBT_APMSUSPEND=4）→ ``set_suspended(True)``：
  强制 T3 + 逐 sprite pause_clip；
- 解锁（WTS_SESSION_UNLOCK=8）/恢复（PBT_APMRESUMEAUTOMATIC=0x12 /
  PBT_APMRESUMESUSPEND=7）→ ``set_suspended(False)``：note_kinetic 同帧回全速 +
  resume_clip；
- **只降档**：不 hide overlay、不动全屏/光标 watcher；
- 锁屏通知需 WTSRegisterSessionNotification 注册才会收到：注册失败降级为不支持
  并记日志（绝不抛）；非 Windows 平台无操作。

纪律：真 ``wintypes.MSG`` 结构 + ``ctypes.addressof``（同
``tests/test_session_watcher_message_read.py``）；不注册真实 WTS 通知（打桩 win32
边界），不起真 QTimer、不 sleep 赌时序。
"""
from __future__ import annotations

import ctypes
import logging
import sys

import pytest
from ctypes import wintypes
from PySide6.QtCore import QObject, Signal

from pet import session_watcher as sw_mod


def _msg_pointer(message: int, wparam: int = 0):
    """构造真实 MSG 结构并返回 (地址, 结构体)；结构体需保活到断言结束。"""
    msg = wintypes.MSG()
    msg.hwnd = 0
    msg.message = message
    msg.wParam = wparam
    msg.lParam = 0
    msg.time = 0
    msg.pt.x = 0
    msg.pt.y = 0
    return ctypes.addressof(msg), msg


class _FakeApp(QObject):
    """带 Qt 会话信号的假 app（只暴露探测器真正使用的接口）。"""

    commitDataRequest = Signal()
    aboutToQuit = Signal()


# ---------------------------------------------------------------- 消息解码
@pytest.mark.parametrize("wparam,expected", [
    (sw_mod.WTS_SESSION_LOCK, "session_lock"),
    (sw_mod.WTS_SESSION_UNLOCK, "session_unlock"),
])
def test_session_change_message_decodes_lock_state(wparam, expected):
    addr, keepalive = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE, wparam)
    assert sw_mod.session_power_event(addr) == expected
    assert keepalive is not None


@pytest.mark.parametrize("wparam,expected", [
    (sw_mod.PBT_APMSUSPEND, "power_suspend"),
    (sw_mod.PBT_APMRESUMEAUTOMATIC, "power_resume"),
    (sw_mod.PBT_APMRESUMESUSPEND, "power_resume"),
])
def test_power_broadcast_message_decodes_suspend_state(wparam, expected):
    addr, keepalive = _msg_pointer(sw_mod.WM_POWERBROADCAST, wparam)
    assert sw_mod.session_power_event(addr) == expected
    assert keepalive is not None


def test_unrelated_messages_and_bogus_pointers_return_none():
    addr, keepalive = _msg_pointer(0x000F)          # WM_PAINT
    assert sw_mod.session_power_event(addr) is None
    # 会话结束消息不由本函数负责（走既有 session_end_reason）
    addr2, keepalive2 = _msg_pointer(sw_mod.WM_QUERYENDSESSION)
    assert sw_mod.session_power_event(addr2) is None
    # 同一条消息的非挂起 power 事件（如 PBT_APMQUERYSUSPEND=0）不算
    addr3, keepalive3 = _msg_pointer(sw_mod.WM_POWERBROADCAST, 0x0000)
    assert sw_mod.session_power_event(addr3) is None
    assert sw_mod.session_power_event(None) is None
    assert sw_mod.session_power_event(0) is None
    assert sw_mod.session_power_event("not-a-pointer") is None
    assert keepalive is not None and keepalive2 is not None and keepalive3 is not None


def test_non_session_message_skips_full_struct_copy(monkeypatch):
    """窄读纪律不变：非（会话结束/电源）消息只读 message 字段，不整结构拷贝。"""
    calls: list = []
    real = ctypes.string_at

    def _recording(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(sw_mod.ctypes, "string_at", _recording)
    addr, keepalive = _msg_pointer(0x000F)          # WM_PAINT
    assert sw_mod.session_end_reason(addr) is None
    assert calls == []
    assert keepalive is not None


# ---------------------------------------------------------------- 探测器接线
def test_native_filter_reports_lock_and_unlock():
    app = _FakeApp()
    events: list = []
    watcher = sw_mod.SessionWatcher(app=app, on_session_end=lambda: None,
                                    install_native_filter=False,
                                    on_suspend_change=(
                                        lambda active, reason:
                                        events.append((active, reason))))
    try:
        addr, keep1 = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                   sw_mod.WTS_SESSION_LOCK)
        assert watcher.nativeEventFilter(0, addr) == (False, 0)
        addr2, keep2 = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                    sw_mod.WTS_SESSION_UNLOCK)
        watcher.nativeEventFilter(0, addr2)
        addr3, keep3 = _msg_pointer(sw_mod.WM_POWERBROADCAST,
                                    sw_mod.PBT_APMSUSPEND)
        watcher.nativeEventFilter(0, addr3)
        addr4, keep4 = _msg_pointer(sw_mod.WM_POWERBROADCAST,
                                    sw_mod.PBT_APMRESUMEAUTOMATIC)
        watcher.nativeEventFilter(0, addr4)

        assert events == [(True, "session_lock"), (False, "session_unlock"),
                          (True, "power_suspend"), (False, "power_resume")]
        assert watcher.armed is False, "锁屏不是会话结束：绝不许置位关机闸门"
    finally:
        app.deleteLater()
        assert keep1 is not None and keep2 is not None
        assert keep3 is not None and keep4 is not None


def test_suspend_callback_failure_is_isolated():
    """挂起回调抛异常不得打断原生过滤器（只观测、不拦截）。"""
    app = _FakeApp()

    def _boom(active, reason):
        raise RuntimeError("挂起接线失败")

    watcher = sw_mod.SessionWatcher(app=app, on_session_end=lambda: None,
                                    install_native_filter=False,
                                    on_suspend_change=_boom)
    try:
        addr, keepalive = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                       sw_mod.WTS_SESSION_LOCK)
        assert watcher.nativeEventFilter(0, addr) == (False, 0)
    finally:
        app.deleteLater()
        assert keepalive is not None


def test_suspend_repeat_is_forwarded_but_state_is_tracked():
    """重复的同一状态仍转发（幂等由消费方保证），但绝不影响关机 latch。"""
    app = _FakeApp()
    events: list = []
    watcher = sw_mod.SessionWatcher(app=app, on_session_end=lambda: None,
                                    install_native_filter=False,
                                    on_suspend_change=(
                                        lambda active, reason: events.append(active)))
    try:
        addr, keepalive = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                      sw_mod.WTS_SESSION_LOCK)
        watcher.nativeEventFilter(0, addr)
        watcher.nativeEventFilter(0, addr)
        assert events == [True, True]
        assert watcher.armed is False
    finally:
        app.deleteLater()
        assert keepalive is not None


# ---------------------------------------------------------------- WTS 注册（win32 边界打桩）
@pytest.mark.skipif(sys.platform != "win32",
                    reason="WTSRegisterSessionNotification 是 Windows 专有 API")
def test_register_session_notifications_is_idempotent_and_unregisters(monkeypatch):
    calls: list = []

    def _register(hwnd, flags):
        calls.append(("register", int(hwnd), int(flags)))
        return 1

    def _unregister(hwnd):
        calls.append(("unregister", int(hwnd)))
        return 1

    monkeypatch.setattr(sw_mod, "_wts_register_session_notification", _register)
    monkeypatch.setattr(sw_mod, "_wts_unregister_session_notification", _unregister)

    watcher = sw_mod.SessionWatcher(app=None, on_session_end=lambda: None,
                                    install_native_filter=False)
    assert watcher.register_session_notifications(12345) is True
    assert watcher.register_session_notifications(12345) is True   # 幂等
    assert calls == [("register", 12345, sw_mod.NOTIFY_FOR_THIS_SESSION)]

    watcher.unregister_session_notifications()

    assert calls == [("register", 12345, sw_mod.NOTIFY_FOR_THIS_SESSION),
                     ("unregister", 12345)]
    watcher.unregister_session_notifications()                     # 幂等
    assert len(calls) == 2


@pytest.mark.skipif(sys.platform != "win32",
                    reason="WTSRegisterSessionNotification 是 Windows 专有 API")
def test_register_failure_degrades_without_raising(monkeypatch, caplog):
    """注册失败 → 降级为不支持并记日志（绝不让启动路径炸掉）。"""
    def _register(hwnd, flags):
        raise OSError("wtsapi32 不可用")

    monkeypatch.setattr(sw_mod, "_wts_register_session_notification", _register)
    watcher = sw_mod.SessionWatcher(app=None, on_session_end=lambda: None,
                                    install_native_filter=False)
    with caplog.at_level(logging.INFO, logger="pet.session_watcher"):
        assert watcher.register_session_notifications(12345) is False
    assert any("降级为不支持" in r.getMessage() for r in caplog.records), "失败必须留痕"
    # 未注册成功 ⇒ 反注册不得调用（绝不误摘别人的注册）
    monkeypatch.setattr(sw_mod, "_wts_unregister_session_notification",
                        lambda hwnd: pytest.fail("未注册成功不得反注册"))
    watcher.unregister_session_notifications()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX 降级路径：Windows 上走真实 WTS 注册（上面两条用例覆盖）")
def test_register_session_notifications_noop_on_posix(monkeypatch):
    """非 Windows：注册/反注册为无操作——返回 False 且绝不触碰 WTS 边界。

    产品契约（session_watcher 模块 docstring / install_session_watcher）：非
    Windows 平台降级为不支持锁屏探测，注册调用静默短路。本用例把这条降级
    契约锁死，避免「Windows 专有」用例被门控后 POSIX 路径裸奔。
    """
    monkeypatch.setattr(sw_mod, "_wts_register_session_notification",
                        lambda hwnd, flags: pytest.fail("POSIX 上不得触碰 WTS 注册边界"))
    monkeypatch.setattr(sw_mod, "_wts_unregister_session_notification",
                        lambda hwnd: pytest.fail("POSIX 上不得触碰 WTS 反注册边界"))
    watcher = sw_mod.SessionWatcher(app=None, on_session_end=lambda: None,
                                    install_native_filter=False)
    assert watcher.register_session_notifications(12345) is False
    watcher.unregister_session_notifications()


# ---------------------------------------------------------------- OverlayShell 接线
def _make_shell(tmp_path):
    import tests.test_sprite_menu_facade as fac

    return fac._make_shell(tmp_path)


def _stub_sprite(shell):
    from tests.test_sprite_visibility import PausableStubSprite

    sprite = PausableStubSprite()
    shell.overlay.add_sprite(sprite)
    return sprite


def test_shell_installs_watcher_with_suspend_callback_and_registers_hwnd(
        tmp_path, monkeypatch):
    """壳装配：探测器接上挂起回调，并对 overlay 句柄注册锁屏通知。"""
    from pet import overlay_shell as shell_mod

    installed: list = []
    registered: list = []

    class _Watcher:
        def register_session_notifications(self, hwnd):
            registered.append(int(hwnd))
            return True

        def unregister_session_notifications(self):
            registered.append("unregister")

    def _fake_install(*, app=None, on_session_end=None, on_suspend_change=None):
        installed.append((app, on_session_end, on_suspend_change))
        return _Watcher()

    monkeypatch.setattr(shell_mod, "install_session_watcher", _fake_install)
    shell, _lib = _make_shell(tmp_path)
    try:
        shell._install_session_watcher()

        assert len(installed) == 2, "构造期 + 显式调用各一次（幂等由装配方保证）"
        app, on_end, on_suspend = installed[-1]
        assert app is shell.app
        assert on_end == shell._on_session_end
        assert on_suspend == shell._on_suspend_changed, "挂起回调必须接线到壳"
        assert registered[-1] == int(shell.overlay.winId()), "必须对 overlay 句柄注册"
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_shell_quit_unregisters_session_notifications(tmp_path):
    """退出收口必须反注册锁屏通知（与窗口句柄同生共死）。"""
    shell, _lib = _make_shell(tmp_path)
    calls: list = []

    class _Watcher:
        def unregister_session_notifications(self):
            calls.append("unregister")

    try:
        shell._session_watcher = _Watcher()
        shell._on_about_to_quit()

        assert calls == ["unregister"]
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_shell_suspend_downgrades_without_hiding_and_resumes_clips(tmp_path):
    """锁屏/挂起只降档：clips 停/续 + 档位 T3→T0，overlay 可见性一动不动。"""
    from pet.tick_governor import TIER_ACTIVE, TIER_OCCLUDED

    shell, _lib = _make_shell(tmp_path)
    try:
        shell.overlay.show()
        sprite = _stub_sprite(shell)

        shell._on_suspend_changed(True, "session_lock")

        assert shell.driver.suspended is True
        assert shell.driver.applied_tier == TIER_OCCLUDED
        assert (sprite.pause_calls, sprite.resume_calls) == (1, 0)
        assert shell.overlay.isVisible() is True, "只降档：绝不隐藏 overlay"

        shell._on_suspend_changed(False, "session_unlock")

        assert shell.driver.suspended is False
        assert shell.driver.applied_tier == TIER_ACTIVE
        assert (sprite.pause_calls, sprite.resume_calls) == (1, 1)
        assert shell.overlay.isVisible() is True
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_shell_suspend_callback_swallows_errors(tmp_path):
    """接线容错：驱动器异常不得让原生消息链抛（事件过滤器在 Qt 事件循环里）。"""
    shell, _lib = _make_shell(tmp_path)

    class _BoomDriver:
        def set_suspended(self, active):
            raise RuntimeError("驱动故障")

    try:
        shell.driver = _BoomDriver()
        shell._on_suspend_changed(True, "power_suspend")   # 不许抛
    finally:
        shell.driver = None
        shell.overlay.close()
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 缺陷 12：安装契约
def _real_app():
    """真实 QApplication：安装契约必须走真 Qt 类型检查，替身测不出 TypeError。"""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_watcher_satisfies_qt_native_filter_contract():
    """SessionWatcher 必须是 QAbstractNativeEventFilter，而不是普通 QObject。

    缺陷 12：类只继承 QObject 时 ``installNativeEventFilter(self)`` 直接
    TypeError（本机探针：'called with wrong argument types ... Supported
    signatures: installNativeEventFilter(filterObj: QAbstractNativeEventFilter)'），
    异常被 install() 的宽 except 吞掉后仍置 ``_installed=True`` 报成功——WTS
    注册成功也收不到 ``WM_WTSSESSION_CHANGE``，锁屏/挂起降载在生产从未生效。
    """
    from PySide6.QtCore import QAbstractNativeEventFilter

    watcher = sw_mod.SessionWatcher(app=None, on_session_end=lambda: None,
                                    install_native_filter=False)
    assert isinstance(watcher, QAbstractNativeEventFilter), \
        "Qt 只接受 QAbstractNativeEventFilter（普通 QObject 会被拒）"
    assert watcher._installed is False, "未安装（app=None）时不得谎报已安装"


@pytest.mark.skipif(sys.platform != "win32",
                    reason="原生过滤器安装仅在 Windows 启用（install() 的 os.name 门）")
def test_install_really_hands_the_filter_to_qapplication():
    """install() 必须真把 self 交给 QApplication——不是吞掉异常后的假成功。"""
    from PySide6.QtWidgets import QApplication

    application = _real_app()
    seen: list = []
    real = QApplication.installNativeEventFilter
    QApplication.installNativeEventFilter = (
        lambda self, obj: (seen.append(obj), real(self, obj))[1])
    watcher = sw_mod.SessionWatcher(app=application, on_session_end=lambda: None)
    try:
        assert watcher.install() is True
        assert seen == [watcher], "安装调用必须真的落到 QApplication 上"
        assert watcher._installed is True
        assert watcher.install() is True          # 幂等
        assert len(seen) == 1
    finally:
        del QApplication.installNativeEventFilter
        application.removeNativeEventFilter(watcher)


@pytest.mark.skipif(sys.platform != "win32",
                    reason="原生过滤器安装仅在 Windows 启用（install() 的 os.name 门）")
def test_uninstall_detaches_filter_and_is_idempotent():
    """收口时必须能摘掉原生过滤器（Qt 只存裸指针，不给它留悬垂对象）。"""
    from PySide6.QtWidgets import QApplication

    application = _real_app()
    watcher = sw_mod.SessionWatcher(app=application, on_session_end=lambda: None)
    assert watcher.install() is True

    assert watcher.uninstall() is True
    assert watcher._installed is False
    assert watcher.uninstall() is False       # 幂等


@pytest.mark.skipif(sys.platform != "win32",
                    reason="原生过滤器只在 Windows 上安装（install() 的平台闸门）")
def test_install_failure_is_loud_and_never_reports_success(caplog):
    """装了但没装上：warning 留痕 + ``_installed`` 保持 False（绝不谎报成功）。"""
    class _RejectingApp(_FakeApp):
        def installNativeEventFilter(self, watcher):
            raise RuntimeError("Qt 拒绝该过滤器")

    fake_app = _RejectingApp()
    watcher = sw_mod.SessionWatcher(app=fake_app, on_session_end=lambda: None)
    try:
        with caplog.at_level(logging.WARNING, logger="pet.session_watcher"):
            assert watcher.install() is False
        assert watcher._installed is False, "装失败不得置成功位（否则再没人重试/排查）"
        assert any(r.levelno >= logging.WARNING for r in caplog.records), \
            "失败必须 warning 级留痕（线上日志从无锁屏降载记录，正是静默 debug 的代价）"
        assert watcher._signals_connected is True, "降级路径仍须保留 Qt 会话信号兜底"
    finally:
        fake_app.deleteLater()


@pytest.mark.skipif(sys.platform != "win32",
                    reason="原生过滤器只在 Windows 上安装（install() 的平台闸门）")
def test_install_degrades_on_app_without_native_filter_api(caplog):
    """替身 app 没有 installNativeEventFilter：降级、留痕、不报成功（桩路径不红）。"""
    fake_app = _FakeApp()
    watcher = sw_mod.SessionWatcher(app=fake_app, on_session_end=lambda: None)
    try:
        with caplog.at_level(logging.WARNING, logger="pet.session_watcher"):
            assert watcher.install() is False
        assert watcher._installed is False
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
        assert watcher._signals_connected is True
    finally:
        fake_app.deleteLater()


def test_native_filter_accepts_shiboken_voidptr_message():
    """真实传参形态：PySide6 6.x 给的是 ``shiboken6.VoidPtr``，不是 int。

    只认 int 的实现在真机上一条消息都解不出来（本机探针：``windows_generic_MSG``
    路径的 message 是 ``VoidPtr(0x...)``）——锁屏消息收得到也判不出来，锁屏
    降载同样不生效。VoidPtr 与 int 地址必须一视同仁。
    """
    import shiboken6

    fake_app = _FakeApp()
    events: list = []
    watcher = sw_mod.SessionWatcher(app=fake_app, on_session_end=lambda: None,
                                    install_native_filter=False,
                                    on_suspend_change=(
                                        lambda active, reason:
                                        events.append((active, reason))))
    try:
        addr, keepalive = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                       sw_mod.WTS_SESSION_LOCK)
        assert watcher.nativeEventFilter(0, shiboken6.VoidPtr(addr)) == (False, 0)
        addr2, keepalive2 = _msg_pointer(sw_mod.WM_QUERYENDSESSION)
        watcher.nativeEventFilter(0, shiboken6.VoidPtr(addr2))
        assert events == [(True, "session_lock")]
        assert watcher.armed is True, "VoidPtr 形态的关机消息同样必须置位"
    finally:
        fake_app.deleteLater()
        assert keepalive is not None and keepalive2 is not None


@pytest.mark.skipif(sys.platform != "win32",
                    reason="PostThreadMessageW 与原生事件过滤器都是 Windows 专有")
def test_real_native_lock_message_reaches_the_callback():
    """真投递一条 ``WM_WTSSESSION_CHANGE``：完整原生链必须把锁屏回调打出来。

    上一条用例只锁"装得上/类型对"，这条证明**收得到**——真
    ``PostThreadMessageW`` + 真事件循环走完 Qt 的 ``QAbstractEventDispatcher``
    → 原生过滤器 → MSG 解析 → 回调的整条链（含 PySide6 的真实传参形态
    VoidPtr）。投递到线程消息队列而不是某个 HWND，因此不依赖 QPA 平台：
    offscreen 下同样成立（offscreen 仍用 ``QEventDispatcherWin32``，实测同一条
    消息只会派发一次）。
    """
    import time

    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    events: list = []
    watcher = sw_mod.SessionWatcher(app=application, on_session_end=lambda: None,
                                    on_suspend_change=(
                                        lambda active, reason:
                                        events.append((active, reason))))
    try:
        assert watcher.install() is True
        posted = ctypes.windll.user32.PostThreadMessageW(
            ctypes.windll.kernel32.GetCurrentThreadId(),
            sw_mod.WM_WTSSESSION_CHANGE, sw_mod.WTS_SESSION_LOCK, 0)
        assert posted != 0, "前提：消息确实投进了本线程的队列"
        # 事件同步：泵消息直到回调到达，宽预算兜住慢 runner（不猜时序）
        deadline = time.monotonic() + 10.0
        while not events and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.01)
        assert events == [(True, "session_lock")]
    finally:
        watcher.uninstall()
