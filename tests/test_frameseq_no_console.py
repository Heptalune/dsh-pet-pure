"""帧序列供给派生 ffmpeg 不得弹控制台窗口。

打包产物是 --windowed（GUI 子系统，无控制台）；Windows 上派生 ffmpeg.exe 这类控制台
程序时若不带 CREATE_NO_WINDOW，系统会为它新建一个可见的控制台窗口——首跑供给在启动后
约 2 分钟内逐个转换 clip，表现为桌宠启动后反复闪出命令行窗口（实机 2026-10-12）。
"""
from __future__ import annotations

import os
import subprocess

import pytest

from pet import frameseq_provision as fp


@pytest.mark.skipif(os.name != "nt", reason="creationflags 仅 Windows 生效")
def test_ffmpeg_creation_flags_hide_console_and_keep_low_priority():
    flags = fp._creation_flags()
    assert flags & subprocess.CREATE_NO_WINDOW, "缺 CREATE_NO_WINDOW 会在 GUI 进程里弹控制台窗口"
    assert flags & subprocess.BELOW_NORMAL_PRIORITY_CLASS


@pytest.mark.skipif(os.name == "nt", reason="POSIX 分支")
def test_ffmpeg_creation_flags_posix_is_zero():
    assert fp._creation_flags() == 0
