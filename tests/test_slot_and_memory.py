# -*- coding: utf-8 -*-
"""槽位机制与个体记忆的全面测试。

覆盖 plan5 §7 的规范测试场景：
1. 真实子进程竞争同一临时配置根目录，依次获得 slot-0/1/2；指定槽竞争失败不降级，锁文件残留可复用。
2. 子进程持有 slot-1 后 exit 或被终止，新子进程重新加锁 slot-1，读取个体配置与 sessions，不删 lock 文件。
3. 真实子进程并发首次创建 slot 配置，最终 JSON 完整，PID 后缀 .tmp 不撞名。
4. 主配置变更后，新 slot 首次创建继承主配置（位置独立、自启仅主槽有效）；已有 slot 保持个体修改记忆。
5. 损坏配置唯一备份名，连续恢复不覆盖旧备份。
6. 旧 config.json 无槽位元数据仍作为 slot-0；旧 spawn 原子迁移到 slot-1/2，中断回滚与标记。
7. slot-0 被占用时自启失败不拿 slot-1。
8. 手动启动与菜单 spawn 顺序与 offset index 独立测试。
9. 位置避让与 spawn offset 独立生效测试。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pet.config import Config, APP_DIR_NAME
from pet import slot_manager as sm
from pet import catalog
from pet.chat.session_store import SessionStore
from pet.chat.models import ChatMessage, ChatSession
from pet.settings_slot_config import route_settings_config


def test_field_default_factory_and_individual_memory(tmp_path):
    """场景 4（G1 归属口径）：每宠键按**这只宠自己的档案**取值，全局键全宠共用一份。

    旧版"每个 slot 一份完整 config、首次创建从主配置整表落种"随规范 §五 作废：
    ``config-slot-N.json`` 只存每宠键；全局键（``on_top`` / 开机自启…）仍只有主配置
    一份真值，任何一只宠都从它读。落种（生成子宠时从 slot0 档案取种）在
    ``tests/test_overlay_spawn.py`` 与 ``tests/test_single_process_spawn.py`` 覆盖。
    """
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    master = Config(base=tmp_path)
    master.set("on_top", False)
    master.set("show_dock_icon", False)
    master.set("chat_follow_pet", True)
    master.set("autostart_wanted", True)
    assert master.save() is True

    # 主宠档案（slot0）：每宠键写进它自己的档案
    slot0 = route_settings_config(Config(base=tmp_path, instance_id="slot-0"))
    slot0.set("character", "shenshen")
    slot0.set("playback_speed", 1.5)
    slot0.set("click_sound_volume", 0.33)
    assert slot0.save() is True

    # slot-1 还没落种：每宠键取**同源默认**（不继承主宠当前值——否则"关掉的 slot0
    # 会继承主宠的新设置"），全局键照旧读主配置那一份
    slot1 = route_settings_config(Config(base=tmp_path, instance_id="slot-1"))
    assert slot1.get("playback_speed") == 1.0
    assert slot1.get("click_sound_volume") == 0.7
    assert slot1.get("on_top") is False
    assert slot1.get("show_dock_icon") is False
    assert slot1.get("chat_follow_pet") is True
    assert slot1.get("autostart_wanted") is True
    assert slot1.get("rx") is None
    assert slot1.get("ry") is None

    # slot-1 写自己的档案（个体记忆）
    slot1.set("character", "dundun")
    slot1.set("click_sound_volume", 0.55)
    assert slot1.save() is True

    # 之后改主宠档案：已有 slot-1 的每宠键不受影响
    slot0b = route_settings_config(Config(base=tmp_path, instance_id="slot-0"))
    slot0b.set("character", "master_new")
    slot0b.set("click_sound_volume", 0.99)
    assert slot0b.save() is True

    slot1_reload = route_settings_config(Config(base=tmp_path, instance_id="slot-1"))
    assert slot1_reload.get("character") == "dundun"
    assert slot1_reload.get("click_sound_volume") == 0.55

    # 主宠自己的档案确实跟着改了（不是"谁都没动"）
    assert route_settings_config(
        Config(base=tmp_path, instance_id="slot-0")).get("character") == "master_new"

    # 新建 slot-2 仍是同源默认起步（每宠键不跨身份继承），全局键继续共用
    slot2 = route_settings_config(Config(base=tmp_path, instance_id="slot-2"))
    assert slot2.get("character") == catalog.DEFAULT_CHARACTER
    assert slot2.get("playback_speed") == 1.0
    assert slot2.get("on_top") is False


def test_spawn_reuse_keeps_existing_slot_config(tmp_path, monkeypatch):
    """「生小肥鱼」复用已有存档的 slot 时不再顶掉原槽设置（回归：旧版
    DSH_PET_SPAWN_FRESH 强制重播种会把 slot-N 个体配置覆盖成主配置）。

    G1 口径：重用槽的每宠键真值在它自己的档案里，构造侧或环境都没法再"重播种"。
    """
    old_slot = route_settings_config(Config(base=tmp_path, instance_id="slot-1"))
    old_slot.set("character", "dundun")
    old_slot.set("playback_speed", 0.5)
    old_slot.set("click_sound_volume", 0.2)
    assert old_slot.save() is True

    # 旧版孵化进程环境可能仍带着 SPAWN_FRESH 标记，须被完全忽略
    monkeypatch.setenv("DSH_PET_SPAWN_FRESH", "1")
    reused_slot = route_settings_config(Config(base=tmp_path, instance_id="slot-1"))
    assert reused_slot.get("character") == "dundun"
    assert reused_slot.get("playback_speed") == 0.5
    assert reused_slot.get("click_sound_volume") == 0.2


def test_normal_reopen_keeps_existing_slot_config(tmp_path, monkeypatch):
    """普通重启/复用 slot 时，已有个体配置不被主宠的当前值覆盖。"""
    master = route_settings_config(Config(base=tmp_path, instance_id="slot-0"))
    master.set("character", "shenshen")
    assert master.save() is True

    slot = route_settings_config(Config(base=tmp_path, instance_id="slot-1"))
    slot.set("character", "dundun")
    assert slot.save() is True

    reopened = route_settings_config(Config(base=tmp_path, instance_id="slot-1"))
    assert reopened.get("character") == "dundun"


def test_spawn_seed_respects_inherit_size_switch(tmp_path):
    """生小肥鱼落种遵循 ``spawn_inherit_size`` / ``spawn_scale``（G1 口径）。

    落种源是 **slot0 的档案**（不是"主配置整表"）：开启继承 → 新槽拿到的 scale 跟
    slot0 一样；关闭继承 → 用主配置里给"小肥鱼"单独选的 ``spawn_scale``。
    """
    import tests.test_overlay_spawn as osp
    from pet import pet_profiles

    def _spawned_scale(base, values):
        shell, _made = osp._make_persistent_shell(base, values)
        try:
            shell.spawn_pet()
            child = shell._spawned[0]
            slot = shell._spawned_slots[child]
            profile = pet_profiles.read_slot_profile(shell._spawn_config_dir(), slot)
            return child.scale, float(profile.data["scale"])
        finally:
            shell._delete_runtime_marker()

    # 开启继承：slot0 档案 1.0，新鱼也应为 1.0（档案与运行态一致）
    child_scale, profile_scale = _spawned_scale(tmp_path / "inherit", {
        "scale": 1.0, "spawn_inherit_size": True, "spawn_scale": 0.5})
    assert profile_scale == 1.0
    assert child_scale == 1.0

    # 关闭继承：slot0 仍是 1.0，但新鱼按 spawn_scale=0.5 落种
    child_scale, profile_scale = _spawned_scale(tmp_path / "custom", {
        "scale": 1.0, "spawn_inherit_size": False, "spawn_scale": 0.5})
    assert profile_scale == 0.5
    assert child_scale == 0.5


def test_corrupt_config_backup_unique_timestamp(tmp_path):
    """场景 6：损坏配置唯一备份名，连续恢复不覆盖旧备份。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    corrupt_cfg = config_dir / "config-slot-1.json"
    corrupt_cfg.write_text("{invalid-json", encoding="utf-8")

    b1 = sm.backup_corrupt_config(corrupt_cfg)
    assert b1 is not None and b1.exists()
    assert "corrupt-" in b1.name

    time.sleep(0.01)
    b2 = sm.backup_corrupt_config(corrupt_cfg)
    assert b2 is not None and b2.exists()
    assert b1 != b2


def test_migrate_legacy_spawns_atomic_and_rollback(tmp_path):
    """场景 7：旧 spawn 原子迁移到 slot-1/2，旧 config.json 仍为 slot-0，孤儿文件保留。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    # 创建旧 config.json（无槽位元数据）
    (config_dir / "config.json").write_text(json.dumps({"version": 4, "character": "master"}), encoding="utf-8")

    # 创建两个旧 spawn 配置文件和会话
    (config_dir / "config-spawn100x1.json").write_text(json.dumps({"character": "spawn1"}), encoding="utf-8")
    s1_dir = config_dir / "sessions-spawn100x1" / "shenshen"
    s1_dir.mkdir(parents=True, exist_ok=True)
    (s1_dir / "s1.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")

    # 人工给第二个 spawn 一个稍晚的 mtime
    spawn2_file = config_dir / "config-spawn200x1.json"
    spawn2_file.write_text(json.dumps({"character": "spawn2"}), encoding="utf-8")
    os.utime(spawn2_file, (time.time() + 10, time.time() + 10))

    # 执行迁移
    assert sm.migrate_legacy_spawns(config_dir) is True

    # 验证映射到 slot-1 和 slot-2
    cfg1 = json.loads((config_dir / "config-slot-1.json").read_text(encoding="utf-8"))
    assert cfg1.get("character") == "spawn1"
    assert (config_dir / "sessions-slot-1" / "shenshen" / "s1.json").exists()

    cfg2 = json.loads((config_dir / "config-slot-2.json").read_text(encoding="utf-8"))
    assert cfg2.get("character") == "spawn2"

    # 主配置不受影响
    master_cfg = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    assert master_cfg.get("character") == "master"

    # 迁移标记文件写入
    assert (config_dir / "migration-spawns.done").exists()


def test_corrupt_config_backup_and_wiring_in_config_load(tmp_path):
    """测试 Config._load 对损坏的 slot 配置及主配置调用 backup_corrupt_config 备份而不静默覆盖。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    # 1. 槽位配置文件损坏
    slot1_cfg = config_dir / "config-slot-1.json"
    slot1_cfg.write_text("{bad-json-slot1", encoding="utf-8")

    cfg1 = Config(base=tmp_path, instance_id="slot-1")
    # 检查是否生成了备份文件
    backups1 = list(config_dir.glob("config-slot-1.json.corrupt-*"))
    assert len(backups1) == 1
    assert backups1[0].read_text(encoding="utf-8") == "{bad-json-slot1"
    # cfg1 正常回退到默认配置
    assert cfg1.get("character") is not None

    # 2. 主配置文件损坏
    master_cfg = config_dir / "config.json"
    master_cfg.write_text("{bad-json-master", encoding="utf-8")

    cfg_master = Config(base=tmp_path)
    backups_master = list(config_dir.glob("config.json.corrupt-*"))
    assert len(backups_master) == 1
    assert backups_master[0].read_text(encoding="utf-8") == "{bad-json-master"
    assert cfg_master.get("character") is not None


def test_migrate_legacy_spawns_rollback_on_sessions_move_failure(tmp_path, monkeypatch):
    """测试旧 spawn 迁移时，若 config 移动成功但 sessions 移动失败，完整回滚三方状态。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    old_cfg = config_dir / "config-spawn999x1.json"
    old_cfg.write_text(json.dumps({"character": "spawn_fail"}), encoding="utf-8")
    old_sessions = config_dir / "sessions-spawn999x1"
    old_sessions.mkdir(parents=True, exist_ok=True)
    (old_sessions / "session.json").write_text("{}", encoding="utf-8")

    # 模拟在 move staged_sessions -> target_sessions 时抛异常
    orig_move = shutil.move

    def mock_move(src, dst, **kwargs):
        if "sessions-slot-1" in str(dst):
            raise OSError("Injected sessions move failure")
        return orig_move(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "move", mock_move)

    result = sm.migrate_legacy_spawns(config_dir)
    assert result is False

    # 验证完整回滚：原文件存在，目标文件不存在
    assert old_cfg.exists()
    assert old_sessions.exists()
    assert (old_sessions / "session.json").exists()
    assert not (config_dir / "config-slot-1.json").exists()
    assert not (config_dir / "sessions-slot-1").exists()
    assert not (config_dir / ".migration_staging").exists()


def test_migrate_legacy_spawns_recovers_staged_remnants(tmp_path):
    """测试启动迁移时若存在非空 .migration_staging 残留，能够恢复完成或清理。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = config_dir / ".migration_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "config-slot-1.json").write_text(json.dumps({"character": "staged_pet"}), encoding="utf-8")

    assert sm.migrate_legacy_spawns(config_dir) is True
    # 验证已从 staging 恢复到目标位置
    assert (config_dir / "config-slot-1.json").exists()
    assert json.loads((config_dir / "config-slot-1.json").read_text(encoding="utf-8"))["character"] == "staged_pet"
    assert not staging_dir.exists()


def test_migration_staging_remnant_is_kept_when_target_already_exists(tmp_path):
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "config-slot-1.json"
    target.write_text(json.dumps({"character": "current"}), encoding="utf-8")
    staging_dir = config_dir / ".migration_staging"
    staging_dir.mkdir()
    staged = staging_dir / "config-slot-1.json"
    staged.write_text(json.dumps({"character": "staged"}), encoding="utf-8")

    assert sm.migrate_legacy_spawns(config_dir) is False
    assert staged.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["character"] == "current"


def test_list_runtime_marker_files_returns_legacy_and_v2(tmp_path):
    """批 B：list_runtime_marker_files 同时认旧名与版本化新名 runtime 标记。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    (config_dir / "runtime-111.json").write_text("{}", encoding="utf-8")
    (config_dir / "pet-runtime-v2-222-slot-1.json").write_text("{}", encoding="utf-8")
    (config_dir / "pet-runtime-v2-333-slot-2.json").write_text("{}", encoding="utf-8")
    (config_dir / "config-slot-1.json").write_text("{}", encoding="utf-8")
    (config_dir / "sessions-slot-1").mkdir()

    names = {p.name for p in sm.list_runtime_marker_files(config_dir)}
    # 新旧两种命名都被列出
    assert {"runtime-111.json",
            "pet-runtime-v2-222-slot-1.json",
            "pet-runtime-v2-333-slot-2.json"} <= names
    # 非 runtime 标记文件不得被误列
    assert "config-slot-1.json" not in names
    assert "sessions-slot-1" not in names


def test_migrate_legacy_spawns_skips_when_v2_marker_alive(tmp_path):
    """批 B：migrate_legacy_spawns 检测 v2 版本化 runtime 标记（多进程模式只写
    新名），发现存活实例时跳过迁移。旧 glob 只认 runtime-*.json 会误放行。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    old_cfg = config_dir / "config-spawn100x1.json"
    old_cfg.write_text(json.dumps({"character": "spawn1"}), encoding="utf-8")
    (config_dir / "sessions-spawn100x1").mkdir(parents=True, exist_ok=True)

    # v2 标记用当前（必然存活）进程 pid
    v2 = config_dir / f"pet-runtime-v2-{os.getpid()}-slot-1.json"
    v2.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    assert sm.migrate_legacy_spawns(config_dir) is False
    # 旧配置未被迁移（检测到存活实例跳过）
    assert old_cfg.exists()
    assert not (config_dir / "config-slot-1.json").exists()
    assert not (config_dir / "migration-spawns.done").exists()


# --------------------------------------------------------------------------
# 缺陷 18：runtime 标记 / slot 落种的写侧非原子 + 读侧把解析失败一律当陈旧删除
# --------------------------------------------------------------------------
def test_runtime_marker_write_goes_through_atomic_replace(tmp_path, monkeypatch):
    """缺陷 18（写侧）：标记必须 temp + 原子替换，成功路径不留 .tmp。

    直写最终路径会被独立设置进程的 ``read_live_instances`` 读到半截 JSON。
    """
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    seen: list[tuple[Path, Path]] = []
    real = sm.atomic_replace_with_retry

    def spy(temp, target, attempts=5):
        seen.append((Path(temp), Path(target)))
        real(temp, target, attempts)

    monkeypatch.setattr(sm, "atomic_replace_with_retry", spy)
    path = sm.write_runtime_marker(config_dir, "", 10, 20, 30, 40, versioned=True)

    assert seen, "标记写入必须经原子替换（不得直写最终路径）"
    temp, target = seen[0]
    assert target == path
    assert temp != path and temp.parent == config_dir
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not temp.exists(), "成功路径不得残留 .tmp"
    assert list(config_dir.glob("*.tmp")) == []


def test_runtime_marker_write_failure_keeps_old_file_and_no_tmp(tmp_path, monkeypatch):
    """缺陷 18（写侧）：替换失败不得留下半写内容，且 .tmp 必须清掉。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    path = sm.runtime_marker_path(config_dir, "", versioned=True)
    path.write_text(json.dumps({"pid": os.getpid(), "x": 1, "y": 1, "w": 5, "h": 5}),
                    encoding="utf-8")

    def deny(temp, target, attempts=5):
        raise OSError("replace denied")

    monkeypatch.setattr(sm, "atomic_replace_with_retry", deny)
    sm.write_runtime_marker(config_dir, "", 99, 99, 99, 99, versioned=True)

    assert json.loads(path.read_text(encoding="utf-8"))["x"] == 1, \
        "替换失败时旧内容必须完整保留（不得半写/覆盖）"
    assert list(config_dir.glob("*.tmp")) == [], "失败路径同样不得残留 .tmp"


def test_seed_slot_config_write_goes_through_atomic_replace(tmp_path, monkeypatch):
    """缺陷 18（写侧）：slot 落种写盘同样走 temp + 原子替换。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"character": "shenshen"}),
                                            encoding="utf-8")
    seen: list[tuple[Path, Path]] = []
    real = sm.atomic_replace_with_retry

    def spy(temp, target, attempts=5):
        seen.append((Path(temp), Path(target)))
        real(temp, target, attempts)

    monkeypatch.setattr(sm, "atomic_replace_with_retry", spy)

    assert sm.seed_slot_config_from_main(config_dir, 1) is True
    target = sm.get_config_path_for_slot(config_dir, 1)
    assert seen and seen[0][1] == target, "落种必须经原子替换"
    assert seen[0][0] != target and seen[0][0].parent == config_dir
    assert json.loads(target.read_text(encoding="utf-8"))["character"] == "shenshen"
    assert list(config_dir.glob("*.tmp")) == []


def test_seed_slot_config_write_failure_reports_false(tmp_path, monkeypatch):
    """缺陷 18（写侧）：落种替换失败如实返回 False，且不留目标文件/.tmp。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"character": "shenshen"}),
                                            encoding="utf-8")

    def deny(temp, target, attempts=5):
        raise OSError("replace denied")

    monkeypatch.setattr(sm, "atomic_replace_with_retry", deny)

    assert sm.seed_slot_config_from_main(config_dir, 1) is False
    assert not sm.get_config_path_for_slot(config_dir, 1).exists()
    assert list(config_dir.glob("*.tmp")) == []


def test_read_live_instances_keeps_truncated_marker(tmp_path):
    """缺陷 18（读侧）：解析失败的标记只跳过、绝不删。

    读写竞态窗口内那可能是活进程正在写的半截文件；删掉 = 活进程的避让标记
    凭空消失（其它窗按错误几何避让）。
    """
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / f"pet-runtime-v2-{os.getpid()}-slot-1.json"
    marker.write_text(f'{{"pid": {os.getpid()}, "x": 10', encoding="utf-8")  # 半写截断

    assert sm.read_live_instances(config_dir) == []
    assert marker.exists(), "解析失败不得删标记"


def test_read_live_instances_keeps_live_marker_with_bad_geometry(tmp_path):
    """缺陷 18（读侧）：活 pid 但几何字段非法 → 不删（进程还活着，删了就永久失去避让）。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / "pet-runtime-v2-4002-slot-2.json"
    marker.write_text(json.dumps({"pid": 4002, "x": "abc", "y": 0, "w": 3, "h": 4}),
                      encoding="utf-8")

    assert sm.read_live_instances(config_dir, pid_alive_fn=lambda pid: pid == 4002) == []
    assert marker.exists(), "活进程的标记不得因几何字段非法被删"


def test_read_live_instances_ignores_non_dict_marker_without_deleting(tmp_path):
    """缺陷 18（读侧）：JSON 合法但非对象（判不出 pid）→ 只跳过不删。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    marker = config_dir / f"pet-runtime-v2-{os.getpid()}-slot-1.json"
    marker.write_text("[]", encoding="utf-8")

    assert sm.read_live_instances(config_dir) == []
    assert marker.exists(), "字段非法判不出 pid：只跳过不删"


def test_read_live_instances_reclaims_confirmed_dead_pid(tmp_path):
    """缺陷 18（读侧）回归：pid 已确认死亡的标记仍被回收（死 pid 不虚增避让计数）。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    dead = config_dir / "pet-runtime-v2-4001-slot-1.json"
    dead.write_text(json.dumps({"pid": 4001, "x": 1, "y": 2, "w": 3, "h": 4}),
                    encoding="utf-8")
    alive = config_dir / "pet-runtime-v2-4002-slot-2.json"
    alive.write_text(json.dumps({"pid": 4002, "x": 5, "y": 6, "w": 7, "h": 8}),
                     encoding="utf-8")

    live = sm.read_live_instances(config_dir, pid_alive_fn=lambda pid: pid == 4002)

    assert live == [(4002, 5, 6, 7, 8)]
    assert not dead.exists(), "死 pid 的标记必须被回收"
    assert alive.exists(), "活 pid 的标记必须保留"


def test_app_main_validates_slot_arg():
    """测试 app.main 校验 --slot 参数范围（身份空间 0~128）及非法值。"""
    from pet import app as app_mod

    # 负数
    assert app_mod.main(["dsh-pet", "--slot", "-1"]) == 1
    # 超大值：上界已按规范 §一 扩到含 128（129 个身份），129 才是非法值
    assert app_mod.main(["dsh-pet", "--slot", "129"]) == 1
    # 非法字符串
    assert app_mod.main(["dsh-pet", "--slot", "abc"]) == 1
    # 缺少值
    assert app_mod.main(["dsh-pet", "--slot"]) == 1

