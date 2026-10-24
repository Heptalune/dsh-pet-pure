# -*- coding: utf-8 -*-
"""mem_probe 计数器回归：``clip_listed_frames``（帧表**物化** Path 数）。

口径（M1 帧表去物化）：``FrameSeqClip._frames`` 是按编号现推路径的序列，帧数以
meta 的 ``frames`` 为权威 → 常态零物化，只有 meta 缺失/非法时才 glob 兜底并真正
持有列表。实机探针要把这两种状态分开（"爬稳态 vs 泄漏"），且对没有 ``_frames``
的 clip（WebMClip/GifClip）与非法值安全（None 安全）。

``_gc_census`` 按**类名**普查，因此这里的替身类名就叫 ``FrameSeqClip``（真 clip
的同口径普查路径）。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 探针模块在 import 期就会落 OUT_DIR（.scratch/mem-probe/adhoc）：测试不许写仓库
os.environ["PET_MEM_TRACE_DIR"] = str(
    Path(tempfile.gettempdir()) / "dsh-pet-mem-probe-test")

from PySide6.QtWidgets import QApplication  # noqa: E402

import tools.mem_probe as mem_probe  # noqa: E402

QApplication.instance() or QApplication([])

_KEY = "clip_listed_frames"


def _listed() -> int:
    return int(mem_probe._gc_census()[_KEY])


class FrameSeqClip:
    """同名替身：``_gc_census`` 按类名普查 FrameSeqClip/WebMClip。"""

    def __init__(self, frames, *, name: str = "_frames") -> None:
        self.name = name
        if name is not None:
            setattr(self, name, frames)


def test_listed_frames_counts_materialized_paths_only(tmp_path):
    """物化列表计其长度；meta 权威的现推算帧表计 0（哪怕逻辑帧数很大）。"""
    before = _listed()
    stub = FrameSeqClip([Path(f"f_{i:04d}.webp") for i in range(3)])
    assert _listed() - before == 3

    # 真 clip：meta 声明 240 帧但目录无帧文件（不触发兜底 glob，也不物化 Path）
    meta_dir = tmp_path / "clip"
    meta_dir.mkdir()
    (meta_dir / "meta.json").write_text(
        json.dumps({"fps": 24.0, "frames": 240}), encoding="utf-8")
    from pet.frameseq_clip import FrameSeqClip as RealClip

    clip = RealClip(meta_dir)
    try:
        assert clip.frameCount() == 240
        assert _listed() - before == 3, "现推帧表不得计入物化 Path"
        assert stub is not None
    finally:
        clip.close()


def test_listed_frames_is_none_and_type_safe(tmp_path):
    """缺 ``_frames`` / ``_frames=None`` / 非法值一律不炸也不多计。"""
    before = _listed()
    FrameSeqClip(None)
    FrameSeqClip(5)
    FrameSeqClip("not-a-container")
    kept = FrameSeqClip([], name=None)          # 完全不带 _frames 属性
    assert kept.name is None
    assert _listed() - before == 0
