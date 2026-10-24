"""让 imageio_ffmpeg 拉起的 ffmpeg 子进程在 Windows 上不弹终端窗口。

imageio_ffmpeg 0.6 只用 ``STARTUPINFO + STARTF_USESHOWWINDOW``（wShowWindow=SW_HIDE）
隐藏控制台；当用户的默认终端是 Windows Terminal 时，打包后的无控制台主进程拉起
控制台子进程会新建一个由 Windows Terminal 承载的窗口，SW_HIDE 被忽略——开机自启
集中做 ffmpeg 版本探测 / fps 探测 / 解码时会闪出标题为 ``...\\_internal\\imageio_ffmpeg\\...``
的空终端。补上 ``CREATE_NO_WINDOW`` 才能根治（与 frameseq_provision 直接 Popen 的
转换路径一致）。

``_io`` 用 ``from ._utils import _popen_kwargs`` 取了一份本地引用，所以 ``_utils`` 与
``_io`` 两处都要替换。幂等；非 Windows / 库缺失时为无操作。
"""
from __future__ import annotations

import subprocess
import sys

_INSTALLED = False


def install() -> bool:
    """安装补丁，返回是否已生效（非 Windows 或库不可用返回 False）。"""
    global _INSTALLED
    if _INSTALLED:
        return True
    if not sys.platform.startswith("win"):
        return False
    flag = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not flag:
        return False
    try:
        from imageio_ffmpeg import _io, _utils
    except Exception:
        return False
    original = getattr(_utils, "_popen_kwargs", None)
    if original is None:
        return False
    if getattr(original, "_dsh_no_window", False):
        _INSTALLED = True
        return True

    def _popen_kwargs(*args, **kwargs):
        result = dict(original(*args, **kwargs))
        result["creationflags"] = int(result.get("creationflags") or 0) | flag
        return result

    _popen_kwargs._dsh_no_window = True  # type: ignore[attr-defined]
    _utils._popen_kwargs = _popen_kwargs
    if getattr(_io, "_popen_kwargs", None) is original:
        _io._popen_kwargs = _popen_kwargs
    _INSTALLED = True
    return True
