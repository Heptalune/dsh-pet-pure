# -*- coding: utf-8 -*-
"""豆包 provider 的两条硬约束（2026-10-13 机主报「调音调就弹窗」的回归）：

① 变调调 ffmpeg 时**必须隐藏控制台窗口**（CREATE_NO_WINDOW）——机主对"又弹一个窗口"是红线；
② 音调为 0 时**不能起子进程**（直接写原音）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pet.tts import doubao


def test_pitch_shift_hides_console_window(monkeypatch, tmp_path) -> None:
    """变调必须带 CREATE_NO_WINDOW——这是机主明说的红线，别退回去。"""
    captured: list[dict] = []

    def fake_run(args, **kwargs):
        captured.append(kwargs)
        Path(args[-1]).write_bytes(b"ID3shifted")

    monkeypatch.setattr(doubao.subprocess, "run", fake_run)
    monkeypatch.setattr(doubao, "_ffmpeg", lambda: "ffmpeg")
    out = tmp_path / "out.mp3"
    doubao._write_with_pitch(b"ID3raw", 15, out)

    assert captured, "变调没有起子进程"
    kwargs = captured[0]
    if sys.platform == "win32":
        assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW, (
            "变调子进程会弹控制台窗口：必须带 CREATE_NO_WINDOW"
        )
    assert out.read_bytes() == b"ID3shifted"


def test_zero_pitch_never_spawns_a_process(monkeypatch, tmp_path) -> None:
    """音调 0 时直接写原音，不应有任何子进程（避免无意义的窗口/开销）。"""
    called = []
    monkeypatch.setattr(doubao.subprocess, "run", lambda *a, **k: called.append(a))
    out = tmp_path / "raw.mp3"
    doubao._write_with_pitch(b"ID3raw", 0, out)
    # _write_with_pitch 只在 pitch>0 时被调用；直接调它传 0 也应走"无变调"分支
    assert out.read_bytes() == b"ID3raw"
    assert not called


def test_provider_contract_basics() -> None:
    provider = doubao.PROVIDER
    assert provider.id == "doubao"
    assert provider.availability({"api_key": ""}) == doubao.AVAILABILITY_CODE
    assert provider.availability({"api_key": "x"}) == ""
    values = {"voice": "zh_female_peiqi_uranus_bigtts", "pitch": 15, "api_key": "x"}
    assert provider.flavor(values) == "doubao|zh_female_peiqi_uranus_bigtts|15"
    plan = provider.plan(values)
    assert plan["ext"] == "mp3" and plan["pitch"] == 15
    assert "x" not in provider.flavor(values), "凭据绝不许进缓存键（flavor）"


@pytest.mark.parametrize("pitch", [0, 30, 99, -5])
def test_plan_clamps_pitch(pitch: int) -> None:
    plan = doubao.PROVIDER.plan({"voice": "v", "pitch": pitch, "api_key": "x"})
    assert 0 <= plan["pitch"] <= 30
