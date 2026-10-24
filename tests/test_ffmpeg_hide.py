"""ffmpeg 子进程隐窗补丁：Windows 上 imageio_ffmpeg 的 Popen 参数必须带 CREATE_NO_WINDOW。"""
import subprocess
import sys

import pytest

imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")

from pet import ffmpeg_hide  # noqa: E402


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows only")
def test_popen_kwargs_carry_create_no_window_in_both_modules():
    from imageio_ffmpeg import _io, _utils

    assert ffmpeg_hide.install() is True
    assert ffmpeg_hide.install() is True  # 幂等
    for mod in (_utils, _io):
        kw = mod._popen_kwargs()
        assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
        # 原有 SW_HIDE startupinfo 不被丢弃
        assert "startupinfo" in kw
    # 不重复包装
    assert getattr(_utils._popen_kwargs, "_dsh_no_window", False)
    assert _io._popen_kwargs is _utils._popen_kwargs


def test_importing_media_modules_installs_hide_patch():
    import pet.webm_clip  # noqa: F401

    if sys.platform.startswith("win"):
        assert ffmpeg_hide._INSTALLED is True


def test_install_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(ffmpeg_hide, "_INSTALLED", False)
    monkeypatch.setattr(ffmpeg_hide.sys, "platform", "linux")
    assert ffmpeg_hide.install() is False
