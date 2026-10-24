# -*- coding: utf-8 -*-
"""进程级全局快捷键服务：``RegisterHotKey`` → ``WM_HOTKEY`` → 回调。

三个动作用户可各绑一个组合键（默认**都不绑**，行为与加这个功能之前逐位相同）：

===============  ============================================  ==========================
动作             落在哪个既有入口                                备注
===============  ============================================  ==========================
``open_chat``    ``PetInstance.open_chat``                     右键菜单「AI 对话」同一条
``look_screen``  ``OverlayShell.look_at_screen(sprite=None)``   右键「看看屏幕」；``None`` = 主宠
``screen_capture`` ``OverlayShell.on_screen_capture``           右键「截图提问」同一条
===============  ============================================  ==========================

纪律：

- **不复制实现**：三个回调由 :class:`~pet.app.AppShell` 注入，这里只负责按键 → 动作。
- **独占失败要报**：Windows 的全局热键表是独占的，``RegisterHotKey`` 失败（通常是
  已被别的程序占用）时把失败项返回给调用方与日志，绝不假装成功。
- **回调不在原生消息里直接跑**：``WM_HOTKEY`` 是在事件过滤器中同步到达的，重活
  （弹窗、开会话、截图）必须在事件循环里跑，故统一 ``QTimer.singleShot(0, ...)``。
- **平台闸门**：非 Windows 不注册（``is_supported()`` 为假），设置页据此提示。
"""

from __future__ import annotations

import ctypes
import logging
import os
from typing import Callable, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, QTimer

from . import hotkey_spec
from . import platform_win

logger = logging.getLogger(__name__)

#: ``WM_HOTKEY``（winuser.h）
WM_HOTKEY = 0x0312

#: 动作 → 配置键。键名与设置行的 ``key`` 同名（「已修改」标记与单项重置才会自动可比）。
ACTIONS: dict[str, str] = {
    "open_chat": "hotkey_open_chat",
    "look_screen": "hotkey_look_screen",
    "screen_capture": "hotkey_screen_capture",
}

#: 动作的中文名（日志/提示用）
ACTION_LABELS: dict[str, str] = {
    "open_chat": "AI 对话",
    "look_screen": "看看屏幕",
    "screen_capture": "截图提问",
}

#: 注册 id 基址（进程内唯一即可；不与 Windows 预定义热键冲突）
_BASE_ID = 0xA5D0

#: 存活的服务实例（弱引用）：**热键 id 是进程级的**，两个实例会互相覆盖同一批 id。
#: 产品里只有 AppShell 一个创建点，这里加一条"出现第二个就告警"的护栏，
#: 免得将来有人顺手在别处再建一个、静默把前一个的绑定顶掉。
_LIVE_SERVICES: list = []


def is_supported() -> bool:
    """当前平台能否注册系统级热键（只有 Windows 有这条路）。"""
    return os.name == "nt"


class GlobalHotkeyService(QObject, QAbstractNativeEventFilter):
    """安装/卸载系统级热键并把按键分发给注入的回调。

    双继承不可省（与 ``session_watcher.SessionWatcher`` 同一个坑）：
    ``installNativeEventFilter`` 只接受 ``QAbstractNativeEventFilter``，普通
    ``QObject`` 子类会直接 TypeError，于是热键注册成功也收不到 ``WM_HOTKEY``。
    两个基类的 ``__init__`` 必须各自显式调用。
    """

    def __init__(self, app=None, *, user32=None, install_native_filter: bool = True,
                 supported: Optional[bool] = None) -> None:
        QObject.__init__(self, None)
        QAbstractNativeEventFilter.__init__(self)
        self._app = app if app is not None else QCoreApplication.instance()
        self._user32 = user32
        self._install_native_filter = bool(install_native_filter)
        self._supported = is_supported() if supported is None else bool(supported)
        self._installed = False
        #: 热键 id → 动作名
        self._id_to_action: dict[int, str] = {}
        #: 动作名 → 已注册的规范字符串（"未生效"的动作不在这里）
        self._active: dict[str, str] = {}
        self._callbacks: dict[str, Callable[[], None]] = {}

    # ------------------------------------------------------------ 状态
    @property
    def installed(self) -> bool:
        return self._installed

    @property
    def supported(self) -> bool:
        return self._supported

    def active_bindings(self) -> dict[str, str]:
        """当前**真的注册成功**的绑定（设置页/诊断读它；不含被占用而失败的）。"""
        return dict(self._active)

    # ------------------------------------------------------------ 生命周期
    def install(self) -> bool:
        """装原生事件过滤器（幂等）。返回是否真的装上了。"""
        if self._installed:
            return True
        if not self._supported:
            return False
        import weakref

        _LIVE_SERVICES[:] = [ref for ref in _LIVE_SERVICES if ref() not in (None, self)]
        if any(ref() is not None for ref in _LIVE_SERVICES):
            logger.warning("全局快捷键：进程内出现了第二个服务实例——热键 id 是进程级的，"
                           "后装的会把先装的绑定顶掉（产品应只有 AppShell 一个创建点）")
        _LIVE_SERVICES.append(weakref.ref(self))
        if self._app is None:
            return False
        if self._install_native_filter:
            try:
                self._app.installNativeEventFilter(self)
            except AttributeError:
                logger.warning("全局快捷键：应用对象没有 installNativeEventFilter（替身环境），"
                               "按键不会生效")
                return False
            except Exception:
                logger.warning("全局快捷键：安装原生事件过滤器失败，按键不会生效",
                               exc_info=True)
                return False
        self._installed = True
        return True

    def uninstall(self) -> bool:
        """注销全部热键并摘掉过滤器（退出收口；幂等）。

        过滤器表只存裸指针：对象被销毁而过滤器还挂着，下一条原生消息就会回调到
        已释放对象上——退出路径必须显式摘除（同 ``SessionWatcher``）。
        """
        self.clear()
        if not self._installed:
            return False
        self._installed = False
        app_ = self._app
        if app_ is not None:
            try:
                app_.removeNativeEventFilter(self)
            except Exception:
                logger.debug("全局快捷键：摘除原生事件过滤器失败", exc_info=True)
        return True

    # ------------------------------------------------------------ 绑定
    def apply(self, bindings: dict[str, str],
              callbacks: dict[str, Callable[[], None]]) -> list[tuple[str, str, str]]:
        """按 ``bindings`` 重设全部热键；返回失败项 ``(动作, 文本, 原因)``。

        每次都是「全量重设」：先注销旧的再按新表注册，避免"改键后旧键还活着"。
        空字符串 = 该动作不绑（不注册、不报错）。
        """
        self.clear()
        self._callbacks = dict(callbacks or {})
        if not self._supported:
            return []
        failures: list[tuple[str, str, str]] = []
        for index, (action, key) in enumerate(ACTIONS.items()):
            text = str((bindings or {}).get(action) or "").strip()
            if not text:
                continue
            parsed = hotkey_spec.parse(text)
            if parsed is None:
                failures.append((action, text, "无法识别这个组合键"))
                continue
            modifiers, vk = parsed
            hotkey_id = _BASE_ID + index
            ok = platform_win._register_windows_hotkey(  # noqa: SLF001 - 同包协作面
                hotkey_id, modifiers | hotkey_spec.MOD_NOREPEAT, vk, 0, self._user32)
            if not ok:
                failures.append((action, text, "注册失败：这个组合键可能已被其他程序占用"))
                continue
            self._id_to_action[hotkey_id] = action
            self._active[action] = hotkey_spec.normalize(text)
        for action, text, reason in failures:
            logger.warning("全局快捷键：%s（%s）%s",
                           ACTION_LABELS.get(action, action), text, reason)
        if self._active:
            logger.info("全局快捷键已生效：%s", "，".join(
                f"{ACTION_LABELS.get(a, a)}={t}" for a, t in self._active.items()))
        return failures

    def clear(self) -> None:
        """注销全部已注册热键（幂等；不摘过滤器）。

        注销失败（API 返回 False 或抛异常）只记日志并照旧丢弃记账：热键 id 到期
        由进程回收，硬留着记账反而会挡住后续重新注册同一个 id。
        """
        for hotkey_id in list(self._id_to_action):
            try:
                ok = platform_win._unregister_windows_hotkey(  # noqa: SLF001 - 同包协作面
                    hotkey_id, 0, self._user32)
            except Exception:
                logger.warning("全局快捷键：注销失败 id=%s", hotkey_id, exc_info=True)
                continue
            if not ok:
                logger.warning("全局快捷键：注销返回失败 id=%s（可能已被系统回收）",
                               hotkey_id)
        self._id_to_action.clear()
        self._active.clear()

    # ------------------------------------------------------------ 原生消息
    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt 命名
        """收 ``WM_HOTKEY`` 并派发（Qt 的签名，返回 ``(是否吞掉, 结果)``）。"""
        try:
            if event_type not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                return False, 0
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if msg.message != WM_HOTKEY:
                return False, 0
            action = self._id_to_action.get(int(msg.wParam))
            if action is None:
                return False, 0
            callback = self._callbacks.get(action)
            if callback is None:
                return True, 0
            # 不在原生消息里直接跑重活：弹窗/开会话/截图都要回事件循环。
            QTimer.singleShot(0, callback)
            return True, 0
        except Exception:
            logger.debug("全局快捷键：处理原生消息失败", exc_info=True)
            return False, 0


class _MSG(ctypes.Structure):
    """Win32 ``MSG``（只用到 message/wParam，其余字段按 ABI 对齐占位）。"""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]
