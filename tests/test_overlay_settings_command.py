# -*- coding: utf-8 -*-
"""D12 设置进程指令通道纯逻辑单测（零 Qt，offscreen 无关）。

覆盖 PHASE4_DESIGN §4 D12 的四条硬约束：
1. 原子写（tmp + os.replace，不留残留 tmp）；
2. 消费即删（读一次即摘除，重放不再执行；再次写入是"新指令"）；
3. 损坏/未知/缺字段/陈旧指令静默跳过（返回 None + 留日志），且同样摘除——
   否则坏文件会被 3s 轮询永远重放；
4. 指令身份：``target`` 为 slot 编号时只退那一只，裸指令 = 全部子肥鱼。

拓扑门（``is_overlay_topology``）也归本模块：设置进程必须按拓扑分流，但
``pet/__main__.py --settings`` 明确禁止导入重量级模块（pet.app/overlay_shell），
故拓扑读取实现落在零 Qt 模块，``overlay_shell.is_overlay_topology`` 转发。
拓扑输入两个：显式 ``PET_RENDER_TOPOLOGY``（dev 逃生门，优先）与主配置
``stream_capture_mode``（T4：勾选 = 重启级切到 legacy 单窗，进程内快照一次）。
"""
from __future__ import annotations

import json
import logging

import pytest

from pet import overlay_settings_command as cmd


def _now() -> float:
    return 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean_topology_snapshot():
    """每个用例从零快照开始（快照按配置路径分键，但真实目录路径会跨用例复用）。

    conftest 的 ``_isolate_topology_config`` 已做同样两件事（钉临时目录 + 清快照），
    这里再显式收一次：本文件的用例直接盯着拓扑门，快照残留会让"改盘不生效"这类
    断言假绿。
    """
    cmd.reset_topology_snapshot()
    yield
    cmd.reset_topology_snapshot()


def _pin_config(tmp_path, monkeypatch):
    """把拓扑门的配置目录钉到临时目录（不读开发机真实 config.json）。

    门的生产口径是 ``_default_base()/APP_DIR_NAME/config.json``（``config.py`` 的
    两个名字）；这里只替换目录解析这一层——与 conftest 的隔离夹具同一个 seam，
    文件读取逻辑本身不 mock。
    """
    monkeypatch.setattr(cmd, "_default_base", lambda: tmp_path)
    return cmd.topology_config_path()


# ---------------------------------------------------------------- 拓扑门
def test_is_overlay_topology_env_contract(tmp_path, monkeypatch):
    # T5 默认化：默认 overlay；legacy 为唯一逃生门（大小写/空白不敏感）。
    # 配置侧未开启时 env 是唯一输入（配置文件不存在，见 _pin_config）。
    _pin_config(tmp_path, monkeypatch)
    monkeypatch.delenv(cmd.ENV_TOPOLOGY, raising=False)
    assert cmd.is_overlay_topology() is True
    monkeypatch.setenv(cmd.ENV_TOPOLOGY, "overlay")
    assert cmd.is_overlay_topology() is True
    monkeypatch.setenv(cmd.ENV_TOPOLOGY, " OVERLAY ")
    assert cmd.is_overlay_topology() is True
    monkeypatch.setenv(cmd.ENV_TOPOLOGY, "legacy")
    assert cmd.is_overlay_topology() is False
    monkeypatch.setenv(cmd.ENV_TOPOLOGY, " LEGACY ")
    assert cmd.is_overlay_topology() is False
    monkeypatch.setenv(cmd.ENV_TOPOLOGY, "")
    assert cmd.is_overlay_topology() is True


def test_topology_follows_stream_capture_mode_in_config(tmp_path, monkeypatch):
    """T4 Phase A：config.json 勾选捕获模式 = 本进程走 legacy 单窗（重启级切换）。"""
    path = _pin_config(tmp_path, monkeypatch)
    monkeypatch.delenv(cmd.ENV_TOPOLOGY, raising=False)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(json.dumps({"stream_capture_mode": True}), encoding="utf-8")
    assert cmd.stream_capture_mode_enabled() is True
    assert cmd.is_overlay_topology() is False


@pytest.mark.parametrize("value", [True, "true", "1", " yes ", "on"])
def test_topology_accepts_user_edited_truthy_values(tmp_path, monkeypatch, value):
    """手改 config.json 的宽松布尔（``config._bool_or_default`` 同口径）也算开启。"""
    path = _pin_config(tmp_path, monkeypatch)
    monkeypatch.delenv(cmd.ENV_TOPOLOGY, raising=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stream_capture_mode": value}), encoding="utf-8")
    assert cmd.is_overlay_topology() is False


@pytest.mark.parametrize("kind", ["missing", "corrupt", "not-object", "unreadable"])
def test_topology_falls_back_to_overlay_on_bad_config(tmp_path, monkeypatch, kind):
    """缺文件/损坏 JSON/内容不是对象/根本读不了 → 一律 overlay，绝不抛给启动链。"""
    path = _pin_config(tmp_path, monkeypatch)
    monkeypatch.delenv(cmd.ENV_TOPOLOGY, raising=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "corrupt":
        path.write_text('{"stream_capture_mode": tru', encoding="utf-8")
    elif kind == "not-object":
        path.write_text("[1, 2, 3]", encoding="utf-8")
    elif kind == "unreadable":
        # 路径被目录占住：read_text 抛 IsADirectoryError（OSError 子类）
        path.mkdir()
    assert cmd.stream_capture_mode_enabled() is False
    assert cmd.is_overlay_topology() is True


def test_explicit_env_wins_over_stream_capture_config(tmp_path, monkeypatch):
    """显式 env 是 dev 逃生门：压过配置（否则开发者无法在勾选状态下跑 overlay）。"""
    path = _pin_config(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stream_capture_mode": True}), encoding="utf-8")

    monkeypatch.setenv(cmd.ENV_TOPOLOGY, "overlay")
    assert cmd.is_overlay_topology() is True
    monkeypatch.setenv(cmd.ENV_TOPOLOGY, "legacy")
    assert cmd.is_overlay_topology() is False


def test_topology_config_verdict_is_snapshotted_once(tmp_path, monkeypatch):
    """T5：配置判定进程内定死——设置页勾选后，正在跑的进程不得中途翻拓扑。"""
    path = _pin_config(tmp_path, monkeypatch)
    monkeypatch.delenv(cmd.ENV_TOPOLOGY, raising=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stream_capture_mode": False}), encoding="utf-8")
    assert cmd.is_overlay_topology() is True

    path.write_text(json.dumps({"stream_capture_mode": True}), encoding="utf-8")
    assert cmd.is_overlay_topology() is True, "盘上改动不得改变本进程已定的拓扑"

    cmd.reset_topology_snapshot()  # 等价于"下一次启动"
    assert cmd.is_overlay_topology() is False


def test_topology_config_path_reuses_app_config_location(monkeypatch):
    """路径解析就是 ``_default_base()/APP_DIR_NAME/config.json``，不另拼第二份。

    两个名字在本模块**模块级**从 ``config`` 复用（同一对象），测试可注入——这也是
    conftest 隔离夹具钉的那一层；本用例直接钉这两个名字验证组合结果。
    """
    from pathlib import Path

    monkeypatch.setattr(cmd, "_default_base", lambda: Path("/tmp/base"))
    monkeypatch.setattr(cmd, "APP_DIR_NAME", "dsh-pet-test")
    assert cmd.topology_config_path() == Path(
        "/tmp/base") / "dsh-pet-test" / "config.json"


# ---------------------------------------------------------------- 父进程运行中拓扑（R01）
def test_parent_running_topology_env_wins_over_disk_capture_config(tmp_path, monkeypatch):
    """父进程的运行中拓扑压过磁盘上的捕获开关（子进程按它分流）。

    R01 场景：用户勾上「直播捕获兼容」并保存但**没重启**（文案就是这么说的），盘上
    ``stream_capture_mode=True``；拉起这个设置子进程的主进程仍在 overlay 上跑。子进程
    若按盘判就是 legacy → 子宠页把全部设置写进 ``config-slot-N.json``，而运行中的
    overlay 主进程对非 per-slot 键只读主配置 ⇒"保存成功、实际无效"，还会污染下一次
    重启的配置身份。
    """
    path = _pin_config(tmp_path, monkeypatch)
    monkeypatch.delenv(cmd.ENV_TOPOLOGY, raising=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stream_capture_mode": True}), encoding="utf-8")

    monkeypatch.setenv(cmd.ENV_PARENT_TOPOLOGY, "overlay")
    assert cmd.is_overlay_topology() is True

    monkeypatch.setenv(cmd.ENV_PARENT_TOPOLOGY, " LEGACY ")
    assert cmd.is_overlay_topology() is False


def test_parent_topology_env_outranks_dev_escape_hatch(tmp_path, monkeypatch):
    """两条 env 同时在场且互相矛盾：**父进程运行中拓扑优先**（记录事实，不是选择）。

    父进程的这条陈述本来就是它按自己那套输入（含 dev 逃生门）定下来的结果，正常
    路径上两者不可能矛盾；这里钉的是矛盾时的取舍，免得判定链悄悄长出第二份判据。
    """
    _pin_config(tmp_path, monkeypatch)
    monkeypatch.setenv(cmd.ENV_TOPOLOGY, "overlay")
    monkeypatch.setenv(cmd.ENV_PARENT_TOPOLOGY, "legacy")
    assert cmd.is_overlay_topology() is False


def test_missing_parent_topology_env_falls_back_to_existing_inputs(tmp_path, monkeypatch):
    """父进程没发话（手工单开设置页/旧主进程）→ 判定逐位回到原有链路。"""
    path = _pin_config(tmp_path, monkeypatch)
    monkeypatch.delenv(cmd.ENV_PARENT_TOPOLOGY, raising=False)
    monkeypatch.delenv(cmd.ENV_TOPOLOGY, raising=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stream_capture_mode": True}), encoding="utf-8")

    assert cmd.is_overlay_topology() is False, "磁盘快照（原行为）"
    monkeypatch.setenv(cmd.ENV_TOPOLOGY, "overlay")
    assert cmd.is_overlay_topology() is True, "dev 逃生门（原行为）"
    monkeypatch.setenv(cmd.ENV_PARENT_TOPOLOGY, "")
    assert cmd.is_overlay_topology() is True, "空白 = 没发话，不参与判定"


def test_runtime_topology_env_value_reports_the_running_verdict(tmp_path, monkeypatch):
    """父进程侧：报给子进程的就是本进程运行中的拓扑（同一个判定，不是第二份判据）。

    "运行中"的含义由 T5 的快照保证：启动时读一次定死，之后用户改设置页只动磁盘，
    这条 env 陈述不变。
    """
    path = _pin_config(tmp_path, monkeypatch)
    monkeypatch.delenv(cmd.ENV_PARENT_TOPOLOGY, raising=False)
    monkeypatch.delenv(cmd.ENV_TOPOLOGY, raising=False)

    assert cmd.runtime_topology_env_value() == cmd.TOPOLOGY_OVERLAY, "空目录 = 默认 overlay"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stream_capture_mode": True}), encoding="utf-8")
    assert cmd.runtime_topology_env_value() == "overlay", "启动时定死的拓扑不随磁盘改"

    cmd.reset_topology_snapshot()  # 等价于"主进程重启"
    assert cmd.runtime_topology_env_value() == "legacy"


def test_slot_from_instance_id():
    assert cmd.slot_from_instance_id("slot-3") == 3
    assert cmd.slot_from_instance_id("slot-0") == 0
    assert cmd.slot_from_instance_id("") is None
    assert cmd.slot_from_instance_id(None) is None
    assert cmd.slot_from_instance_id("slot-x") is None
    assert cmd.slot_from_instance_id("3") is None


# ---------------------------------------------------------------- target 规范（G1b）
def test_target_zero_is_a_valid_slot_identity(tmp_path):
    """``target=0`` 是**合法**身份（slot0 的主宠档案），不是"非法值"。

    旧 ``_normalize_target`` 只认 ``>= 1``：主宠（slot0）的每宠 reset 写指令时被当成
    非法 target **直接拒写**（``write_command`` 返回 False），用户点了却没有任何动静；
    解析侧同样会把它当坏指令跳过。身份空间含 0 之后，这条上界必须跟着放开。
    """
    assert cmd.write_command(tmp_path, cmd.CMD_PROFILE_RESET, target=0, now=_now()) is True
    assert cmd.read_command(tmp_path, now=_now())["target"] == 0
    got = cmd.consume_command(tmp_path, now=_now())
    assert got is not None and got["target"] == 0
    assert not cmd.command_path(tmp_path).exists()


@pytest.mark.parametrize("bad", [-1, True, False, "0", 1.5, "slot-2"])
def test_invalid_targets_are_still_rejected(tmp_path, bad, caplog):
    """放开下界不等于放宽类型：负数/bool/字符串/浮点照旧拒写、拒解析。"""
    assert cmd.write_command(
        tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, target=bad, now=_now()) is False
    cmd.command_path(tmp_path).write_text(json.dumps(
        {"version": 1, "command": cmd.CMD_EXIT_SPAWNED_PETS,
         "issued_at": _now(), "target": bad}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert not cmd.command_path(tmp_path).exists()


def test_profile_reset_command_is_known_and_round_trips(tmp_path):
    """``profile_reset``（本宠 reset 的运行态同步）是一条已知指令，target 带 slot。"""
    assert cmd.CMD_PROFILE_RESET in cmd.KNOWN_COMMANDS
    assert cmd.write_command(tmp_path, cmd.CMD_PROFILE_RESET, target=2, now=_now()) is True

    got = cmd.consume_command(tmp_path, now=_now())

    assert got is not None and got["command"] == cmd.CMD_PROFILE_RESET
    assert got["target"] == 2


# ---------------------------------------------------------------- 写：原子
def test_write_command_is_atomic_and_leaves_no_temp(tmp_path):
    assert cmd.write_command(
        tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, now=_now(), issuer_pid=4242) is True
    path = cmd.command_path(tmp_path)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == cmd.COMMAND_VERSION
    assert data["command"] == cmd.CMD_EXIT_SPAWNED_PETS
    assert data["issued_at"] == _now()
    assert data["issuer_pid"] == 4242
    assert data["target"] is None
    assert list(tmp_path.glob("*.tmp")) == []          # tmp + replace，无残留


def test_write_command_creates_missing_dir(tmp_path):
    target_dir = tmp_path / "config"
    assert cmd.write_command(target_dir, cmd.CMD_EXIT_SPAWNED_PETS, now=_now()) is True
    assert cmd.command_path(target_dir).is_file()


def test_write_command_failure_returns_false(tmp_path):
    """目录位置被文件占住 = 环境错误：返回 False，不抛（调用方留日志）。"""
    blocked = tmp_path / "config"
    blocked.write_text("not a dir", encoding="utf-8")
    assert cmd.write_command(blocked, cmd.CMD_EXIT_SPAWNED_PETS, now=_now()) is False


# ---------------------------------------------------------------- 消费：即删 + 重放
def test_consume_returns_command_and_deletes_file(tmp_path):
    cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, now=_now())
    got = cmd.consume_command(tmp_path, now=_now())
    assert got is not None
    assert got["command"] == cmd.CMD_EXIT_SPAWNED_PETS
    assert got["target"] is None
    assert not cmd.command_path(tmp_path).exists()     # 消费即删
    assert cmd.consume_command(tmp_path, now=_now()) is None   # 不重放


def test_consume_target_slot_roundtrip(tmp_path):
    cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, target=2, now=_now())
    got = cmd.consume_command(tmp_path, now=_now())
    assert got["target"] == 2


def test_rewritten_command_after_consume_is_honored(tmp_path):
    """消费后再次写入 = 新指令（用户又点了一次按钮），不是被去重吞掉。"""
    cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, now=_now())
    assert cmd.consume_command(tmp_path, now=_now()) is not None
    cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, now=_now())
    assert cmd.consume_command(tmp_path, now=_now()) is not None


def test_read_command_is_non_destructive(tmp_path):
    cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, now=_now())
    assert cmd.read_command(tmp_path, now=_now()) is not None
    assert cmd.command_path(tmp_path).is_file()        # 诊断读不消费
    assert cmd.read_command(tmp_path, now=_now()) is not None


# ---------------------------------------------------------------- 消费：损坏/未知/陈旧
def test_missing_file_is_silent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert caplog.records == []                        # 轮询常态：不刷日志


def test_corrupt_command_is_skipped_consumed_and_logged(tmp_path, caplog):
    cmd.command_path(tmp_path).write_text("{ broken", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert not cmd.command_path(tmp_path).exists()     # 坏文件也要摘除（否则轮询重放）
    assert any("损坏" in r.getMessage() for r in caplog.records)


def test_unknown_command_is_skipped_consumed_and_logged(tmp_path, caplog):
    cmd.command_path(tmp_path).write_text(json.dumps(
        {"version": 1, "command": "format_c_drive", "issued_at": _now()}),
        encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert not cmd.command_path(tmp_path).exists()
    assert any("未知" in r.getMessage() for r in caplog.records)


def test_non_object_payload_is_skipped(tmp_path, caplog):
    cmd.command_path(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert not cmd.command_path(tmp_path).exists()


def test_version_mismatch_is_skipped(tmp_path, caplog):
    cmd.command_path(tmp_path).write_text(json.dumps(
        {"version": 99, "command": cmd.CMD_EXIT_SPAWNED_PETS, "issued_at": _now()}),
        encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert not cmd.command_path(tmp_path).exists()


def test_stale_command_is_dropped(tmp_path, caplog):
    """桌宠没在跑时写下的指令不允许在下次启动时突然清空子肥鱼。"""
    cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS,
                      now=_now() - cmd.DEFAULT_MAX_AGE_S - 1.0)
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert not cmd.command_path(tmp_path).exists()
    assert any("陈旧" in r.getMessage() for r in caplog.records)


def test_fresh_command_within_window_is_accepted(tmp_path):
    cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS,
                      now=_now() - cmd.DEFAULT_MAX_AGE_S + 1.0)
    assert cmd.consume_command(tmp_path, now=_now()) is not None


def test_invalid_issued_at_is_skipped(tmp_path, caplog):
    for bad in ({"version": 1, "command": cmd.CMD_EXIT_SPAWNED_PETS},
                {"version": 1, "command": cmd.CMD_EXIT_SPAWNED_PETS,
                 "issued_at": "yesterday"},
                {"version": 1, "command": cmd.CMD_EXIT_SPAWNED_PETS,
                 "issued_at": float("nan")}):
        cmd.command_path(tmp_path).write_text(json.dumps(bad), encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert cmd.consume_command(tmp_path, now=_now()) is None
        assert not cmd.command_path(tmp_path).exists()


def test_invalid_target_is_skipped(tmp_path, caplog):
    cmd.command_path(tmp_path).write_text(json.dumps(
        {"version": 1, "command": cmd.CMD_EXIT_SPAWNED_PETS,
         "issued_at": _now(), "target": "slot-2"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert not cmd.command_path(tmp_path).exists()


def test_unreadable_file_leaves_command_for_retry(tmp_path, monkeypatch, caplog):
    """读失败（权限/锁）时不摘除也不执行：下一轮轮询重试，绝不吞掉用户的点击。"""
    path = cmd.command_path(tmp_path)
    path.write_text("{}", encoding="utf-8")
    real_read = type(path).read_text

    def boom(self, *args, **kwargs):
        if self == path:
            raise OSError("被占用")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", boom)
    with caplog.at_level(logging.WARNING):
        assert cmd.consume_command(tmp_path, now=_now()) is None
    assert path.is_file()


def test_claim_lease_recovers_without_removing_other_pending(tmp_path):
    now = _now()
    assert cmd.write_command(tmp_path, cmd.CMD_PROFILE_RESET, target=1, now=now)
    first = cmd.claim_command(tmp_path, now=now)
    assert cmd.claim_command(tmp_path, now=now + 1) is None
    recovered = cmd.claim_command(tmp_path, now=now + cmd.CLAIM_LEASE_S)
    assert recovered["id"] == first["id"] and recovered["claim"] != first["claim"]


def test_same_slot_reset_does_not_replace_exit(tmp_path):
    for name in (cmd.CMD_PROFILE_RESET, cmd.CMD_EXIT_SPAWNED_PETS, cmd.CMD_PROFILE_RESET):
        assert cmd.write_command(tmp_path, name, target=1, now=_now())
    assert [cmd.consume_command(tmp_path, now=_now())["command"] for _ in range(3)] == [
        cmd.CMD_PROFILE_RESET, cmd.CMD_EXIT_SPAWNED_PETS, cmd.CMD_PROFILE_RESET]


@pytest.mark.parametrize("raw", [b"\xff", b'{"version":1,"command":[],"issued_at":1}'])
def test_corrupt_encoding_or_command_type_does_not_block_next_write(tmp_path, raw):
    cmd.command_path(tmp_path).write_bytes(raw)
    assert cmd.write_command(tmp_path, cmd.CMD_EXIT_SPAWNED_PETS, target=2, now=_now())
    assert cmd.consume_command(tmp_path, now=_now())["target"] == 2
