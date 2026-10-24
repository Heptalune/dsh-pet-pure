# -*- coding: utf-8 -*-
"""G1a 身份存储切片：``pet/pet_profiles.py`` 的语义单测（本批**不接消费者**）。

判据来源：``docs/PRODUCT-NEXT-STEPS-SPEC-2026-10-09.md`` §一/§五/§八 G1。
本文件覆盖：槽档案路径（含 0）、身份空间与最小非活跃分配（0..128 = 129 个）、
v2 活跃集合读写（缺失=[0]、v2 空=空、v1 一律补 0、坏文件不当新装）、
slot0 幂等备份迁移（已有档案绝不覆盖）、同源默认（缺键不继承全局主宠）、
代次（迟到几何写拒绝）、本宠 reset（保身份 + 保位置 rx/ry/screen_name、
朝向属偏好）。

跨进程部分（并发迁移、并发不同键并写）用**真子进程**；线程内/进程内语义用
临时目录。禁止固定 sleep 猜时序：子进程同步走 stdout token，宽预算 60s。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pet import catalog, config_write_lock as cwl, overlay_spawn_state as ovs
from pet import pet_profiles as pp
from pet import settings_slot_config as ssc
from pet.config import Config

REPO = str(Path(__file__).resolve().parent.parent)
WAIT = 60.0


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dump(path: Path, data) -> None:
    _write(path, json.dumps(data, ensure_ascii=False, indent=2))


def _defaults() -> dict:
    """``Config`` 的出厂默认（不起 Config 构造：不读盘、不落种）。"""
    return Config.__new__(Config)._initial_data()


def _begin(config_dir: Path, slot: int) -> int:
    """推进代次并返回新值（失败即用例失败——成功路径才返回 int）。"""
    result = pp.begin_slot_generation(config_dir, slot)
    assert result.status == "ok", result
    assert result.generation is not None
    return result.generation


# ---------------------------------------------------------------- 子进程脚本
#: add     —— 并发往同一份 slot 档案写自己那一个键（不同键并写不互相吞掉）
#: migrate —— 并发跑 slot0 迁移（同一次迁移只许一个进程"created"）
#: hold    —— 抢到目录写锁 → 报 LOCKED → 等 release 哨兵 → 退出（父进程测超时用）
#: silent  —— 什么都不打印、睡 30s（只给 ``_Child`` 的有界等待做自检）
_CHILD = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["PP_REPO"])
from pet import pet_profiles as pp

mode = os.environ["PP_MODE"]
config_dir = os.environ["PP_DIR"]

if mode == "add":
    key = os.environ["PP_KEY"]
    for i in range(int(os.environ["PP_ITER"])):
        result = pp.write_profile_settings(config_dir, 0, {key: i}, timeout=30.0)
        if result.status != "ok":
            print("FAILED %s" % result.status, flush=True)
            sys.exit(3)
    print("DONE", flush=True)
elif mode == "migrate":
    print("STATUS %s" % pp.migrate_slot0_profile(config_dir).status, flush=True)
elif mode == "hold":
    from pet import config_write_lock as cwl
    with cwl.config_write_lock(config_dir, timeout=10.0):
        print("LOCKED", flush=True)
        release = os.environ["PP_RELEASE"]
        deadline = time.monotonic() + 60.0
        while not os.path.exists(release) and time.monotonic() < deadline:
            time.sleep(0.01)
        print("RELEASING", flush=True)
elif mode == "silent":
    time.sleep(30.0)
else:
    print("BAD-MODE", flush=True)
    sys.exit(2)
"""


class _Child:
    """真子进程 + **有界**通信（审查 P8）。

    旧写法用阻塞 ``readline()`` 等 token：deadline 形同虚设，子进程只要不说话就能把
    整个测试套件挂死（测试自己变成事故源）。这里 stdout/stderr 各由一个读线程灌进
    queue/列表，所有等待都带超时；超时立刻 kill 子进程并带诊断失败——**测试绝不无限等**。
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._lines: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:  # noqa: BLE001 - 读线程只负责转送
            pass
        finally:
            self._lines.put(None)

    def _pump_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                self._stderr.append(line)
        except Exception:  # noqa: BLE001
            pass

    @property
    def stderr(self) -> str:
        return "".join(self._stderr)

    def next_line(self, timeout: float = WAIT) -> str:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.kill()
            raise AssertionError(
                f"{timeout}s 内没有子进程输出；stderr={self.stderr[:500]!r}")
        if line is None:
            raise AssertionError(
                f"子进程输出已结束（退出码 {self.proc.poll()}）；"
                f"stderr={self.stderr[:500]!r}")
        return line.strip()

    def wait_token(self, token: str, timeout: float = WAIT) -> None:
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.kill()
                raise AssertionError(
                    f"子进程未在 {timeout}s 内输出 {token}（已见 {seen}）"
                    f"stderr={self.stderr[:500]!r}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(
                    f"子进程输出结束仍未出现 {token}（已见 {seen}，退出码 "
                    f"{self.proc.poll()}）stderr={self.stderr[:500]!r}")
            text = line.strip()
            seen.append(text)
            if text == token:
                return

    def join(self, timeout: float = WAIT) -> None:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError(
                f"子进程未在 {timeout}s 内退出；stderr={self.stderr[:500]!r}")
        assert code == 0, f"子进程退出码 {code}，stderr={self.stderr[:500]!r}"

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _spawn(mode: str, config_dir: Path, **extra) -> _Child:
    env = dict(os.environ)
    env.update({
        "PP_REPO": REPO,
        "PP_MODE": mode,
        "PP_DIR": str(config_dir),
    })
    env.update({key: str(value) for key, value in extra.items()})
    return _Child(subprocess.Popen([sys.executable, "-c", _CHILD], env=env,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True))


def _holder(config_dir: Path) -> tuple[_Child, Path]:
    """起一个真的持锁子进程并等它确认（release 哨兵随返回给出）。"""
    release = config_dir / "release.sentinel"
    child = _spawn("hold", config_dir, PP_RELEASE=release)
    child.wait_token("LOCKED")
    return child, release


def _release(child: _Child, release: Path) -> None:
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text("go", encoding="utf-8")
    child.join()


# ---------------------------------------------------------------- 路径与身份空间
def test_profile_paths_are_stable_and_include_slot_zero(tmp_path):
    assert pp.slot_profile_path(tmp_path, 0) == tmp_path / "config-slot-0.json"
    assert pp.slot_profile_path(tmp_path, 1) == tmp_path / "config-slot-1.json"
    assert pp.slot_profile_path(tmp_path, 128) == tmp_path / "config-slot-128.json"
    assert pp.global_config_path(tmp_path) == tmp_path / "config.json"
    assert pp.slot_profile_exists(tmp_path, 0) is False
    _dump(pp.slot_profile_path(tmp_path, 0), {"version": 4})
    assert pp.slot_profile_exists(tmp_path, 0) is True


def test_identity_space_is_zero_to_128(tmp_path):
    """0 加入后总身份数 = 129（不是把上界缩到 127 丢掉旧的 slot 128）。"""
    assert pp.SLOT_MIN == 0
    assert pp.SLOT_MAX == 128
    assert pp.SLOT_COUNT == 129
    assert pp.SLOT_COUNT == pp.SLOT_MAX - pp.SLOT_MIN + 1
    for bad in (-1, 129, 1000):
        with pytest.raises(pp.SlotRangeError):
            pp.slot_profile_path(tmp_path, bad)
    with pytest.raises(pp.SlotRangeError):
        pp.slot_profile_path(tmp_path, "x")  # type: ignore[arg-type]


def test_sidecar_names_do_not_collide_with_existing_globs():
    """活跃集合沿用生产文件名（原地升 v2）；代次文件不得撞 config-*/runtime-* glob。"""
    assert pp.ACTIVE_SET_FILENAME == "overlay-active-pets.json"
    for pattern in ("config-", "runtime-", "config-slot-", "pet-runtime-v2-"):
        assert not pp.GENERATION_FILENAME.startswith(pattern), pp.GENERATION_FILENAME
    assert pp.GENERATION_FILENAME.endswith(".json")


def test_normalize_slots_keeps_zero_and_drops_out_of_range():
    assert pp.normalize_slots([2, 1, 0, -1, "x", 129, 1, None]) == [2, 1, 0]
    assert pp.normalize_slots([0]) == [0]
    assert pp.normalize_slots(None) == []
    assert pp.normalize_slots([128, 129]) == [128]


# ---------------------------------------------------------------- 最小非活跃分配
def test_allocate_returns_zero_even_when_profile_exists(tmp_path):
    """已有档案不阻止"恢复"：0 号档案在盘上也算空闲（恢复 ≠ 覆盖）。"""
    _dump(pp.slot_profile_path(tmp_path, 0), {"version": 4, "character": "dog"})
    _dump(pp.slot_profile_path(tmp_path, 1), {"version": 4})
    assert pp.allocate_slot(active_slots=()) == 0
    assert pp.allocate_slot(active_slots=[1]) == 0
    assert pp.allocate_slot(active_slots=[0]) == 1
    # 档案内容一个字节都没被分配动作碰过
    assert json.loads(pp.slot_profile_path(tmp_path, 0).read_text(
        encoding="utf-8"))["character"] == "dog"


def test_allocate_picks_smallest_non_active(tmp_path):
    assert pp.allocate_slot(active_slots=[0, 2]) == 1
    assert pp.allocate_slot(active_slots=[2]) == 0
    assert pp.allocate_slot(active_slots=[0, 1, 2]) == 3
    assert pp.allocate_slot(active_slots=["1", 0, None]) == 2


def test_allocate_keeps_top_identity_and_reports_capacity(tmp_path):
    """0..127 活跃时仍能分配 128（上界不缩短）；0..128 全活跃才真满。"""
    assert pp.allocate_slot(active_slots=list(range(0, 128))) == 128
    assert pp.allocate_slot(active_slots=list(range(1, 129))) == 0
    with pytest.raises(pp.CapacityFullError) as excinfo:
        pp.allocate_slot(active_slots=list(range(0, 129)))
    assert "129" in str(excinfo.value)
    with pytest.raises(pp.CapacityFullError):
        pp.allocate_slot(active_slots=range(0, 129))


# ---------------------------------------------------------------- 活跃集合 v2
def test_active_set_missing_file_is_fresh_install_default_zero(tmp_path):
    state = pp.load_active_set(tmp_path)
    assert state.status == "missing"
    assert state.slots == (0,)
    assert state.raw_version is None
    assert state.path == tmp_path / pp.ACTIVE_SET_FILENAME


def test_active_set_v2_empty_stays_empty(tmp_path):
    """有效 v2 空集合是合法状态：不得静默补一只。"""
    _dump(pp.active_set_path(tmp_path),
          {"version": 2, "slots": []})
    state = pp.load_active_set(tmp_path)
    assert state.status == "ok"
    assert state.slots == ()
    assert state.raw_version == 2


def test_active_set_v2_keeps_zero_and_reports_dropped_entries(tmp_path):
    _dump(pp.active_set_path(tmp_path),
          {"version": 2, "slots": [0, 300, -1, "x", 5, 5]})
    state = pp.load_active_set(tmp_path)
    assert state.status == "ok"
    assert state.slots == (0, 5)
    assert state.dropped == (300, -1, "x")


@pytest.mark.parametrize("legacy,expected", [
    ([1, 2], (0, 1, 2)),
    ([], (0,)),               # 主代理裁决：v1 空子集合迁移后必须 [0]，不是 []
    ([2], (0, 2)),
    ([0, 3], (0, 3)),         # 旧文件里偶然带 0：不得重复
    ([3, 0, 2], (3, 0, 2)),   # 顺序保持（0 已在则原样）
    ([2, None, "a"], (0, 2)),  # 非法条目丢弃但 0 照样补上
])
def test_active_set_v1_any_list_gets_zero(tmp_path, legacy, expected):
    """v1 时代的 0 是隐式主宠：任何旧列表（含空）迁移视图都必须补上 0。"""
    _dump(pp.active_set_path(tmp_path), {"version": 1, "slots": legacy})
    state = pp.load_active_set(tmp_path)
    assert state.status == "migrated-v1"
    assert state.slots == expected
    assert state.raw_version == 1


@pytest.mark.parametrize("text,status", [
    ("{ not json", "corrupt"),
    ("", "corrupt"),          # 崩溃留下的 0 字节文件同样是"损坏"，不是新装
    ("[]", "corrupt"),
    ('"slots"', "corrupt"),
    ('{"version": 2}', "corrupt"),
    ('{"version": 2, "slots": {"a": 1}}', "corrupt"),
    ('{"version": 3, "slots": [0]}', "unsupported-version"),
    ("{}", "corrupt"),
])
def test_active_set_broken_is_not_treated_as_fresh_install(tmp_path, text, status):
    """坏文件/未知版本：只报告，不覆盖，也**不**当作新装返回 [0]。"""
    path = pp.active_set_path(tmp_path)
    _write(path, text)
    state = pp.load_active_set(tmp_path)
    assert state.status == status
    assert state.slots == ()
    assert state.reason
    assert path.read_text(encoding="utf-8") == text, "读路径不得改写坏文件"


def test_save_active_set_roundtrip_and_normalization(tmp_path):
    result = pp.save_active_set(tmp_path, [0, 2])
    assert result.status == "ok"
    assert json.loads(pp.active_set_path(tmp_path).read_text(encoding="utf-8")) == {
        "version": 2, "slots": [0, 2]}
    assert pp.load_active_set(tmp_path).slots == (0, 2)

    assert pp.save_active_set(tmp_path, []).status == "ok"
    assert pp.load_active_set(tmp_path).slots == ()

    assert pp.save_active_set(tmp_path, [2, 0, 0, "x", 999]).status == "ok"
    assert pp.load_active_set(tmp_path).slots == (2, 0)


def test_save_active_set_refuses_to_overwrite_broken_file(tmp_path):
    path = pp.active_set_path(tmp_path)
    _write(path, "{ not json")
    result = pp.save_active_set(tmp_path, [0])
    assert result.status == "refused-corrupt"
    assert path.read_text(encoding="utf-8") == "{ not json"

    result = pp.save_active_set(tmp_path, [0], on_broken="backup-overwrite")
    assert result.status == "ok"
    assert pp.load_active_set(tmp_path).slots == (0,)
    backups = list(tmp_path.glob(f"{pp.ACTIVE_SET_FILENAME}.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ not json"


def test_migrate_active_set_v1_backs_up_and_is_idempotent(tmp_path):
    path = pp.active_set_path(tmp_path)
    legacy_bytes = json.dumps({"version": 1, "slots": [1, 2]}, indent=2)
    _write(path, legacy_bytes)
    result = pp.migrate_active_set(tmp_path)
    assert result.status == "migrated"
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 2, "slots": [0, 1, 2]}
    backup = path.with_name(f"{path.name}.v1.bak")
    assert backup.read_text(encoding="utf-8") == legacy_bytes

    after_first = path.read_bytes()
    backup_bytes = backup.read_bytes()
    assert pp.migrate_active_set(tmp_path).status == "already-v2"
    assert path.read_bytes() == after_first, "重跑必须字节不变（幂等）"
    assert backup.read_bytes() == backup_bytes, "备份不得被重跑覆盖"


def test_migrate_active_set_v1_empty_becomes_zero(tmp_path):
    """主代理裁决（规范 §一）：v1 空子集合迁移后是 [0]，不是 []。"""
    _dump(pp.active_set_path(tmp_path), {"version": 1, "slots": []})
    assert pp.migrate_active_set(tmp_path).status == "migrated"
    assert pp.load_active_set(tmp_path).slots == (0,)


def test_migrate_active_set_never_touches_broken_or_missing_file(tmp_path):
    path = pp.active_set_path(tmp_path)
    assert pp.migrate_active_set(tmp_path).status == "no-file"
    assert not path.exists(), "缺失 = 新装默认 [0]，不需要凭空创建文件"

    _write(path, "{ not json")
    result = pp.migrate_active_set(tmp_path)
    assert result.status == "corrupt"
    assert path.read_text(encoding="utf-8") == "{ not json"
    assert list(tmp_path.glob(f"{pp.ACTIVE_SET_FILENAME}.*.bak")) == []
    assert list(tmp_path.glob(f"{pp.ACTIVE_SET_FILENAME}.corrupt-*")) == []

    _dump(path, {"version": 3, "slots": [0]})
    result = pp.migrate_active_set(tmp_path)
    assert result.status == "unsupported-version"
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 3, "slots": [0]}


# ---------------------------------------------------------------- slot0 档案迁移
def _global_with_per_pet(config_dir: Path) -> dict:
    data = {
        "version": 4,
        "rx": 0.25, "ry": 0.75, "screen_name": "DP-1", "facing": "right",
        "scale": 1.4, "character": "cat", "collision_enabled": False,
        "self_talk_enabled": True, "user_customized": False,
        "pet_opacity": 80, "on_top": False, "no_move": True,
        "chat": {"providers": {}}, "agent_link": {"enabled": True},
        "quiet_hours": {"enabled": True},
    }
    _dump(pp.global_config_path(config_dir), data)
    return data


def test_slot0_migration_extracts_per_pet_keys_and_geometry(tmp_path):
    source = _global_with_per_pet(tmp_path)
    source_bytes = pp.global_config_path(tmp_path).read_bytes()

    result = pp.migrate_slot0_profile(tmp_path)
    assert result.status == "created"
    assert result.profile_path == pp.slot_profile_path(tmp_path, 0)
    target = json.loads(result.profile_path.read_text(encoding="utf-8"))
    for key in ("rx", "ry", "screen_name", "facing", "scale", "character",
                "collision_enabled", "self_talk_enabled"):
        assert target[key] == source[key], key
    assert target["version"] == pp.PROFILE_FILE_VERSION
    assert target["user_customized"] is True  # 档案已含用户真值，落种刷新不得覆盖
    for global_key in ("pet_opacity", "on_top", "no_move", "chat", "agent_link",
                       "quiet_hours"):
        assert global_key not in target, f"全局键不得进档案: {global_key}"

    # 先备份再写：备份 = 迁移前的全局文件字节
    assert result.backup_path == tmp_path / "config.json.pre-slot0-migration.bak"
    assert result.backup_path.read_bytes() == source_bytes
    remaining = json.loads(pp.global_config_path(tmp_path).read_text(encoding="utf8"))
    assert not pp.PER_PET_KEYS.intersection(remaining)
    assert remaining["pet_opacity"] == source["pet_opacity"]
    assert set(result.copied_keys) >= {"rx", "ry", "character"}
    assert result.retained_keys == ()


def test_slot0_migration_never_overwrites_existing_profile(tmp_path):
    _global_with_per_pet(tmp_path)
    target = pp.slot_profile_path(tmp_path, 0)
    _dump(target, {"version": 4, "character": "dog", "sentinel": 7})
    before = target.read_bytes()

    result = pp.migrate_slot0_profile(tmp_path)
    assert result.status == "already-exists"
    assert target.read_bytes() == before
    assert (tmp_path / "config.json.pre-slot0-migration.bak").exists()


def test_slot0_migration_existing_profile_cleans_source_and_keeps_it_recoverable(tmp_path):
    """目标已存在：**不覆盖目标**、**清理源每宠键**，且被清掉的值必须取得回。

    清理这一半与 ``Config.save()`` 是同一条规则（它对全局身份无条件
    ``pop(PER_PET_KEYS | SLOT_LOCAL_KEYS)``）：迁移侧"不清源"留不住任何东西，
    只会在下一次保存时被抹掉，``retained_keys`` 也就成了骗人的字段。
    "不丢数据"由**备份**承担——规范备份本来就是这批值（`_backup_file` 在这条支路
    之前跑），所以不需要第二份副本。
    """
    source = _global_with_per_pet(tmp_path)
    target = pp.slot_profile_path(tmp_path, 0)
    _dump(target, {"version": 4, "character": "dog", "sentinel": 7})
    before_target = target.read_bytes()

    result = pp.migrate_slot0_profile(tmp_path)

    assert result.status == "already-exists"
    assert target.read_bytes() == before_target, "目标档案一个字节都不许动"

    remaining = json.loads(pp.global_config_path(tmp_path).read_text(encoding="utf8"))
    assert not pp.PER_PET_KEYS.intersection(remaining), "源每宠键必须清干净"
    assert remaining["pet_opacity"] == source["pet_opacity"], "全局键一个都不许少"

    # 被清掉的那批值全部可从规范备份取回（这就是"不丢数据"的落点）
    assert result.backup_path is not None and result.backup_path.exists()
    backup = json.loads(result.backup_path.read_text(encoding="utf8"))
    for key in ("rx", "ry", "screen_name", "facing", "scale", "character"):
        assert backup[key] == source[key], key
    assert result.recovery_path is None, "规范备份本来就是这批值，不必再存副本"

    # 未采用的键如实登记（可诊断），copied_keys 只记真的写进档案的
    assert set(result.retained_keys) >= {"rx", "ry", "character"}
    assert result.copied_keys == ()
    assert result.backup_path.name in result.reason, "诊断信息必须点名备份文件"

    # 本函数**不**把源几何补进既有档案（「不覆盖目标」是已发布契约），所以它
    # 修不了"位置落回右下角"：档案缺 rx/ry 时，覆盖层落点由档案决定（缺 → 默认
    # 右下角），全局 config.json 的 rx/ry 在覆盖层里没有读者。别把这条支路当成
    # 位置丢失的修复。
    restored = pp.read_slot_profile(tmp_path, 0)
    assert restored.value("rx") is None, "目标档案缺的位置不会被源值补上"
    assert restored.value("ry") is None


def test_slot0_migration_existing_profile_consistency_with_config_save(tmp_path):
    """迁移清源 == ``Config.save()`` 的全局清理：两条路不许有分歧。

    分歧的真实形态（本用例钉住的就是它）：迁移说"不清源"，源里留着每宠键，
    但 ``Config.save()`` 的 ``pop(PER_PET_KEYS | SLOT_LOCAL_KEYS)`` 是无条件的，
    下一次保存就把它们抹掉——于是 `retained_keys` 描述的"保留"在盘上并不成立，
    用户拿到的是一份写着"保留"却已经空掉的配置。这里连续走两步（先迁移、再真保存），
    断言源侧**始终**没有每宠键、目标档案**始终**逐字节不变。
    """
    from pet.config import APP_DIR_NAME, Config

    cfg_dir = tmp_path / APP_DIR_NAME          # Config.dir = base / APP_DIR_NAME
    source = _global_with_per_pet(cfg_dir)
    target = pp.slot_profile_path(cfg_dir, 0)
    _dump(target, {"version": 4, "character": "dog", "sentinel": 7})
    before_target = target.read_bytes()

    assert pp.migrate_slot0_profile(cfg_dir).status == "already-exists"
    after_migration = json.loads(pp.global_config_path(cfg_dir).read_text(encoding="utf8"))
    assert not pp.PER_PET_KEYS.intersection(after_migration), \
        "迁移这一步就该清完（否则 save 会再清一次 = 两条规则）"

    cfg = Config(base=tmp_path, instance_id="")
    assert cfg.path == pp.global_config_path(cfg_dir), "夹具钉住：save 的就是迁移源"
    assert cfg.save() is True
    after_save = json.loads(pp.global_config_path(cfg_dir).read_text(encoding="utf8"))
    assert not pp.PER_PET_KEYS.intersection(after_save), "保存后源侧同样不许有每宠键"
    assert after_save["pet_opacity"] == source["pet_opacity"]
    assert target.read_bytes() == before_target, "保存路径同样不许动目标档案"


def test_slot0_migration_existing_profile_keeps_a_different_backup_and_gets_recovery_copy(tmp_path):
    """规范备份已被**另一份快照**占用：不覆盖它，另存副本把当前值留住。

    这是"清源"唯一真正会丢数据的场面：``_is_global_config_snapshot`` 只查
    **键在不在**，所以一份**旧**快照也能通过校验并被"保留第一份"规则保护下来。
    若这时照旧清源，源里**当前**的每宠值就再无着落（旧版本退回过、又升级回来时
    真实可达）。契约：规范备份逐字节不动 + 当前值可从 ``recovery_path`` 取回。
    """
    source = _global_with_per_pet(tmp_path)
    target = pp.slot_profile_path(tmp_path, 0)
    _dump(target, {"version": 4, "character": "dog", "sentinel": 7})

    # 旧快照 = 键齐全（过得了"是不是全局配置快照"的入场校验）但**值不同**
    stale = dict(source)
    stale.update({"character": "dog", "scale": 0.5, "rx": 1.0, "ry": 2.0,
                  "screen_name": "DISPLAY1", "facing": "right", "pet_opacity": 42})
    canonical = tmp_path / "config.json.pre-slot0-migration.bak"
    _dump(canonical, stale)
    canonical_before = canonical.read_bytes()

    result = pp.migrate_slot0_profile(tmp_path)

    assert result.status == "already-exists"
    assert canonical.read_bytes() == canonical_before, "规范备份（旧快照）不得被覆盖"
    assert result.recovery_path is not None, "必须另存恢复副本"
    assert result.recovery_path.exists()
    copy = json.loads(result.recovery_path.read_text(encoding="utf8"))
    for key in ("rx", "ry", "screen_name", "facing", "scale", "character"):
        assert copy[key] == source[key], f"当前值 {key} 必须留在恢复副本里"
    assert result.recovery_path.name in result.reason, "诊断信息必须点名恢复副本"


def test_slot0_migration_existing_profile_refuses_to_clean_without_a_recovery_copy(
        tmp_path, monkeypatch):
    """取不到恢复副本就**不清源**：宁可留下双份，也不做不可恢复的删除。

    不改这里的话，``_copy_file`` 失败会静默降级成"照旧清源"——源里当前值没了、
    备份里只有旧快照，正是"数据丢了还没人知道"。
    """
    source = _global_with_per_pet(tmp_path)
    target = pp.slot_profile_path(tmp_path, 0)
    _dump(target, {"version": 4, "character": "dog", "sentinel": 7})
    before_target = target.read_bytes()
    # 键齐全但值不同的旧快照 → 校验通过、但"装着这批值"为假 → 才走到恢复副本这一步
    stale = dict(source)
    stale.update({"character": "dog", "scale": 0.5, "rx": 1.0, "ry": 2.0,
                  "screen_name": "DISPLAY1", "facing": "right"})
    _dump(tmp_path / "config.json.pre-slot0-migration.bak", stale)

    def boom(*_args, **_kwargs):
        return False

    monkeypatch.setattr(pp, "_copy_file", boom)
    result = pp.migrate_slot0_profile(tmp_path)

    assert result.status == "backup-failed", result
    remaining = json.loads(pp.global_config_path(tmp_path).read_text(encoding="utf8"))
    assert remaining["character"] == source["character"], "取不到副本就不许清源"
    assert remaining["rx"] == source["rx"]
    assert target.read_bytes() == before_target, "目标档案照旧一个字节不动"


def test_slot0_migration_is_idempotent_and_retryable(tmp_path, monkeypatch):
    _global_with_per_pet(tmp_path)
    target = pp.slot_profile_path(tmp_path, 0)

    def boom(temp, dest, attempts=5):
        raise OSError("disk on fire")

    monkeypatch.setattr(cwl, "atomic_replace_with_retry", boom)
    failed = pp.migrate_slot0_profile(tmp_path)
    assert failed.status == "write-failed"
    assert not target.exists(), "写失败不得留半成品档案"
    assert (tmp_path / "config.json.pre-slot0-migration.bak").exists(), "备份先行"
    monkeypatch.undo()

    # 重试成功（中途失败可重跑，不撒谎原子事务）
    assert pp.migrate_slot0_profile(tmp_path).status == "created"
    first = target.read_bytes()
    assert pp.migrate_slot0_profile(tmp_path).status == "already-exists"
    assert target.read_bytes() == first


def test_slot0_migration_rejects_a_non_backup_file_at_the_backup_path(tmp_path):
    """备份路径上放一份 SENTINEL（不是迁移前快照）≠ "备份已完成"。

    "已存在就跳过"这种口径会把任意文件当成备份证据：真正的前置条件要么压根没做、
    要么做了却没验证。正确行为是**拒绝迁移**并如实报告，同时**不覆盖**那个文件
    （它可能是用户的别的东西）。
    """
    _global_with_per_pet(tmp_path)
    backup = tmp_path / "config.json.pre-slot0-migration.bak"
    _write(backup, "SENTINEL")
    result = pp.migrate_slot0_profile(tmp_path)
    assert result.status == "backup-invalid", result
    assert backup.read_text(encoding="utf-8") == "SENTINEL", "不得覆盖路径上的既有文件"
    assert not pp.slot_profile_path(tmp_path, 0).exists(), "证据无效必须中止，不写档案"


def test_slot0_migration_keeps_a_genuine_existing_backup(tmp_path):
    """真备份（内容就是迁移前的全局配置）保留第一份，不覆盖。"""
    source_bytes = json.dumps(_global_with_per_pet(tmp_path), ensure_ascii=False).encode()
    backup = tmp_path / "config.json.pre-slot0-migration.bak"
    backup.write_bytes(source_bytes)
    result = pp.migrate_slot0_profile(tmp_path)
    assert result.status == "created", result
    assert backup.read_bytes() == source_bytes, "已有真备份不得被重写"


def test_slot0_migration_aborts_on_backup_failure_without_partial_backup(tmp_path, monkeypatch):
    """备份复制中断：最终名上**不许**留下残片（唯一 tmp → 完整写 → 原子发布）。"""
    _global_with_per_pet(tmp_path)
    backup = tmp_path / "config.json.pre-slot0-migration.bak"
    real_copy = shutil.copy2

    def partial_copy(src, dst, **kwargs):
        Path(dst).write_text("{ partial", encoding="utf-8")   # 模拟复制到一半被中断
        raise OSError("copy interrupted")

    monkeypatch.setattr(pp.shutil, "copy2", partial_copy)
    result = pp.migrate_slot0_profile(tmp_path)
    assert result.status == "backup-failed", result
    assert not backup.exists(), "中断的复制不得在最终名上留下残片（会被误认成完成）"
    assert not pp.slot_profile_path(tmp_path, 0).exists()
    assert pp.global_config_path(tmp_path).is_file(), "源全局文件不得丢"
    monkeypatch.setattr(pp.shutil, "copy2", real_copy)
    assert pp.migrate_slot0_profile(tmp_path).status == "created"


def test_slot0_migration_backup_failure_without_copy_leaves_nothing(tmp_path, monkeypatch):
    _global_with_per_pet(tmp_path)
    monkeypatch.setattr(
            pp, "_backup_file",
            lambda src, dst, validate=None: pp.BACKUP_FAILED)
    result = pp.migrate_slot0_profile(tmp_path)
    assert result.status == "backup-failed"
    assert not pp.slot_profile_path(tmp_path, 0).exists(), "备份失败必须中止，不写档案"
    assert pp.global_config_path(tmp_path).is_file(), "源全局文件不得丢"


def test_migrate_active_set_rejects_a_non_v1_backup(tmp_path):
    path = pp.active_set_path(tmp_path)
    legacy = json.dumps({"version": 1, "slots": [1, 2]}, indent=2)
    _write(path, legacy)
    backup = path.with_name(f"{path.name}.v1.bak")
    _write(backup, "SENTINEL")
    result = pp.migrate_active_set(tmp_path)
    assert result.status == "backup-invalid", result
    assert backup.read_text(encoding="utf-8") == "SENTINEL"
    assert path.read_text(encoding="utf-8") == legacy, "证据无效时不得升级文件"


def test_migrate_active_set_keeps_a_genuine_v1_backup(tmp_path):
    path = pp.active_set_path(tmp_path)
    legacy = json.dumps({"version": 1, "slots": [1, 2]}, indent=2)
    _write(path, legacy)
    backup = path.with_name(f"{path.name}.v1.bak")
    _write(backup, legacy)
    assert pp.migrate_active_set(tmp_path).status == "migrated"
    assert backup.read_text(encoding="utf-8") == legacy, "已有真备份不得被重写"


def test_migrate_active_set_backup_failure_leaves_no_partial_backup(tmp_path, monkeypatch):
    path = pp.active_set_path(tmp_path)
    legacy = json.dumps({"version": 1, "slots": [1, 2]}, indent=2)
    _write(path, legacy)

    def partial_copy(src, dst, **kwargs):
        Path(dst).write_text("{ partial", encoding="utf-8")
        raise OSError("copy interrupted")

    monkeypatch.setattr(pp.shutil, "copy2", partial_copy)
    result = pp.migrate_active_set(tmp_path)
    assert result.status == "backup-failed", result
    backup = path.with_name(f"{path.name}.v1.bak")
    assert not backup.exists(), "中断的备份不得在最终名上留下残片"
    assert path.read_text(encoding="utf-8") == legacy, "升级失败时原文件不得被改"


def test_slot0_migration_source_states(tmp_path):
    assert pp.migrate_slot0_profile(tmp_path).status == "no-source"

    _write(pp.global_config_path(tmp_path), "{ not json")
    result = pp.migrate_slot0_profile(tmp_path)
    assert result.status == "source-corrupt"
    assert not pp.slot_profile_path(tmp_path, 0).exists()
    assert pp.global_config_path(tmp_path).read_text(encoding="utf-8") == "{ not json"


def test_slot0_migration_without_any_per_pet_key_creates_nothing(tmp_path):
    """只有全局键的 config.json：没有可迁移的真值，不造"空档案"。"""
    _dump(pp.global_config_path(tmp_path),
          {"version": 4, "pet_opacity": 80, "on_top": False})
    source_bytes = pp.global_config_path(tmp_path).read_bytes()
    result = pp.migrate_slot0_profile(tmp_path)
    assert result.status == "no-source"
    assert not pp.slot_profile_path(tmp_path, 0).exists()
    assert pp.global_config_path(tmp_path).read_bytes() == source_bytes
    assert not (tmp_path / "config.json.pre-slot0-migration.bak").exists()


def test_migrated_profile_is_the_truth_not_the_retained_global_keys(tmp_path):
    """迁移后档案即真值：全局配置之后怎么改，都已迁走的每宠键都不再跟随。

    （源全局键暂留是过渡（legacy 单窗还在读它），但**读侧真值只有档案**——
    否则同一个值有两个活读源，正是规范禁止的"双份有效真值"。）
    """
    _global_with_per_pet(tmp_path)
    assert pp.migrate_slot0_profile(tmp_path).status == "created"
    profile = pp.read_slot_profile(tmp_path, 0)
    assert profile.value("character") == "cat"
    assert profile.value("scale") == 1.4

    # 全局侧后来被改（例如 legacy 路径/设置页写主配置）
    changed = _global_with_per_pet(tmp_path)
    changed.update({"character": "dog", "scale": 0.6})
    _dump(pp.global_config_path(tmp_path), changed)
    after = pp.read_slot_profile(tmp_path, 0)
    assert after.value("character") == "cat", "档案是真值，不随全局漂移"
    assert after.value("scale") == 1.4

    # 反过来：档案被就地改写（用户在该宠自己的设置页改了角色），读到的就是新值
    assert pp.write_profile_settings(tmp_path, 0, {"character": "bird"}).status == "ok"
    assert pp.read_slot_profile(tmp_path, 0).value("character") == "bird"


def test_slot0_migration_refuses_a_slot_profile_as_source(tmp_path):
    """``--slot N`` 形态：那个进程的"全局配置"就是另一只宠的档案。

    拿它当迁移源 = 把别宠身份写成 slot0 的档案（跨身份替换，规范明令禁止）；
    ``--slot 0`` 形态更直接：源与目标同一个文件。两种情况都必须拒绝写入。
    """
    _dump(pp.slot_profile_path(tmp_path, 3), {"version": 4, "character": "fish"})
    result = pp.migrate_slot0_profile(tmp_path,
                                      source=pp.slot_profile_path(tmp_path, 3))
    assert result.status == "refused-slot-source"
    assert not pp.slot_profile_path(tmp_path, 0).exists()

    result = pp.migrate_slot0_profile(tmp_path,
                                      source=pp.slot_profile_path(tmp_path, 0))
    assert result.status == "refused-slot-source"
    assert not pp.slot_profile_path(tmp_path, 0).exists()


def test_slot0_migration_under_real_concurrency(tmp_path):
    """3 个真进程同时迁移：只许一个 created，其余 already-exists，档案不被写坏。"""
    _global_with_per_pet(tmp_path)
    children = [_spawn("migrate", tmp_path) for _ in range(3)]
    statuses = []
    for child in children:
        statuses.append(child.next_line())
        child.join()
    assert statuses.count("STATUS created") == 1, statuses
    assert statuses.count("STATUS already-exists") == 2, statuses
    target = json.loads(pp.slot_profile_path(tmp_path, 0).read_text(encoding="utf-8"))
    assert target["character"] == "cat"
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------- 同源默认（不继承全局）
def test_per_pet_defaults_come_from_config_and_cover_every_per_pet_key():
    defaults = pp.per_pet_defaults()
    config_defaults = _defaults()
    assert set(pp.PER_PET_KEYS) <= set(defaults)
    for key in pp.PER_PET_KEYS:
        assert defaults[key] == config_defaults[key], key
    # 返回值是副本：调用方改它不得污染同源默认
    defaults["scale"] = "mutated"
    assert pp.per_pet_defaults()["scale"] == config_defaults["scale"]


def test_missing_per_pet_key_uses_default_not_global_main_pet(tmp_path):
    """缺键 → 同源默认；**绝不**回落全局主宠当前值（一处真值的读侧收口）。"""
    _dump(pp.global_config_path(tmp_path),
          {"version": 4, "scale": 0.5, "character": "special", "facing": "right"})
    profile = pp.read_slot_profile(tmp_path, 0)
    assert profile.status == "missing"
    assert profile.value("scale") == _defaults()["scale"]
    assert profile.value("character") == catalog.DEFAULT_CHARACTER
    assert profile.value("facing") == _defaults()["facing"]
    assert profile.value("pet_opacity", 33) == 33, "全局键不由本模块代言"
    _dump(pp.slot_profile_path(tmp_path, 0),
          {"version": 4, "pet_opacity": 50})
    assert pp.read_slot_profile(tmp_path, 0).value("pet_opacity", 33) == 33, (
        "混进档案的全局键不得因此变成有效值（否则档案就是第二个全局真值源）")


def test_profile_value_precedence_and_null_means_no_memory(tmp_path):
    _dump(pp.global_config_path(tmp_path), {"version": 4, "scale": 0.5})
    _dump(pp.slot_profile_path(tmp_path, 2),
          {"version": 4, "scale": None, "character": "cat", "bogus": 1})
    profile = pp.read_slot_profile(tmp_path, 2)
    assert profile.status == "ok"
    assert profile.value("scale") == _defaults()["scale"], "显式 null = 没有记忆"
    assert profile.value("character") == "cat"
    assert profile.value("unknown_key", "fallback") == "fallback"


def test_profile_read_reports_corrupt_without_touching_the_file(tmp_path):
    path = pp.slot_profile_path(tmp_path, 1)
    _write(path, "{ not json")
    profile = pp.read_slot_profile(tmp_path, 1)
    assert profile.status == "corrupt"
    assert profile.exists is True
    assert profile.value("scale") == _defaults()["scale"]
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_empty_profile_file_is_corrupt_not_missing(tmp_path):
    """0 字节档案 ≠ 档案不存在：后者按同源默认读，前者是损坏（如实报告）。"""
    path = pp.slot_profile_path(tmp_path, 0)
    _write(path, "")
    assert pp.read_slot_profile(tmp_path, 0).status == "corrupt"
    assert pp.read_slot_profile(tmp_path, 1).status == "missing"


def test_per_pet_key_set_matches_existing_slot_routing():
    """键集漂移守卫：本模块的每宠键集必须与既有 slot 路由键集逐字一致。

    （旧副本仍在 ``overlay_spawn_state``；接线批把单一来源换成 ``pet_profiles``
    后本用例仍成立——它锁的是"两边不许漂移"，不是"副本必须留在这里"。）
    """
    assert pp.PER_PET_SETTING_KEYS == ovs.PER_SLOT_SETTING_KEYS | {"collision_enabled"}
    assert pp.PER_PET_KEYS == ssc.SLOT_ROUTED_KEYS
    assert pp.SLOT_LOCAL_KEYS == ssc.SLOT_LOCAL_KEYS
    assert pp.POSITION_KEYS == ("rx", "ry", "screen_name")
    assert pp.PER_PET_PREFERENCE_KEYS == pp.PER_PET_SETTING_KEYS | {"facing"}
    assert pp.PER_PET_KEYS == pp.PER_PET_PREFERENCE_KEYS | set(pp.POSITION_KEYS)


def test_deferred_per_pet_keys_are_recorded_not_silently_decided():
    """规范 §五 的"物理/碰撞"里，碰撞参数今天仍是**全局**键（唯一读点在主配置）。

    本批不把它们偷偷塞进每宠集（那会立刻造出第二份有效真值），只登记为待接线批
    裁决的键；这条用例把"登记过"钉住，防止它变成一句空口承诺。
    """
    assert pp.DEFERRED_PER_PET_KEYS == frozenset({
        "collision_restitution", "collision_friction",
        "collision_mass_scale", "collision_impulse_cap"})
    assert pp.DEFERRED_PER_PET_KEYS.isdisjoint(pp.PER_PET_KEYS)
    defaults = _defaults()
    for key in pp.DEFERRED_PER_PET_KEYS:
        assert key in defaults, f"待裁决键必须是现存全局键: {key}"


def test_profile_document_version_matches_config_default_version():
    assert pp.PROFILE_FILE_VERSION == _defaults()["version"]


# ---------------------------------------------------------------- 档案写：设置键
def test_write_profile_settings_merges_and_preserves_other_keys(tmp_path):
    path = pp.slot_profile_path(tmp_path, 0)
    _dump(path, {"version": 4, "character": "cat", "collision_enabled": True,
                 "user_customized": False})
    result = pp.write_profile_settings(tmp_path, 0, {"scale": 0.8})
    assert result.status == "ok"
    assert result.changed == ("scale",)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["scale"] == 0.8
    assert data["character"] == "cat" and data["collision_enabled"] is True
    assert data["version"] == 4
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_profile_settings_creates_missing_profile(tmp_path):
    result = pp.write_profile_settings(tmp_path, 3, {"character": "dog"})
    assert result.status == "ok"
    assert json.loads(pp.slot_profile_path(tmp_path, 3).read_text(encoding="utf-8")) == {
        "version": pp.PROFILE_FILE_VERSION, "character": "dog"}


def test_write_profile_settings_rejects_global_keys(tmp_path):
    """所有权表是闸门：全局键写进档案 = 造第二份真值，必须拒绝。"""
    path = pp.slot_profile_path(tmp_path, 0)
    _dump(path, {"version": 4})
    before = path.read_bytes()
    for key in ("pet_opacity", "on_top", "stream_capture_mode", "chat"):
        result = pp.write_profile_settings(tmp_path, 0, {key: 1})
        assert result.status == "refused-key", key
        assert path.read_bytes() == before


def test_write_profile_settings_user_customized_flag_and_empty_change(tmp_path):
    path = pp.slot_profile_path(tmp_path, 0)
    _dump(path, {"version": 4, "user_customized": False})
    assert pp.write_profile_settings(tmp_path, 0, {"scale": 1.1},
                                     user_customized=True).status == "ok"
    assert json.loads(path.read_text(encoding="utf-8"))["user_customized"] is True
    assert pp.write_profile_settings(tmp_path, 0, {"scale": 1.2},
                                     user_customized=None).status == "ok"
    assert json.loads(path.read_text(encoding="utf-8"))["user_customized"] is True

    before = path.read_bytes()
    result = pp.write_profile_settings(tmp_path, 0, {})
    assert result.status == "no-changes"
    assert path.read_bytes() == before


def test_write_profile_settings_refuses_broken_profile(tmp_path):
    path = pp.slot_profile_path(tmp_path, 0)
    _write(path, "{ not json")
    assert pp.write_profile_settings(tmp_path, 0, {"scale": 1.0}).status == "refused-corrupt"
    assert path.read_text(encoding="utf-8") == "{ not json"

    result = pp.write_profile_settings(tmp_path, 0, {"scale": 1.0},
                                      on_broken="backup-overwrite")
    assert result.status == "ok"
    assert json.loads(path.read_text(encoding="utf-8"))["scale"] == 1.0
    backups = list(tmp_path.glob("config-slot-0.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{ not json"


def test_concurrent_processes_write_distinct_profile_keys(tmp_path):
    """真子进程不同键并写同一份档案：锁内重读合并，谁的键都不许丢。"""
    keys = ["character", "scale", "click_sound_volume", "bubble_text_scale"]
    children = [_spawn("add", tmp_path, PP_KEY=key, PP_ITER=6) for key in keys]
    for child in children:
        child.wait_token("DONE")
        child.join()
    data = json.loads(pp.slot_profile_path(tmp_path, 0).read_text(encoding="utf-8"))
    for key in keys:
        assert key in data, key
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------- 档案写：几何 + 代次
def test_write_profile_geometry_accepts_position_and_facing(tmp_path):
    path = pp.slot_profile_path(tmp_path, 0)
    _dump(path, {"version": 4, "character": "cat"})
    result = pp.write_profile_geometry(tmp_path, 0, {
        "rx": 0.1, "ry": 0.2, "screen_name": "DP-1", "facing": "right",
        "scale": 1.5, "bogus": 9,
    })
    assert result.status == "ok"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["rx"] == 0.1 and data["ry"] == 0.2
    assert data["screen_name"] == "DP-1" and data["facing"] == "right"
    assert data["scale"] == 1.5
    assert "bogus" not in data
    assert data["character"] == "cat"


def test_write_profile_geometry_drops_illegal_values(tmp_path):
    path = pp.slot_profile_path(tmp_path, 0)
    result = pp.write_profile_geometry(tmp_path, 0, {"rx": 0.5, "scale": -1,
                                                    "facing": "sideways"})
    assert result.status == "ok"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["rx"] == 0.5
    assert "scale" not in data and "facing" not in data

    before = path.read_bytes()
    result = pp.write_profile_geometry(tmp_path, 0, {"rx": "abc", "scale": float("nan")})
    assert result.status == "invalid-geometry"
    assert path.read_bytes() == before


def test_write_profile_geometry_rejects_stale_generation(tmp_path):
    """代次防覆盖：reset/重建之后，迟到的几何写必须被拒绝（旧数据不动）。"""
    path = pp.slot_profile_path(tmp_path, 0)
    _dump(path, {"version": 4, "rx": 0.9, "ry": 0.9})
    generation = _begin(tmp_path, 0)
    assert generation == 1
    assert pp.write_profile_geometry(tmp_path, 0, {"rx": 0.1},
                                     generation=generation).status == "ok"
    before = path.read_bytes()
    stale = pp.write_profile_geometry(tmp_path, 0, {"rx": 0.5}, generation=0)
    assert stale.status == "stale-generation"
    assert path.read_bytes() == before
    # 不带代次的普通写不受代次门影响
    assert pp.write_profile_geometry(tmp_path, 0, {"rx": 0.5}).status == "ok"
    assert json.loads(path.read_text(encoding="utf-8"))["rx"] == 0.5


# ---------------------------------------------------------------- 本宠 reset
def _reset_fixture(config_dir: Path) -> Path:
    path = pp.slot_profile_path(config_dir, 0)
    _dump(path, {
        "version": 4, "rx": 0.11, "ry": 0.22, "screen_name": "DP-2",
        "facing": "right", "scale": 1.9, "character": "cat",
        "self_talk_enabled": True, "click_sound_enabled": False,
        "user_customized": True,
    })
    return path


def test_reset_keeps_identity_and_position_and_clears_preferences(tmp_path):
    path = _reset_fixture(tmp_path)
    result = pp.reset_slot_profile(tmp_path, 0)
    assert result.status == "ok"
    assert path.is_file(), "reset 保身份：档案文件（该槽地址）必须还在"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["rx"] == 0.11 and data["ry"] == 0.22
    assert data["screen_name"] == "DP-2"
    assert data["version"] == 4
    assert data["user_customized"] is True
    for key in ("facing", "scale", "character", "self_talk_enabled",
                "click_sound_enabled"):
        assert key not in data, f"偏好键必须被清掉（回同源默认）: {key}"
    assert set(result.kept) == {"rx", "ry", "screen_name"}
    assert set(result.cleared) == {"facing", "scale", "character",
                                   "self_talk_enabled", "click_sound_enabled"}
    assert result.generation == 1

    profile = pp.read_slot_profile(tmp_path, 0)
    assert profile.value("rx") == 0.11
    assert profile.value("facing") == _defaults()["facing"], "朝向属偏好，reset 回默认"
    assert profile.value("scale") == _defaults()["scale"]
    assert profile.value("character") == catalog.DEFAULT_CHARACTER


def test_reset_rejects_stale_generation(tmp_path):
    path = _reset_fixture(tmp_path)
    _begin(tmp_path, 0)
    before = path.read_bytes()
    result = pp.reset_slot_profile(tmp_path, 0, generation=0)
    assert result.status == "stale-generation"
    assert path.read_bytes() == before
    assert pp.slot_generation(tmp_path, 0) == 1, "被拒的 reset 不得推进代次"


def test_reset_without_profile_is_a_noop_that_still_invalidates_late_writes(tmp_path):
    result = pp.reset_slot_profile(tmp_path, 4)
    assert result.status == "no-profile"
    assert not pp.slot_profile_path(tmp_path, 4).exists(), "不得凭空造档案"
    assert result.generation == 1
    assert pp.slot_generation(tmp_path, 4) == 1


def test_reset_refuses_broken_profile(tmp_path):
    path = pp.slot_profile_path(tmp_path, 0)
    _write(path, "{ not json")
    result = pp.reset_slot_profile(tmp_path, 0)
    assert result.status == "refused-corrupt"
    assert path.read_text(encoding="utf-8") == "{ not json"
    assert pp.slot_generation(tmp_path, 0) == 0


# ---------------------------------------------------------------- 代次侧车
def test_generation_is_monotonic_persistent_and_per_slot(tmp_path):
    assert pp.generation_status(tmp_path) == "missing"
    assert pp.slot_generation(tmp_path, 5) == 0
    assert _begin(tmp_path, 5) == 1
    assert _begin(tmp_path, 5) == 2
    assert pp.slot_generation(tmp_path, 5) == 2
    assert _begin(tmp_path, 1) == 1
    assert pp.slot_generation(tmp_path, 5) == 2, "别的槽的代次不得被覆盖"
    assert pp.generation_status(tmp_path) == "ok"
    assert json.loads((tmp_path / pp.GENERATION_FILENAME).read_text(
        encoding="utf-8")) == {"version": 1, "generations": {"5": 2, "1": 1}}
    assert list(tmp_path.glob("*.tmp")) == []


def test_generation_broken_sidecar_is_not_treated_as_fresh(tmp_path):
    path = tmp_path / pp.GENERATION_FILENAME
    _write(path, "{ not json")
    assert pp.generation_status(tmp_path) == "corrupt"
    with pytest.raises(pp.ProfileStateError):
        pp.slot_generation(tmp_path, 0)
    result = pp.begin_slot_generation(tmp_path, 0)
    assert result.status == "refused-corrupt"
    assert path.read_text(encoding="utf-8") == "{ not json"


# ---------------------------------------------------------------- 失败态词表
def test_save_active_set_reports_lock_timeout_without_writing(tmp_path):
    """真进程持锁时：写类 API 返回 lock-timeout 并且一个字节都不写。"""
    path = pp.active_set_path(tmp_path)
    child, release = _holder(tmp_path)
    try:
        result = pp.save_active_set(tmp_path, [0, 1], timeout=0.5)
        assert result.status == "lock-timeout", result
        assert not path.exists(), "抢不到锁时绝不写盘"
    finally:
        _release(child, release)


def test_write_api_reports_lock_unavailable_when_primitive_missing(tmp_path, monkeypatch):
    """拿不到锁原语/打不开锁文件（只读盘、平台无 fcntl/msvcrt）也必须如实上报。

    这是 OS 边界失败：真机上无法稳定复现（要造只读盘），故按 AGENTS.md
    "只 mock 跑不动的操作系统边界"的口径注入。
    """
    def boom(_path):
        raise cwl.ConfigWriteError("no lock primitive here")

    monkeypatch.setattr(cwl, "_open_lock_fd", boom)
    result = pp.save_active_set(tmp_path, [0])
    assert result.status == "lock-unavailable", result
    assert not pp.active_set_path(tmp_path).exists()

    migration = pp.migrate_slot0_profile(tmp_path)
    assert migration.status == "lock-unavailable", migration


# ---------------------------------------------------------------- 审查修复批（P1–P9）
def test_reset_persists_generation_before_profile(tmp_path, monkeypatch):
    """P1：代次必须先落盘、档案随后写。

    反过来（先写档案后代次）时，第二次 replace 失败会留下"旧代次 + 已重置档案"：
    迟到几何写带着**仍然有效**的旧代次就被错误接受，reset 被静默抵消。故障注入点 =
    档案那次 replace；断言的状态是"代次已推进、档案未动"（partial），并且旧代次的
    迟到几何写必须被拒。
    """
    path = _reset_fixture(tmp_path)
    original = path.read_bytes()
    real_replace = cwl.atomic_replace_with_retry

    def fail_profile_write(temp, dest, attempts=5):
        if str(dest).endswith("config-slot-0.json"):
            raise OSError("profile write interrupted")
        return real_replace(temp, dest)

    monkeypatch.setattr(cwl, "atomic_replace_with_retry", fail_profile_write)
    result = pp.reset_slot_profile(tmp_path, 0)
    assert result.status == "partial", result
    assert result.generation == 1, "partial 必须给出已落盘的新代次，供调用方重同步"
    assert pp.slot_generation(tmp_path, 0) == 1
    assert path.read_bytes() == original, "档案那一步失败 → 档案内容未动（partial）"

    # 关键性质：迟到的旧代次几何写被拒（不会被写回 scale/facing）
    monkeypatch.undo()
    stale = pp.write_profile_geometry(tmp_path, 0, {"scale": 1.9, "facing": "right"},
                                      generation=0)
    assert stale.status == "stale-generation", stale
    assert path.read_bytes() == original

    # 用 partial 返回的新代次重跑即可收敛（幂等）
    retry = pp.reset_slot_profile(tmp_path, 0, generation=result.generation)
    assert retry.status == "ok", retry
    assert retry.generation == 2


def test_reset_generation_write_failure_changes_nothing(tmp_path, monkeypatch):
    """P1 另一半：代次那次 replace 失败 → 一个字节都没变，可以如实说"无变化"。"""
    _reset_fixture(tmp_path)
    real_replace = cwl.atomic_replace_with_retry

    def fail_generation_write(temp, dest, attempts=5):
        if str(dest).endswith(pp.GENERATION_FILENAME):
            raise OSError("generation write interrupted")
        return real_replace(temp, dest)

    monkeypatch.setattr(cwl, "atomic_replace_with_retry", fail_generation_write)
    result = pp.reset_slot_profile(tmp_path, 0)
    assert result.status == "write-failed", result
    assert result.generation is None
    assert pp.slot_generation(tmp_path, 0) == 0, "代次未推进"
    assert pp.slot_profile_path(tmp_path, 0).is_file(), "档案仍在（未被重置）"
    assert pp.read_slot_profile(tmp_path, 0).value("character") == "cat"


@pytest.mark.parametrize("text", [
    '{"version": 1, "generations": {"0": true}}',
    '{"version": 1, "generations": {"0": 1.0}}',
    '{"version": 1, "generations": {"abc": 1}}',
    '{"version": 1, "generations": {"01": 1}}',
    '{"version": 1, "generations": {"+1": 1}}',
    '{"version": 1, "generations": {" 1": 1}}',
    '{"version": 1, "generations": {"-1": 1}}',
    '{"version": 1, "generations": {"129": 1}}',
    '{"version": 1, "generations": {"0": -1}}',
    '{"version": 1, "generations": {"0": "1"}}',
    '{"version": 1, "generations": []}',
    '{"version": 1}',
    '{"version": 2, "generations": {}}',
    '{"generations": {"0": 1}}',
])
def test_generation_table_with_any_invalid_item_is_wholly_corrupt(tmp_path, text):
    """P2：代次表只有"整表可信"或"整表不可信"，**不许**跳过非法项当 0。

    跳过 = 把那个槽当成全新槽 → 迟到的旧代次写被错误接受（保护静默失效）。
    严格口径：值必须是 int（bool/float/字符串都不行）、键必须是规范十进制且落在
    0..128（"01"/"+1"/" 1" 这类非规范写法同样算坏）。
    """
    path = tmp_path / pp.GENERATION_FILENAME
    _write(path, text)
    assert pp.generation_status(tmp_path) == "corrupt"
    with pytest.raises(pp.ProfileStateError):
        pp.slot_generation(tmp_path, 0)
    assert pp.begin_slot_generation(tmp_path, 0).status == "refused-corrupt"
    assert pp.write_profile_geometry(tmp_path, 0, {"rx": 0.5},
                                     generation=0).status == "refused-corrupt"
    assert path.read_text(encoding="utf-8") == text, "损坏表不得被改写"


def test_generation_table_accepts_canonical_entries_and_zero_for_absent(tmp_path):
    _write(tmp_path / pp.GENERATION_FILENAME,
           '{"version": 1, "generations": {"0": 3, "128": 1}}')
    assert pp.generation_status(tmp_path) == "ok"
    assert pp.slot_generation(tmp_path, 0) == 3
    assert pp.slot_generation(tmp_path, 128) == 1
    assert pp.slot_generation(tmp_path, 7) == 0, "缺项的新槽才按 0"


def test_snapshot_binds_profile_and_generation_in_one_locked_read(tmp_path):
    """P4：快照（档案 + 代次）必须锁内一次读出来当 token。

    调用方不许"保存前再新读一次代次"——那样读到的是另一代的代次，门就形同虚设。
    """
    _dump(pp.slot_profile_path(tmp_path, 0), {"version": 4, "scale": 0.7})
    assert pp.begin_slot_generation(tmp_path, 0).status == "ok"

    snapshot = pp.read_slot_snapshot(tmp_path, 0)
    assert snapshot.status == "ok", snapshot
    assert snapshot.slot == 0
    assert snapshot.generation == 1
    assert snapshot.profile.value("scale") == 0.7
    assert pp.write_profile_geometry(tmp_path, 0, {"rx": 0.1},
                                     generation=snapshot.generation).status == "ok"


def test_snapshot_reports_corrupt_without_handing_out_usable_token(tmp_path):
    _dump(pp.slot_profile_path(tmp_path, 0), {"version": 4, "scale": 0.7})
    _write(tmp_path / pp.GENERATION_FILENAME, "{ not json")
    snapshot = pp.read_slot_snapshot(tmp_path, 0)
    assert snapshot.status == "corrupt", snapshot
    assert snapshot.generation is None, "不可信时不许给可用 token（给 0 就会放行旧写）"
    assert snapshot.reason

    _write(tmp_path / pp.GENERATION_FILENAME, '{"version": 1, "generations": {}}')
    _write(pp.slot_profile_path(tmp_path, 0), "{ not json")
    snapshot = pp.read_slot_snapshot(tmp_path, 0)
    assert snapshot.status == "corrupt" and snapshot.generation is None


def test_snapshot_takes_the_lock_and_bounds_the_wait(tmp_path):
    """快照必须真的进锁：别人持锁时它不能读到"锁外的拼凑视图"。"""
    child, release = _holder(tmp_path)
    try:
        snapshot = pp.read_slot_snapshot(tmp_path, 0, timeout=0.5)
        assert snapshot.status == "lock-timeout", snapshot
        assert snapshot.generation is None
    finally:
        _release(child, release)


@pytest.mark.parametrize("target", ["active", "profile"])
def test_invalid_utf8_bytes_are_corrupt_not_an_exception(tmp_path, target):
    """P5：读盘遇到非法 UTF-8 必须归入 corrupt，不许 UnicodeDecodeError 逃逸。"""
    if target == "active":
        path = pp.active_set_path(tmp_path)
        path.write_bytes(b'{"version": 2, "slots": [0]}\xff\xfe')
        assert pp.load_active_set(tmp_path).status == "corrupt"
        before = path.read_bytes()
        assert pp.save_active_set(tmp_path, [0]).status == "refused-corrupt"
    else:
        path = pp.slot_profile_path(tmp_path, 0)
        path.write_bytes(b'{"version": 4, "scale": 1.0}\xff')
        assert pp.read_slot_profile(tmp_path, 0).status == "corrupt"
        before = path.read_bytes()
        assert pp.write_profile_settings(
            tmp_path, 0, {"scale": 1.1}).status == "refused-corrupt"
    assert path.read_bytes() == before, "坏文件不得被改写"


def test_generation_file_with_invalid_utf8_is_corrupt(tmp_path):
    (tmp_path / pp.GENERATION_FILENAME).write_bytes(b'{"version": 1}\xff')
    assert pp.generation_status(tmp_path) == "corrupt"
    with pytest.raises(pp.ProfileStateError):
        pp.slot_generation(tmp_path, 0)


def test_save_active_set_refuses_to_upgrade_v1_without_the_backup_path(tmp_path):
    """P6：v1 文件上直接写 v2 = 绕过迁移备份，默认必须拒绝（先 migrate）。"""
    path = pp.active_set_path(tmp_path)
    legacy = json.dumps({"version": 1, "slots": [1, 2]}, indent=2)
    _write(path, legacy)
    result = pp.save_active_set(tmp_path, [0, 1])
    assert result.status == "refused-legacy-v1", result
    assert path.read_text(encoding="utf-8") == legacy

    result = pp.save_active_set(tmp_path, [0, 1], on_broken="backup-overwrite")
    assert result.status == "ok", result
    assert pp.load_active_set(tmp_path).slots == (0, 1)
    backup = path.with_name(f"{path.name}.v1.bak")
    assert backup.read_text(encoding="utf-8") == legacy, "显式升级也要先备份"


def test_single_key_default_does_not_build_the_whole_table(tmp_path, monkeypatch):
    """P9：单键默认只拷那一个键；不许每次调用都走"全表 deepcopy"。"""
    def boom():
        raise AssertionError("单键读取不得调用 per_pet_defaults()")

    monkeypatch.setattr(pp, "per_pet_defaults", boom)
    config_defaults = _defaults()
    assert pp.same_source_default("scale") == config_defaults["scale"]

    texts = pp.same_source_default("self_talk_texts")
    assert texts == config_defaults["self_talk_texts"]
    texts.append("mutated")
    assert pp.same_source_default("self_talk_texts") == config_defaults["self_talk_texts"]

    profile = pp.SlotProfile(path=tmp_path / "config-slot-0.json", exists=False,
                             status="missing", data={})
    assert profile.value("character") == config_defaults["character"]
    assert profile.value("pet_opacity", 33) == 33
