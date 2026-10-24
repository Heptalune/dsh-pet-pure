# -*- coding: utf-8 -*-
"""会话结束探测（Windows 关机/注销）：issue #111。

问题：桌宠对「操作系统已宣告会话结束」零感知。关机时动画链仍在正常运转，
随时 CreateProcess 新的 ffmpeg 取帧进程；此时登录会话（窗口站/桌面堆/CSRSS）
已在拆除，新进程的 user32/gdi32 初始化失败（0xc0000142），系统弹出错误对话框
阻塞关机。ffmpeg 启停频率高（冷首帧预热/reader 换代/元数据探测/圈末回收），
撞上关机窗口的概率因此几乎为 100%。

本模块只做两件事：
1. **探测**会话结束：Windows 上经应用级原生事件过滤器捕获
   ``WM_QUERYENDSESSION`` / ``WM_ENDSESSION``；并连接 Qt 会话框架信号
   ``commitDataRequest`` / ``aboutToQuit`` 作次生兜底。
2. **置位**进程级闸门 ``pet.webm_clip.set_session_ending()``，并调用注入的
   安全网回调（``AppShell._on_session_end``）终止现有 reader。

除此之外还观测**锁屏/挂起**（O4）：``WM_WTSSESSION_CHANGE``（需
``WTSRegisterSessionNotification`` 注册才收得到，见
``register_session_notifications``）与 ``WM_POWERBROADCAST``（广播，无需注册）
→ 注入的 ``on_suspend_change(active, reason)``。锁屏/挂起时窗口仍是
``isVisible()``，既有档位判定拿不到任何"不可见"信号，AC 供电下会一直满速空转、
clip 按帧率解码——故由这条事件链把档位压到 T3 并停播放节拍（只降档，不隐藏
窗口）。注册失败降级为不支持锁屏探测并记日志（挂起探测与关机探测不受影响）。

为什么原生过滤器是权威信号：``WM_QUERYENDSESSION`` 在会话拆除**之前**送达，
是唯一能真正赶在窗口期前生效的时机；Qt 会话框架的信号是次生路径，且在
Windows 上是否触发受 Qt 版本/会话管理器实现影响，不能单靠它。

平台：POSIX 上不安装原生过滤器（无此消息），仍保留闸门与信号接线，行为等价。
线程：只在 GUI 线程创建/安装/触发（原生事件过滤器与 Qt 信号都在 GUI 线程）。
"""
from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from typing import Callable, Optional

import shiboken6
from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject

from . import webm_clip

logger = logging.getLogger(__name__)

WM_QUERYENDSESSION = 0x0011  # 会话即将结束：关机/注销前的最后一次询问
WM_ENDSESSION = 0x0016       # 会话已结束（拆除开始）

# 锁屏/解锁（需 WTSRegisterSessionNotification 注册才收得到）与挂起/恢复
# （广播给全部顶层窗口，无需注册）。wParam 是本模块唯一关心的状态量。
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

#: 注册/反注册用的通知范围：只要本会话（``NOTIFY_FOR_THIS_SESSION``）
NOTIFY_FOR_THIS_SESSION = 0

#: 只关心的消息号 → 日志用 reason。**先比对消息号再解引用**：事件过滤器每帧都
#: 会被调用，绝不能对任意消息都去读 lParam 指向的内存（非 WM_* 消息的 lParam
#: 是任意值，当作指针解引用会触发访问违规——实测在 CPython 上表现为
#: "Windows fatal exception: access violation"）。
_SESSION_END_MESSAGES = {
    WM_QUERYENDSESSION: 'native_query_end_session',
    WM_ENDSESSION: 'native_end_session',
}

#: 锁屏/挂起消息：wParam → (是否挂起中, 日志标签)
_SUSPEND_MESSAGES = {
    WM_WTSSESSION_CHANGE: {
        WTS_SESSION_LOCK: (True, 'session_lock'),
        WTS_SESSION_UNLOCK: (False, 'session_unlock'),
    },
    WM_POWERBROADCAST: {
        PBT_APMSUSPEND: (True, 'power_suspend'),
        PBT_APMRESUMESUSPEND: (False, 'power_resume'),
        PBT_APMRESUMEAUTOMATIC: (False, 'power_resume'),
    },
}

# Windows MSG 结构（原生事件过滤器的 message 指针在 Windows 上即 MSG*）。
# 必须用真实 ctypes 结构解析：字段布局错误会让解析静默失效（假绿）。
_MSG_FIELDS = [
    ('hwnd', wintypes.HWND),
    ('message', wintypes.UINT),
    ('wParam', wintypes.WPARAM),
    ('lParam', wintypes.LPARAM),
    ('time', wintypes.DWORD),
    ('pt', wintypes.POINT),
]


class _WinMsg(ctypes.Structure):
    """Windows MSG（仅取本模块需要的字段，布局与 winuser.h 一致）。"""

    _fields_ = _MSG_FIELDS

    def message_id(self) -> int:
        return int(self.message)


#: ``message`` 字段在该结构里的字节偏移：由 ctypes 布局算出，不硬编码
#: （winuser.h 里 MSG 各字段宽度随位数/对齐变化，写死偏移会在别的架构上静默读错）。
_MSG_MESSAGE_OFFSET = _WinMsg.message.offset
_MSG_FIELD_TYPE = ctypes.c_uint  # MSG.message 是 UINT


def _message_address(message) -> int:
    """把原生事件过滤器的 ``message`` 参数归一成 MSG 地址（非指针形态返回 0）。

    PySide6 6.x 传给 ``nativeEventFilter`` 的是 ``shiboken6.VoidPtr``——实测
    （6.11，``windows_generic_MSG`` 路径）**不是 int**；而测试与部分调用方直接
    传 ``ctypes.addressof`` 得到的 int。两种形态在这里统一成整型地址：只认 int
    的实现会把真机上传进来的每一条消息都判成"不是消息"，锁屏/挂起/关机通知
    收得到也认不出来。其余形态（None / 字符串 / 已失效对象）一律返回 0，调用
    方据此短路，绝不拿非指针去做解引用。
    """
    if isinstance(message, int):
        return message if message > 0 else 0
    try:
        return max(0, int(message))
    except (TypeError, ValueError):
        return 0


def session_end_reason(message) -> Optional[str]:
    """把原生事件过滤器的 message 参数翻译成会话结束原因（非会话消息返回 None）。

    读取分两步（O2）：**先只读 ``MSG.message`` 一个字段**（4 字节）判断是不是
    会话消息——事件过滤器对**每一条** Windows 消息都会被调用，先前的实现每条
    都做 ``string_at(48B)`` + ``from_buffer_copy(48B)`` 才拿到消息号，绝大多数
    消息（WM_PAINT/WM_MOUSEMOVE…）的这次整结构拷贝纯属浪费；只有确认是关机/
    注销消息时才完整解析 MSG（用于 wParam/lParam 留痕）。

    ``message`` 的形态差异（PySide6 的 VoidPtr / 测试的 int 地址）统一由
    :func:`_message_address` 归一。解析失败（非 Windows / 指针失效）一律返回
    None——本模块只做观测，任何异常都必须吞掉，绝不让 Qt 事件循环因探测器崩掉。
    """
    address = _message_address(message)
    if address <= 0:
        return None
    try:
        msg_id = int(_MSG_FIELD_TYPE.from_address(
            address + _MSG_MESSAGE_OFFSET).value)
        reason = _SESSION_END_MESSAGES.get(msg_id)
    except Exception:
        return None
    if reason is None:
        return None
    # 确认是会话消息后才完整解析（wParam/lParam 留痕：WM_ENDSESSION 的
    # lParam 带 ENDSESSION_LOGOFF 标志）。解析失败不影响判定——探测器宁可
    # 诊断少一行，也绝不能漏掉关机信号。
    try:
        win_msg = _WinMsg.from_buffer_copy(
            ctypes.string_at(address, ctypes.sizeof(_WinMsg)))
    except Exception:
        return reason
    logger.debug('原生会话消息 0x%04X wParam=%s lParam=%s',
                 int(win_msg.message), int(win_msg.wParam), int(win_msg.lParam))
    return reason


def _suspend_state(message) -> Optional[tuple]:
    """把原生消息翻译成 ``(是否挂起中, 日志标签)``；无关消息返回 None（O4）。

    与 :func:`session_end_reason` 同款纪律：先窄读 ``MSG.message`` 定消息号，
    命中锁屏/挂起消息才整结构解析取 wParam（其余消息一个字节都不多读——事件
    过滤器每条消息都会被调用）。不认识的 wParam（如 Windows 新增的电源事件、
    ``PBT_APMQUERYSUSPEND`` 查询）一律返回 None：只观测、不改状态。
    """
    address = _message_address(message)
    if address <= 0:
        return None
    try:
        msg_id = int(_MSG_FIELD_TYPE.from_address(
            address + _MSG_MESSAGE_OFFSET).value)
        states = _SUSPEND_MESSAGES.get(msg_id)
    except Exception:
        return None
    if states is None:
        return None
    try:
        win_msg = _WinMsg.from_buffer_copy(
            ctypes.string_at(address, ctypes.sizeof(_WinMsg)))
    except Exception:
        return None
    entry = states.get(int(win_msg.wParam))
    if entry is None:
        return None
    logger.debug('原生锁屏/挂起消息 0x%04X wParam=%s → %s',
                 int(win_msg.message), int(win_msg.wParam), entry[1])
    return entry


def session_power_event(message) -> Optional[str]:
    """锁屏/挂起消息的日志标签（无关消息返回 None；测试与日志的公开面）。"""
    entry = _suspend_state(message)
    return entry[1] if entry is not None else None


# ---------------------------------------------------------------- WTS 注册边界
def _wts_register_session_notification(hwnd: int, flags: int) -> int:
    """``WTSRegisterSessionNotification`` 边界：返回 0 表示未注册。

    只在这里碰 win32（测试打桩本函数即替换系统调用）。
    """
    import ctypes as _ctypes

    return int(_ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
        _ctypes.c_void_p(int(hwnd)), _ctypes.c_uint(int(flags))))


def _wts_unregister_session_notification(hwnd: int) -> int:
    """``WTSUnRegisterSessionNotification`` 边界（注意 win32 里的拼写无 Register 的 r）。"""
    import ctypes as _ctypes

    return int(_ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(
        _ctypes.c_void_p(int(hwnd))))


class SessionWatcher(QObject, QAbstractNativeEventFilter):
    """会话结束探测器：置位 ffmpeg spawn 闸门 + 跑安全网回调（幂等）。

    生命周期：由 AppShell 创建并强引用持有，进程存活期间常驻。

    双继承不可省（缺陷 12）：``installNativeEventFilter`` 只接受
    ``QAbstractNativeEventFilter``——普通 QObject 子类会直接 TypeError，
    于是 WTS 注册成功也收不到 ``WM_WTSSESSION_CHANGE``。
    """

    def __init__(self, app=None, on_session_end: Optional[Callable[[], None]] = None,
                 install_native_filter: bool = True,
                 on_suspend_change: Optional[Callable[[bool, str], None]] = None) -> None:
        # 两个基类必须各自显式初始化：``super().__init__()`` 只走到第一个基类
        # （QObject），漏掉 ``QAbstractNativeEventFilter.__init__`` 时 C++ 侧的
        # 过滤器子对象没建起来，Qt 的原生消息派发不会回调到这里——实测表现为
        # "装上了但一条消息都收不到"（与"根本没装上"一样静默）。
        QObject.__init__(self, None)
        QAbstractNativeEventFilter.__init__(self)
        self._app = app if app is not None else QCoreApplication.instance()
        self._on_session_end = on_session_end
        self._on_suspend_change = on_suspend_change
        self._install_native_filter = bool(install_native_filter)
        self._armed = False
        self._installed = False
        self._signals_connected = False
        # 锁屏通知注册状态：已注册的 hwnd（0 = 未注册/注册失败降级）
        self._wts_hwnd = 0

    # ------------------------------------------------------------ 状态
    @property
    def armed(self) -> bool:
        """是否已收到会话结束通知（幂等 latch）。"""
        return self._armed

    # ------------------------------------------------------------ 安装
    def install(self) -> bool:
        """安装原生过滤器并接线 Qt 会话信号（幂等；无 QApplication 时为无操作）。

        返回 True 只代表**真装上了**：环境不具备安装条件（无 QApplication /
        非 Windows / 过滤器被 Qt 拒绝）时返回 False 并记 warning——置一个假的
        成功位就再没人重试，日志里也看不出锁屏/挂起探测其实没接入（缺陷 12：
        线上日志从无"触发因=suspended"，与"装失败报成功"互相印证）。
        """
        if self._installed:
            return True
        if self._app is None or not shiboken6.isValid(self._app):
            return False
        self.connect_app_signals()
        if self._install_native_filter and os.name == 'nt':
            try:
                self._app.installNativeEventFilter(self)
            except AttributeError:
                # 鸭子类型替身（测试桩）没有该方法：只保留信号兜底路径
                logger.warning('app 无 installNativeEventFilter（替身/异常环境）：'
                               '关机与锁屏探测降级为 Qt 会话信号兜底')
                return False
            except Exception:
                logger.warning('安装会话结束原生事件过滤器失败：'
                               '关机与锁屏探测降级为 Qt 会话信号兜底', exc_info=True)
                return False
        self._installed = True
        return True

    def uninstall(self) -> bool:
        """摘掉应用级原生过滤器（幂等；退出收口调用）。

        Qt 的过滤器表只存裸指针、不持有对象：探测器被销毁而过滤器还挂着，
        下一条原生消息就会回调到已释放对象上。故退出收口必须显式摘除。
        """
        if not self._installed:
            return False
        self._installed = False
        app_ = self._app
        if app_ is None or not shiboken6.isValid(app_):
            return False
        try:
            app_.removeNativeEventFilter(self)
        except Exception:
            logger.warning('摘除会话结束原生事件过滤器失败', exc_info=True)
            return False
        return True

    def connect_app_signals(self) -> bool:
        """连接 Qt 会话框架信号（次生兜底路径；幂等）。"""
        if self._signals_connected or self._app is None:
            return False
        connected = False
        for name, reason in (('commitDataRequest', 'qt_commit_data_request'),
                             ('aboutToQuit', 'about_to_quit')):
            signal = getattr(self._app, name, None)
            if signal is None:
                continue
            try:
                signal.connect(lambda _reason=reason: self.arm(_reason))
            except Exception:
                logger.debug('连接 %s 会话信号失败', name, exc_info=True)
            else:
                connected = True
        self._signals_connected = connected
        return connected

    # ------------------------------------------------------------ 原生过滤器
    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt API
        """应用级原生事件过滤器：Windows 关机/注销 → 置位闸门；锁屏/挂起 → 降档。

        恒返回 ``(False, 0)``（返回值 + ``qintptr *result`` 出参）：只观测、不
        拦截——绝不 veto 关机，也不改变 Qt 的默认处理（Qt 对 WM_QUERYENDSESSION
        的应答语义保持原样）。``event_type`` 是 Qt 给的 ``QByteArray``
        （Windows 上恒为 ``windows_generic_MSG``），本模块不解释它。

        两条分支互不影响：会话结束 latch 一旦置位就不再观测（关机窗口里锁屏
        消息没有意义）；锁屏/挂起在任何时候都转发给 ``on_suspend_change``
        （回调异常单独隔离，绝不影响关机探测）。
        """
        if not self._armed:
            reason = session_end_reason(message)
            if reason is not None:
                self.arm(reason)
                return (False, 0)
        self._report_suspend(message)
        return (False, 0)

    def _report_suspend(self, message) -> None:
        """锁屏/挂起消息 → ``on_suspend_change(active, reason)``（异常隔离）。"""
        if self._on_suspend_change is None:
            return
        try:
            entry = _suspend_state(message)
        except Exception:
            return  # 探测器只做观测：解析失败绝不打断 Qt 事件循环
        if entry is None:
            return
        try:
            self._on_suspend_change(bool(entry[0]), entry[1])
        except Exception:
            logger.exception('锁屏/挂起降档回调失败（只观测，不影响事件循环）')

    # ------------------------------------------------------------ 锁屏通知注册
    def register_session_notifications(self, hwnd) -> bool:
        """对给定窗口句柄注册会话通知（Windows 才收得到 WM_WTSSESSION_CHANGE）。

        注册失败（``wtsapi32`` 不可用 / API 返回 0 / 非 Windows）→ 返回 False 并
        记日志：**降级为不支持锁屏探测**，挂起/恢复（WM_POWERBROADCAST 无需
        注册）与关机探测照常工作。幂等：同一 hwnd 不重复注册。
        """
        if os.name != 'nt':
            return False
        hwnd = int(hwnd or 0)
        if hwnd == 0:
            logger.info('会话通知注册跳过（无窗口句柄）：锁屏探测降级为不支持')
            return False
        if self._wts_hwnd == hwnd:
            return True
        if self._wts_hwnd:
            self.unregister_session_notifications()
        try:
            ok = bool(_wts_register_session_notification(
                hwnd, NOTIFY_FOR_THIS_SESSION))
        except Exception:
            logger.info('会话通知注册失败（锁屏探测降级为不支持）', exc_info=True)
            return False
        if not ok:
            logger.info('会话通知注册被拒绝 hwnd=%s（锁屏探测降级为不支持）', hwnd)
            return False
        self._wts_hwnd = hwnd
        logger.debug('会话通知已注册 hwnd=%s（锁屏/解锁可探测）', hwnd)
        return True

    def unregister_session_notifications(self) -> None:
        """关闭时反注册（幂等；未注册成功时绝不去摘别人的注册）。"""
        hwnd, self._wts_hwnd = self._wts_hwnd, 0
        if not hwnd or os.name != 'nt':
            return
        try:
            _wts_unregister_session_notification(hwnd)
        except Exception:
            logger.debug('会话通知反注册失败 hwnd=%s', hwnd, exc_info=True)

    # ------------------------------------------------------------ 触发
    def arm(self, reason='') -> None:
        """置位会话结束（幂等）：先关 spawn 闸门，再跑安全网回调。

        顺序不可颠倒：闸门先落，后续任何代码路径、任何回调异常都不可能再让
        ffmpeg 起来。回调只跑一次（重复的 WM_QUERYENDSESSION/WM_ENDSESSION 与
        aboutToQuit 都会到这里）。

        ``reason`` 是日志标签：Qt 的 ``commitDataRequest`` 会把 ``QSessionManager``
        作为信号参数传进来（实测打包产物日志里出现过对象 repr），非字符串一律
        归一成 ``unknown``——日志是关机阶段唯一的排查入口，不能印对象地址。
        """
        if self._armed:
            return
        self._armed = True
        label = reason if isinstance(reason, str) and reason else 'unknown'
        logger.info(
            '收到会话结束通知（%s）：停止派生 ffmpeg 子进程并静默退出', label,
        )
        self.apply_session_ending()
        if self._on_session_end is not None:
            try:
                self._on_session_end()
            except Exception:
                logger.exception('会话结束收口失败（闸门已置位，不再派生进程）')

    def apply_session_ending(self) -> None:
        """只置位 spawn 闸门（安全网回调的前置步，异常隔离）。"""
        try:
            webm_clip.set_session_ending(True)
        except Exception:
            logger.debug('置位会话结束闸门失败', exc_info=True)


def install_session_watcher(app=None, on_session_end=None,
                            on_suspend_change=None) -> SessionWatcher:
    """便捷入口：创建并安装探测器（AppShell / OverlayShell 使用）。

    平台判定在 ``SessionWatcher.install()`` 内完成：只有 Windows
    （``os.name == 'nt'``）才注册原生事件过滤器，POSIX 上仅保留闸门与 Qt
    会话信号接线。锁屏通知的注册（``register_session_notifications``）由调用方
    在拿到窗口句柄后单独触发（非 Windows 上是 no-op）。
    """
    watcher = SessionWatcher(app=app, on_session_end=on_session_end,
                             on_suspend_change=on_suspend_change)
    watcher.install()
    return watcher
