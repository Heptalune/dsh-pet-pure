# -*- coding: utf-8 -*-
"""4.2b 纯逻辑单测：活跃宠清单（D5）+ 无锁 slot 身份分配（D6）+ 每身份几何。

零 Qt：本文件只 import ``pet.overlay_spawn_state``（纯逻辑模块），不起
QApplication。覆盖：清单原子读写/顺序保持、缺失=首次运行、损坏或缺首字段
回退「只有主宠」、身份分配单调不撞且跳过既有配置与活跃身份、分配上限报错、
几何合并写回（保留身份其它键）、非法几何丢弃。
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from pet import overlay_spawn_state as ovs


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 活跃宠清单（D5）
def test_active_list_roundtrip_keeps_spawn_order(tmp_path):
    assert ovs.save_active_slots(tmp_path, [3, 1, 2]) is True
    assert ovs.load_active_slots(tmp_path) == [3, 1, 2]
    data = json.loads(ovs.active_pets_path(tmp_path).read_text(encoding="utf-8"))
    assert data == {"version": ovs.ACTIVE_PETS_VERSION, "slots": [3, 1, 2]}


def test_active_list_write_is_atomic(tmp_path):
    ovs.save_active_slots(tmp_path, [1])
    ovs.write_slot_geometry(tmp_path, 1, {"rx": 0.5, "ry": 0.5, "scale": 1.0})
    assert list(tmp_path.glob("*.tmp")) == []


def test_active_list_missing_file_is_first_run(tmp_path):
    assert ovs.load_active_slots(tmp_path) == [0]


@pytest.mark.parametrize("text", [
    "{ not json",
    "[]",
    '"slots"',
    '{"slots": [1, 2]}',                        # 缺首字段 version
    '{"version": 3, "slots": [1]}',             # 版本不匹配
    '{"version": 1, "slots": {"a": 1}}',        # slots 非列表
])
def test_active_list_corrupt_falls_back_to_main_only(tmp_path, text):
    _write(ovs.active_pets_path(tmp_path), text)
    assert ovs.load_active_slots(tmp_path) == []


def test_active_list_drops_illegal_entries(tmp_path):
    _write(ovs.active_pets_path(tmp_path),
           json.dumps({"version": 1, "slots": [1, 0, -2, "2", None, 1, "x"]}))
    assert ovs.load_active_slots(tmp_path) == [1, 0, 2]


def test_normalize_slots_dedupes_preserving_order():
    assert ovs.normalize_slots([2, 1, 2, "3", 0, -1, "x"]) == [2, 1, 3, 0]
    assert ovs.normalize_slots(None) == []


# ---------------------------------------------------------------- 无锁 slot 分配（D6）
def test_allocate_slot_restores_smallest_inactive_profile(tmp_path):
    assert ovs.allocate_slot(tmp_path) == 0
    ovs.write_slot_geometry(tmp_path, 1, {"rx": 0.1, "ry": 0.1})
    assert ovs.allocate_slot(tmp_path, active_slots=[0]) == 1
    ovs.write_slot_geometry(tmp_path, 2, {"rx": 0.2, "ry": 0.2})
    assert ovs.allocate_slot(tmp_path, active_slots=[0, 1]) == 2


def test_allocate_slot_skips_existing_configs_and_active_identities(tmp_path):
    _write(tmp_path / "config-slot-1.json", "{}")
    _write(tmp_path / "config-slot-2.json", "{}")
    # 活跃身份 3 尚未落配置（进程内已占）——同样不得撞
    assert ovs.allocate_slot(tmp_path, active_slots=[0, 3]) == 1
    assert ovs.allocate_slot(tmp_path, active_slots=[0, 1, 4]) == 2


def test_allocate_slot_without_config_dir_uses_active_only():
    assert ovs.allocate_slot(None, active_slots=[1, 2]) == 0


def test_scan_slot_configs_matches_frozen_naming(tmp_path):
    _write(tmp_path / "config-slot-7.json", "{}")
    _write(tmp_path / "config-slot-x.json", "{}")       # 非数字后缀不认
    _write(tmp_path / "config.json", "{}")
    _write(tmp_path / "overlay-active-pets.json", "{}")
    assert ovs.scan_slot_configs(tmp_path) == {7}
    assert ovs.scan_slot_configs(None) == set()


def test_allocate_slot_reuses_smallest_retired_when_history_is_full(tmp_path):
    """B01：历史身份占满 1..MAX_SCAN_SLOTS 时复用**最小退役身份**，不抛错。

    普通用户反复生/退累计 128 次后目录里全是历史 ``config-slot-N.json``；旧实现
    直接抛 ``SpawnStateError`` → 「生小肥鱼」永久失效（UI 也只有日志）。
    """
    for slot in range(1, ovs.MAX_SCAN_SLOTS + 1):
        _write(tmp_path / f"config-slot-{slot}.json", "{}")
    assert ovs.allocate_slot(tmp_path, active_slots=[0]) == 1
    assert ovs.allocate_slot(tmp_path, active_slots=[0, 1, 2]) == 3


def test_allocate_slot_preserves_existing_profiles(tmp_path):
    for slot in (0, 1, 2, 3):
        _write(tmp_path / f"config-slot-{slot}.json", '{"scale": 0.8}')
    before = {p: p.read_bytes() for p in tmp_path.glob('config-slot-*.json')}
    assert ovs.allocate_slot(tmp_path, max_slots=3) == 0
    assert all(p.read_bytes() == content for p, content in before.items())


def test_allocate_slot_raises_only_when_all_slots_active(tmp_path):
    """真耗尽 = max_slots 个身份**全在活跃集合**（实际不可能）才抛错。"""
    for slot in (1, 2, 3):
        _write(tmp_path / f"config-slot-{slot}.json", "{}")
    with pytest.raises(ovs.SpawnStateError):
        ovs.allocate_slot(tmp_path, active_slots=[0, 1, 2, 3], max_slots=3)


# ---------------------------------------------------------------- 每身份几何
def test_slot_geometry_roundtrip_preserves_other_keys(tmp_path):
    _write(tmp_path / "config-slot-1.json",
           json.dumps({"version": 4, "character": "nova", "user_customized": True}))
    assert ovs.write_slot_geometry(
        tmp_path, 1, {"rx": 0.25, "ry": 0.5, "facing": "right", "scale": 0.9}) is True
    assert ovs.read_slot_geometry(tmp_path, 1) == {
        "rx": 0.25, "ry": 0.5, "facing": "right", "scale": 0.9}
    raw = json.loads((tmp_path / "config-slot-1.json").read_text(encoding="utf-8"))
    assert raw["character"] == "nova"              # 身份其它键一个不丢
    assert raw["user_customized"] is True


def test_slot_geometry_missing_or_corrupt_is_empty(tmp_path):
    assert ovs.read_slot_geometry(tmp_path, 9) == {}
    _write(tmp_path / "config-slot-9.json", "{ broken")
    assert ovs.read_slot_geometry(tmp_path, 9) == {}
    assert ovs.slot_config_exists(tmp_path, 9) is True
    assert ovs.slot_config_exists(tmp_path, 10) is False


def test_slot_geometry_drops_illegal_values(tmp_path):
    _write(tmp_path / "config-slot-1.json",
           '{"version": 4, "rx": NaN, "ry": Infinity, "facing": "up", "scale": -1}')
    assert ovs.read_slot_geometry(tmp_path, 1) == {}


def test_slot_geometry_empty_write_is_rejected(tmp_path):
    assert ovs.write_slot_geometry(tmp_path, 1, {"rx": "bad", "facing": "up"}) is False
    assert ovs.slot_config_exists(tmp_path, 1) is False


# ---------------------------------------------------------------- B7b：通用 slot 读/写点
def test_read_slot_config_missing_is_none_corrupt_is_empty(tmp_path):
    """缺文件 → None（调用方继承主配置）；损坏/非对象 → {}（逐键回默认）。"""
    assert ovs.read_slot_config(tmp_path, 1) is None
    _write(tmp_path / "config-slot-1.json", "{ broken")
    assert ovs.read_slot_config(tmp_path, 1) == {}
    _write(tmp_path / "config-slot-1.json", "[1, 2]")
    assert ovs.read_slot_config(tmp_path, 1) == {}


def test_read_slot_config_returns_raw_dict(tmp_path):
    _write(tmp_path / "config-slot-2.json",
           json.dumps({"version": 4, "playback_speed": 0.5,
                       "self_talk_texts": ["台词"]}))
    data = ovs.read_slot_config(tmp_path, 2)
    assert data["playback_speed"] == 0.5
    assert data["self_talk_texts"] == ["台词"]


def test_write_slot_setting_preserves_other_keys_and_is_atomic(tmp_path):
    """单键写入保留该身份其它键；原子替换（无 .tmp 残留）；不碰主配置。"""
    _write(tmp_path / "config-slot-1.json",
           json.dumps({"version": 4, "character": "nova", "rx": 0.5}))
    assert ovs.write_slot_setting(tmp_path, 1, "playback_speed", 0.5) is True
    raw = json.loads((tmp_path / "config-slot-1.json").read_text(encoding="utf-8"))
    assert raw["playback_speed"] == 0.5
    assert raw["character"] == "nova" and raw["rx"] == 0.5
    assert list(tmp_path.glob("*.tmp")) == []
    assert not (tmp_path / "config.json").exists()


def test_write_slot_setting_missing_file_creates_it(tmp_path):
    """身份文件不存在也照写（旧版子宠进程的 Config 会自行落种）。"""
    assert ovs.write_slot_setting(tmp_path, 3, "drag_physics", True) is True
    raw = json.loads((tmp_path / "config-slot-3.json").read_text(encoding="utf-8"))
    assert raw["drag_physics"] is True
    assert raw["version"] == 4


def test_write_slot_setting_user_customized_flag(tmp_path):
    """``user_customized=True`` 置位（旧契约：用户主动设置过的 slot 不再被主设置刷新）。"""
    assert ovs.write_slot_setting(tmp_path, 1, "scale", 1.5,
                                  user_customized=True) is True
    raw = json.loads((tmp_path / "config-slot-1.json").read_text(encoding="utf-8"))
    assert raw["user_customized"] is True
    # 不传 = 保持原值（后台自存写盘不置位）
    assert ovs.write_slot_setting(tmp_path, 1, "scale", 1.6) is True
    raw = json.loads((tmp_path / "config-slot-1.json").read_text(encoding="utf-8"))
    assert raw["user_customized"] is True

    _write(tmp_path / "config-slot-2.json", json.dumps({"version": 4}))
    assert ovs.write_slot_setting(tmp_path, 2, "scale", 1.2) is True
    raw = json.loads((tmp_path / "config-slot-2.json").read_text(encoding="utf-8"))
    assert "user_customized" not in raw


def test_write_slot_setting_rejects_empty_key(tmp_path):
    assert ovs.write_slot_setting(tmp_path, 1, "", 1.0) is False
    assert ovs.write_slot_setting(tmp_path, 1, "   ", 1.0) is False
    assert ovs.slot_config_exists(tmp_path, 1) is False


# ------------------------------------------ 写盘瞬时共享冲突（Windows 实机 S1）
def test_write_slot_setting_retries_transient_share_conflict(tmp_path, monkeypatch):
    """实机：目录 watcher / 3s 轮询读 slot 配置时撞上 ``os.replace`` → WinError 5。

    ``MSVCRT _wopen`` 的共享模式不含 FILE_SHARE_DELETE，读方与写方撞同一文件即
    PermissionError；瞬时冲突必须退避重试，否则子宠设置静默丢失。
    """
    target = tmp_path / "config-slot-1.json"
    real = os.replace
    seen = {"n": 0}

    def flaky(src, dst, *args, **kwargs):
        if os.fspath(dst) == os.fspath(target) and seen["n"] < 1:
            seen["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    assert ovs.write_slot_setting(tmp_path, 1, "playback_speed", 0.5) is True
    assert seen["n"] == 1
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["playback_speed"] == 0.5
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_writers_clean_temp_on_persistent_conflict(tmp_path, monkeypatch):
    """冲突不消失：三个原子写都返回 False，且都不留 ``.tmp`` 残留。"""
    def boom(src, dst, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", boom)
    assert ovs.save_active_slots(tmp_path, [1]) is False
    assert ovs.write_slot_setting(tmp_path, 1, "scale", 1.5) is False
    assert ovs.write_slot_geometry(tmp_path, 1, {"rx": 0.5, "ry": 0.5}) is False
    assert list(tmp_path.glob("*.tmp")) == []
