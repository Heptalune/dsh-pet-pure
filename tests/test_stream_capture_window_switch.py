# -*- coding: utf-8 -*-
"""窗口能力的捕获开关归并：主配置压过 slot 配置（T4 Phase A 审查修复 3）。

背景：拓扑只看**主配置**（``overlay_settings_command``），而 ``PetWindow`` 的窗
旗标读的是**自己那份** ``Config``。``--slot N`` 启动出来的窗读
``config-slot-N.json``，于是「主配置开捕获 + slot 没有」会落在 legacy 单窗形态里
却仍是 Tool 窗口——OBS 依旧枚举不到，正是捕获模式要解决的症状。归并只影响窗口
能力的有效值，不改写 slot 配置。

纪律：offscreen；同步直调，不 sleep。
"""
from __future__ import annotations

import json
import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pet.config import Config
from pet.window import STREAM_CAPTURE_TITLE, PetWindow
from tests.test_window_pause import FakeLibrary

app = QApplication.instance() or QApplication([])


def _window_type_mask(flags):
    return flags & Qt.WindowType.WindowType_Mask


def _close(win):
    win.close()
    win.deleteLater()
    app.processEvents()


def _write_slot_file(tmp_path, slot_id: int, payload: dict):
    """直接落一份 ``config-slot-N.json``（模拟升级前遗留的槽档案）。

    ``stream_capture_mode`` 现在是**全局**键（规范 §五）：槽档案里不再写它，
    裸 slot ``Config.save`` 会被归属表拒绝。这里造的是旧版留下的真实文件——
    "主配置 true + 槽档案里还残着 false"正是窗口能力归并要处理的输入。
    """
    from pet.config import APP_DIR_NAME

    cfg_dir = tmp_path / APP_DIR_NAME
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / f"config-slot-{slot_id}.json"
    path.write_text(json.dumps({"version": 4, **payload}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _make_slot_before_main_switch(tmp_path):
    """先落出槽档案（此时主配置还没开捕获），再让主配置开捕获。

    槽那份窗口视图读它自己的 ``config-slot-N.json``（遗留值 false），主配置随后
    置 true——正是"归并只看主配置、且不改写槽档案"要覆盖的组合。
    """
    _write_slot_file(tmp_path, 1, {"stream_capture_mode": False})
    slot = Config(base=tmp_path, instance_id="slot-1")
    assert slot.get("stream_capture_mode") is False
    main = Config(base=tmp_path)
    main.set("stream_capture_mode", True)
    assert main.save()
    return slot, main


def test_slot_window_follows_main_capture_switch(tmp_path):
    """主配置捕获=true + slot 配置 false：窗口照样带捕获旗标与标题（OBS 可枚举）。"""
    slot, _main = _make_slot_before_main_switch(tmp_path)
    win = PetWindow(FakeLibrary(), slot)
    try:
        assert win._stream_capture_mode is True
        assert _window_type_mask(win.windowFlags()) == Qt.WindowType.Window
        assert win.windowTitle() == STREAM_CAPTURE_TITLE
        assert win._speech_bubble.parentWidget() is win
        # 归并只作用在窗口能力的有效值上：slot 配置文件不得被改写
        on_disk = json.loads(slot.path.read_text(encoding="utf-8"))
        assert on_disk["stream_capture_mode"] is False
    finally:
        _close(win)


def test_slot_window_keeps_own_switch_when_main_capture_off(tmp_path, monkeypatch):
    """对照：主配置没开捕获（env 逃生门）时，slot 自己那份配置仍然算数、可运行期切。"""
    monkeypatch.setenv("PET_RENDER_TOPOLOGY", "legacy")
    _write_slot_file(tmp_path, 1, {"stream_capture_mode": True})
    slot = Config(base=tmp_path, instance_id="slot-1")
    assert slot.get("stream_capture_mode") is True

    win = PetWindow(FakeLibrary(), slot)
    try:
        assert win._stream_capture_mode is True
        assert win.windowTitle() == STREAM_CAPTURE_TITLE

        # dev 逃生门路径保留原有的运行期即时切换（不是重启级）
        slot.set("stream_capture_mode", False)
        win.refresh_pet_settings()
        assert win._stream_capture_mode is False
        assert win.windowTitle() == ""
        assert _window_type_mask(win.windowFlags()) == Qt.WindowType.Tool
    finally:
        _close(win)


def test_runtime_switch_refused_when_topology_came_from_config(tmp_path, caplog):
    """审查修复 4：本次启动的 legacy 由主配置捕获开关造成 ⇒ 运行期不撤窗旗标。

    用户在设置页取消勾选时，运行中的窗口不得当场变回 Tool（那会切断正在进行的
    直播捕获，且与「重启后生效」的文案矛盾）。
    """
    cfg = Config(base=tmp_path)
    cfg.set("stream_capture_mode", True)
    assert cfg.save()

    win = PetWindow(FakeLibrary(), cfg)
    try:
        assert win._stream_capture_mode is True
        assert win.windowTitle() == STREAM_CAPTURE_TITLE

        cfg.set("stream_capture_mode", False)   # 设置页取消勾选
        with caplog.at_level(logging.INFO):
            win.refresh_pet_settings()

        assert win._stream_capture_mode is True, "重启级开关不得运行期回撤"
        assert win.windowTitle() == STREAM_CAPTURE_TITLE
        assert _window_type_mask(win.windowFlags()) == Qt.WindowType.Window
        assert any("重启后生效" in record.getMessage() for record in caplog.records)
    finally:
        _close(win)
