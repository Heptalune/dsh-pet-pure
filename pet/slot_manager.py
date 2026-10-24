# -*- coding: utf-8 -*-
"""桌宠槽位身份、配置播种与旧 spawn 迁移。

4.4b（PHASE4_DESIGN §3 T6）：多进程多宠退役层删除后，本模块**不再承载**
跨进程 slot 文件锁（``acquire_pet_slot`` / ``acquire_file_lock`` /
``_try_acquire_slot_lock`` / 定长 PID 记录 / ``SlotLockError`` /
``SlotManagerError``）——多宠身份改由 overlay 拓扑的 D6 无锁分配器
（``overlay_spawn_state.allocate_slot``）与进程内窗身份承担。

保留面（T6 明确「不能整删」）：

- ``backup_corrupt_config``：``Config._load`` 的核心依赖（损坏配置备份）；
- ``seed_slot_config_from_main`` / ``get_config_path_for_slot`` /
  ``get_sessions_dir_for_slot``：新身份落种（首启迁移仍需跑）；
- ``migrate_legacy_spawns``：旧 ``config-spawn*.json`` → ``config-slot-N.json``
  一次性原子迁移；
- ``slot_to_instance_id``：slot 编号 ↔ 配置身份（overlay 壳 D13 复用）；
- **runtime 标记 API**（``write/delete/read_live_instances`` 等）：D7 设置进程
  避让几何通道，overlay 壳与独立设置进程共用，不属退役层。

纯 Python 实现，不依赖 Qt。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import catalog
from .config import _bool_or_default, atomic_replace_with_retry


def _atomic_write_text(path: Path, text: str) -> None:
    """temp + ``os.replace`` 原子写入（缺陷 18）。失败上抛 OSError，由调用方处理。

    本文件的两处写盘（slot 落种 / runtime 标记）此前都是 ``path.write_text``
    直写：读者（主进程 Config._load、独立设置进程 read_live_instances）会在
    写盘中途读到半截 JSON。临时名带 PID 且以 ``.tmp`` 结尾（不匹配
    ``runtime-*.json`` / ``pet-runtime-v2-*.json`` 两个 glob，不会被读侧当成
    标记）；替换走 config 的退避重试（骑过 Windows 读句柄造成的瞬时共享
    冲突），任何出口都清掉临时文件。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        atomic_replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def slot_to_instance_id(slot_id: int) -> str:
    """slot-0 映射为空 instance_id（主实例），slot-N 映射为 'slot-N'。"""
    return "" if slot_id == 0 else f"slot-{slot_id}"


def get_config_path_for_slot(config_dir: Path | str, slot_id: int) -> Path:
    """获取槽位对应的配置文件路径。"""
    config_path = Path(config_dir)
    return config_path / "config.json" if slot_id == 0 else config_path / f"config-slot-{slot_id}.json"


# 新 slot 落种/刷新时剔除的每窗状态键（位置/朝向不继承，其余设置跟随主配置）。
# 批 C：落种/刷新永不写位置键（位置由各子肥鱼拖动后自存自管，生成逻辑不碰）。
_SEED_EXCLUDE_KEYS = ("rx", "ry", "screen_name", "facing")


def seed_slot_config_from_main(config_dir: Path | str, slot_id: int) -> bool:
    """新 slot 的初始配置跟随主设置；对目标 slot 三分支（批 C）：

    - slot 配置文件不存在 → 按当前主设置落种（含 spawn_inherit_size /
      spawn_scale / spawn_inherit_dynamic_island 逻辑）；
    - slot 存在但 ``user_customized`` 为假（含旧存档无此键）→ 按当前主设置
      重新刷新一遍（仍保留该 slot 自己拖动后自存的位置键）；
    - slot 存在且 ``user_customized`` 为真 → 整个跳过，一个键都不碰。

    落种/刷新**永不写位置键**（_SEED_EXCLUDE_KEYS）：位置由各子肥鱼拖动后
    自存自管，生成逻辑不碰——同时根治"原来位置的设置被顶掉"。

    用户反馈：多开出的新桌宠从零默认设置起步不合理，应跟随主设置。
    返回 True 表示落种/刷新成功；False 表示跳过（slot 0 / 已自定义 / 读取失败）。
    """
    if not slot_id:
        return False
    config_dir = Path(config_dir)
    slot_path = get_config_path_for_slot(config_dir, slot_id)
    main_path = config_dir / "config.json"
    # 已自定义的 slot 不碰；刷新时先保留其自存位置键（旧存档无 user_customized
    # 一律按假处理 -> 刷新跟随主设置）。
    if slot_path.exists():
        try:
            existing = json.loads(slot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        if _bool_or_default(existing.get("user_customized"), False):
            return False  # 用户在该子肥鱼自己的设置界面保存过 -> 整个跳过
        existing_position = {k: existing.get(k) for k in _SEED_EXCLUDE_KEYS}
    else:
        existing_position = {}
    try:
        data = json.loads(main_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    seed = copy.deepcopy(data)
    seed["version"] = 4
    # 副槽不继承主桌宠的位置/屏幕，避免新鱼叠在旧鱼身上；自启仍仅主槽。
    seed["autostart_wanted"] = False
    seed["harness_autostart"] = False
    # 生小肥鱼大小策略：开启继承 → 保留主配置 scale；
    # 关闭继承 → 用主配置里给“小肥鱼”单独选择的 spawn_scale。
    inherit_size = _bool_or_default(seed.get("spawn_inherit_size"), True)
    seed["spawn_inherit_size"] = inherit_size
    if not inherit_size:
        try:
            seed["scale"] = float(seed.get("spawn_scale", catalog.DEFAULT_SCALE))
        except (TypeError, ValueError):
            seed["scale"] = catalog.DEFAULT_SCALE
    # 生小肥鱼灵动岛策略：默认不继承 → 小肥鱼不开启自己的灵动岛；
    # 开启继承 → 保留主配置的 dynamic_island（含是否启用）。
    inherit_island = _bool_or_default(seed.get("spawn_inherit_dynamic_island"), False)
    seed["spawn_inherit_dynamic_island"] = inherit_island
    island = seed.get("dynamic_island")
    if isinstance(island, dict):
        island["enabled"] = bool(inherit_island)
    else:
        seed["dynamic_island"] = {"enabled": bool(inherit_island)}
    chat = seed.get("chat")
    if isinstance(chat, dict):
        providers = chat.get("providers")
        if isinstance(providers, dict):
            for provider in providers.values():
                if isinstance(provider, dict):
                    provider.pop("api_key", None)
                    provider.pop("vision_api_key", None)
    # 落种/刷新永不写位置键：剔除从主配置继承的位置键，再还原本 slot 自存的位置。
    for key in _SEED_EXCLUDE_KEYS:
        seed.pop(key, None)
    for key, value in existing_position.items():
        if value is not None:
            seed[key] = value
    seed["user_customized"] = False
    try:
        _atomic_write_text(slot_path, json.dumps(seed, ensure_ascii=False, indent=2))
    except OSError:
        return False
    return True


def get_sessions_dir_for_slot(config_dir: Path | str, slot_id: int) -> Path:
    """获取槽位对应的会话目录。"""
    config_path = Path(config_dir)
    return config_path / "sessions" if slot_id == 0 else config_path / f"sessions-slot-{slot_id}"


def backup_corrupt_config(config_file: Path) -> Path | None:
    """损坏配置备份：使用毫秒时间戳加 PID 后缀，连续恢复不覆盖旧备份。"""
    if not config_file.is_file():
        return None
    timestamp = int(time.time() * 1000)
    backup_file = config_file.with_name(f"{config_file.name}.corrupt-{timestamp}-{os.getpid()}")
    try:
        shutil.copy2(config_file, backup_file)
        return backup_file
    except Exception as exc:
        logging.error("备份损坏配置文件失败: %s -> %s (%s)", config_file, backup_file, exc)
        return None


def _recover_migration_staging(config_path: Path, staging_dir: Path) -> bool:
    """恢复或清理残留的 .migration_staging 目录。

    若上次迁移中途被杀，staging 内可能残留尚未移入目标或尚未回滚的文件。
    若 staging 内有文件，将它们按元数据/对应旧名字或就近还原。
    为保证幂等与安全：
    staging 中的 target 格式文件（如 config-slot-N.json），若目标路径不存在则移入目标路径；若目标已存在则忽略。
    清理完成后移除 staging 目录。
    4.4b：目标槽位文件锁已退役——「是否可落位」只看目标是否已存在。
    """
    if not staging_dir.is_dir():
        return True
    remaining = False
    for item in list(staging_dir.iterdir()):
        target = config_path / item.name
        try:
            if target.exists():
                logging.warning("恢复 staging 时目标已存在，保留残留: %s", item)
                remaining = True
            else:
                shutil.move(str(item), str(target))
        except Exception as exc:
            logging.warning("恢复 staging 文件失败: %s -> %s (%s)", item, target, exc)
            remaining = True
    if not remaining:
        try:
            staging_dir.rmdir()
        except OSError:
            remaining = True
    return not remaining


def migrate_legacy_spawns(config_dir: Path | str) -> bool:
    """将旧版 config-spawn*.json 和 sessions-spawn*/ 原子迁移到 slot-1, slot-2, ...。

    约束:
    1. 仅在确认没有运行中的旧 spawn 实例时进行（runtime 标记探活）；
    2. 按旧 config 的 mtime 升序稳定排序，依次映射到 slot-1, slot-2, ...；
    3. config 与对应 sessions 作为原子迁移单元，使用 staging 临时目录回滚；
    4. 目标槽位已有文件则跳过该槽位（保留原文件，记 warning）；
    5. config 已移到目标后 sessions 移动失败必须把目标 config 移回原路径（完整回滚）；
    6. 启动时检测到 .migration_staging 非空，先恢复或回滚上次中断的迁移；
    7. 完成后写入 migration-spawns.done 标记文件；
    8. 未迁移或无法配对的文件一律保留不删。
    """
    config_path = Path(config_dir)
    if not config_path.is_dir():
        return True

    staging_dir = config_path / ".migration_staging"
    if staging_dir.is_dir():
        if not _recover_migration_staging(config_path, staging_dir):
            return False

    marker_file = config_path / "migration-spawns.done"
    if marker_file.exists():
        return True

    # 查找所有旧 spawn 配置文件
    old_configs = list(config_path.glob("config-spawn*.json"))
    if not old_configs:
        try:
            marker_file.write_text("done\n", encoding="utf-8")
        except OSError:
            pass
        return True

    # 检查是否有旧实例正在运行（通过 runtime marker 探活；同时认旧名与
    # 批5.2 版本化新名，否则多进程模式的新标记匹配不到、迁移被误放行）
    for runtime_file in list_runtime_marker_files(config_path):
        try:
            data = json.loads(runtime_file.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid and isinstance(pid, int) and pid_alive(pid):
                logging.warning("检测到正在运行的桌宠进程 (PID: %s)，跳过旧 spawn 迁移", pid)
                return False
        except Exception:
            pass

    # 按 mtime 排序
    old_configs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)

    # 找到目标 slot-1, slot-2 等空闲槽位
    slot_idx = 1
    staging_dir.mkdir(parents=True, exist_ok=True)

    success_all = True
    for old_cfg in old_configs:
        spawn_name = old_cfg.stem.removeprefix("config-")
        old_sessions = config_path / f"sessions-{spawn_name}"

        # 校验 JSON 有效性
        try:
            _ = json.loads(old_cfg.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.warning("旧 spawn 配置损坏，跳过迁移: %s (%s)", old_cfg, exc)
            continue

        # 寻找下一个目标 slot 文件不存在的 slot ID
        target_cfg = None
        target_sessions = None
        while True:
            t_cfg = get_config_path_for_slot(config_path, slot_idx)
            t_sessions = get_sessions_dir_for_slot(config_path, slot_idx)
            if not t_cfg.exists() and not t_sessions.exists():
                target_cfg = t_cfg
                target_sessions = t_sessions
                break
            slot_idx += 1

        # 执行单元原子移动（先 staging 再 target）
        staged_cfg = staging_dir / target_cfg.name
        staged_sessions = staging_dir / target_sessions.name if old_sessions.is_dir() else None

        step = 0  # 0: 未动, 1: old->staging, 2: staged_cfg->target_cfg, 3: staged_sessions->target_sessions
        try:
            shutil.move(str(old_cfg), str(staged_cfg))
            if staged_sessions and old_sessions.is_dir():
                shutil.move(str(old_sessions), str(staged_sessions))
            step = 1

            shutil.move(str(staged_cfg), str(target_cfg))
            step = 2

            if staged_sessions and staged_sessions.exists():
                shutil.move(str(staged_sessions), str(target_sessions))
                step = 3

            slot_idx += 1
        except Exception as exc:
            logging.error("迁移单元失败: %s -> slot-%s (%s)", old_cfg, slot_idx, exc)
            # 完整回滚到迁移前状态
            try:
                # 无论在哪一步出错，若 target 或 staging 存在目标文件，统一撤回至 old 路径
                if target_cfg.exists():
                    shutil.move(str(target_cfg), str(old_cfg))
                elif staged_cfg.exists():
                    shutil.move(str(staged_cfg), str(old_cfg))

                if target_sessions.exists():
                    shutil.move(str(target_sessions), str(old_sessions))
                elif staged_sessions and staged_sessions.exists():
                    shutil.move(str(staged_sessions), str(old_sessions))
            except Exception as rollback_exc:
                logging.error("回滚迁移单元失败: %s (%s)", old_cfg, rollback_exc)
            success_all = False

    try:
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        pass

    if success_all:
        try:
            marker_file.write_text("done\n", encoding="utf-8")
        except OSError:
            pass

    return success_all


def pid_alive(pid: int) -> bool:
    """跨平台探活：Windows 用 OpenProcess + GetExitCodeProcess，其余用 kill(pid, 0)。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION（Vista+ 即可查退出码）
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            # 句柄打得开 ≠ 进程还活着：进程被 TerminateProcess 后，只要还有人
            # 持着它的句柄（如父进程的 Popen 尚未 reap），进程对象不会销毁，
            # OpenProcess 一路成功。只有退出码仍是 STILL_ACTIVE 才算存活。
            # 同 pet/harness_launcher.py::_windows_pid_alive 的既有判定口径。
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --- 批5.2 R4：runtime 标记格式版本化（多窗每窗一份，旧 glob 不匹配）--------
# 旧版（runtime-<pid>.json）用 glob('runtime-*.json') 读取；为避免新旧混跑时
# 旧版把新版标记也计入「存活实例」而虚高计数（多开位置避让被干扰），新版标记
# 改用不与 'runtime-*.json' 匹配的 pet-runtime-v2-<pid>-slot-<N>.json 前缀。
# 4.4b：本 API 是 D7 设置进程避让几何通道（overlay 壳写、独立设置进程读），
# 与多进程多宠退役层无关，保留。
_RUNTIME_V2_PREFIX = "pet-runtime-v2-"


def _slot_label(instance_id: str) -> str:
    """从 instance_id 提取 slot 标签（用于 runtime 标记文件名）。"""
    s = str(instance_id or "").strip()
    if not s or s == "slot-0":
        return "0"
    if s.startswith("slot-"):
        return s[len("slot-"):] or "0"
    return re.sub(r"[^A-Za-z0-9_-]", "_", s) or "0"


def runtime_marker_name(instance_id: str = "", *, versioned: bool = False) -> str:
    """返回某窗 runtime 标记文件名。versioned=False 用旧名
    runtime-<pid>.json（单窗时保持旧行为）；True 用版本化新名
    pet-runtime-v2-<pid>-slot-<N>.json（批5.2 多窗，规避旧 glob 匹配）。"""
    pid = os.getpid()
    if versioned:
        return f"{_RUNTIME_V2_PREFIX}{pid}-slot-{_slot_label(instance_id)}.json"
    return f"runtime-{pid}.json"


def runtime_marker_path(config_dir: Path | str, instance_id: str = "",
                        *, versioned: bool = False) -> Path:
    """返回某窗 runtime 标记的完整路径。"""
    return Path(config_dir) / runtime_marker_name(instance_id, versioned=versioned)


def write_runtime_marker(config_dir: Path | str, instance_id: str,
                         x: int, y: int, w: int, h: int,
                         *, versioned: bool = False) -> Path:
    """写入本窗 runtime 标记（旧格式仅主窗用；versioned 多窗用）。

    写版本化标记时顺手清掉同 pid 的旧格式标记，避免同进程混用重复计数。
    写入走 temp + 原子替换（缺陷 18）：设置进程的 read_live_instances 会在
    任意时刻读这些标记，直写会被它读到半截 JSON（旧行为据此把标记当陈旧删掉，
    活进程的避让几何凭空消失）。
    """
    path = runtime_marker_path(config_dir, instance_id, versioned=versioned)
    try:
        if versioned:
            legacy = Path(config_dir) / f"runtime-{os.getpid()}.json"
            try:
                if legacy.exists():
                    legacy.unlink()
            except OSError:
                pass
        _atomic_write_text(path, json.dumps({
            'pid': os.getpid(),
            'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h),
        }))
    except OSError:
        pass
    return path


def delete_runtime_marker(config_dir: Path | str, instance_id: str = "") -> None:
    """删除本窗 runtime 标记（「退出这只」必须显式删，否则活 pid 的陈旧
    标记永久虚增计数/避让错乱）。新旧两种命名都尝试删（跨格式迁移兜底）。"""
    for ver in (True, False):
        try:
            path = runtime_marker_path(config_dir, instance_id, versioned=ver)
            if path.exists():
                path.unlink()
        except OSError:
            pass


def list_runtime_marker_files(config_dir: Path | str) -> list[Path]:
    """列出 config 目录内全部 runtime 标记文件。

    同时认旧名 ``runtime-<pid>.json`` 与批5.2 版本化新名
    ``pet-runtime-v2-<pid>-slot-<N>.json``。
    """
    root = Path(config_dir)
    try:
        files = list(root.glob("runtime-*.json"))
        files.extend(root.glob(f"{_RUNTIME_V2_PREFIX}*.json"))
        return files
    except OSError:
        return []


def read_live_instances(
    config_dir: Path | str,
    *,
    exclude_pid: int | None = None,
    exclude_markers=None,
    pid_alive_fn=None,
) -> list[tuple[int, int, int, int, int]]:
    """读取目录内 runtime 标记，返回存活实例 (pid, x, y, w, h) 列表。

    同时认旧（runtime-<pid>.json）与新（pet-runtime-v2-*）两种命名
    （避让定位兼容新旧混跑）。**pid 已确认死亡的**标记顺手删除（避免越积越多）；
    exclude_markers（本窗自己的标记路径/文件名）跳过且保留——同 pid 多窗下不能
    再用 exclude_pid 这种按 pid 过滤的方式（会把同进程所有窗都排除）。
    pid_alive_fn 可注入（测试用）。

    缺陷 18：解析失败（半写文件）或字段非法（判不出 pid）的标记**只跳过、不删**
    ——写侧此刻可能正写到一半，删掉等于把活进程的避让标记清掉；只有确认 pid
    已死的标记才回收。
    """
    alive = pid_alive_fn if pid_alive_fn is not None else pid_alive
    try:
        exclude_names = {Path(m).name for m in (exclude_markers or ())}
    except (TypeError, OSError):
        exclude_names = set()
    instances: list[tuple[int, int, int, int, int]] = []
    try:
        files = list(Path(config_dir).glob('runtime-*.json'))
        files.extend(Path(config_dir).glob(f'{_RUNTIME_V2_PREFIX}*.json'))
    except OSError:
        return instances
    for f in files:
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue  # 半写/损坏：判不出 pid，绝不删（可能是活进程正在写的文件）
        try:
            pid = int(data.get('pid', 0))
        except (AttributeError, TypeError, ValueError):
            continue  # 非对象 / pid 类型非法：同样只跳过不删
        if exclude_pid is not None and pid == exclude_pid:
            continue
        if f.name in exclude_names:
            continue
        if not alive(pid):
            # pid 已确认死亡：唯一的删除时机（陈旧标记不再虚增避让计数）。
            try:
                f.unlink()
            except OSError:
                pass
            continue
        try:
            x, y, w, h = (int(data.get(k, 0)) for k in ('x', 'y', 'w', 'h'))
        except (TypeError, ValueError):
            continue  # 进程还活着但几何非法：跳过并保留（下次写盘自然刷新）
        instances.append((pid, x, y, w, h))
    return instances
