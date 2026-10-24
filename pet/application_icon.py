# -*- coding: utf-8 -*-
"""正式桌宠图标（窗口/标题栏用的应用图标）。

``--icon assets/icon.ico`` 只把图标写进可执行文件/应用包的**文件图标**资源；Qt 窗口
标题栏上那一个走 ``setWindowIcon``，产品原先一处都没设过，于是原生标题栏摆着系统给的
通用图标。本模块是它的唯一入口。

两条口径：

1. **双候选**：冻结包取 ``sys._MEIPASS/assets/icon.ico``，源码运行取仓库根下的同一
   相对路径（与 ``pet/game_fps.py::bundled_backend_path`` 同一约定，按 ``__file__``
   解析，因此与当前工作目录无关）。冻结包里那份**必须真的进包**（``--add-data``，
   三端构建脚本已加），否则窗口图标在冻结版里静默消失——打包契约由
   ``tests/test_application_icon.py`` 钉住。
2. **拿不到就给空图标**：候选缺失、或存在但读不出来（截断/非法字节），一律返回空
   ``QIcon``、不抛异常，调用方照旧 ``setWindowIcon``（等于没有图标，不会有更坏的后果）。
   第一个候选坏掉时继续试下一个。

**必须在 Qt GUI 应用建好之后、GUI 线程里调用**：应用还没起来时构造一个从文件加载的
``QIcon`` 会让进程直接死掉（本机实测：无任何 Qt 应用时 ``QIcon("assets/icon.ico")``
让进程以退出码 127 结束，而空的 ``QIcon()`` 安然无恙）。所以加载前先看
``QGuiApplication.instance()``，没有就返回空图标并**不写缓存**；普通 ``QApplication``
同样算数（它是 ``QGuiApplication`` 的子类，``instance()`` 返回同一个对象）。缓存跟着
应用实例走：同一应用内第二次调用返回同一个对象（419 KB 的 ICO 每开一个窗口解析一次
是白付），应用被销毁重建后自动失效。线程那一半不在代码里断言——``QIcon`` 会惰性生成
``QPixmap``，按 Qt 规矩调用点就应该是 GUI 线程。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication, QIcon

__all__ = [
    "APPLICATION_ICON_RELATIVE_PATH",
    "application_icon_candidates",
    "load_application_icon",
]

#: 图标在源码树 / 冻结包里的相对路径（冻结包按 ``sys._MEIPASS`` 为根，与
#: ``pet/game_fps.py`` 的 ``BACKEND_RELATIVE_DIR`` 同一约定）。三端构建脚本里的
#: ``--add-data`` 目标必须与它一致。
APPLICATION_ICON_RELATIVE_PATH = ("assets", "icon.ico")

#: 缓存内容：``(所属应用实例, QIcon)``。用应用实例做键：QIcon 不能早于应用构造，
#: 也不该跨应用实例复用（测试会重建 QApplication）。
_ICON_CACHE: tuple[object, QIcon] | None = None


def application_icon_candidates() -> tuple[Path, ...]:
    """图标候选路径，冻结包在前、源码树在后（**纯函数**，不碰文件系统）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass).joinpath(*APPLICATION_ICON_RELATIVE_PATH))
    candidates.append(Path(__file__).resolve().parents[1].joinpath(*APPLICATION_ICON_RELATIVE_PATH))
    return tuple(candidates)


def load_application_icon() -> QIcon:
    """正式桌宠图标的 ``QIcon``；拿不到时返回**空图标**。"""
    global _ICON_CACHE
    application = QGuiApplication.instance()
    if application is None:
        return QIcon()

    cached = _ICON_CACHE
    if cached is not None and cached[0] is application:
        return cached[1]

    for candidate in application_icon_candidates():
        if not candidate.is_file():
            continue
        icon = QIcon(str(candidate))
        if not icon.isNull():
            _ICON_CACHE = (application, icon)
            return icon
    return QIcon()
