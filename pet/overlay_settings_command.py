# -*- coding: utf-8 -*-
"""D12：独立设置进程 → 主进程的「退出子肥鱼」指令通道（零 Qt 纯逻辑）。

设计稿：``.scratch/single-overlay-window/PHASE4_DESIGN.md``（§4 D12 / §5 4.2c）。

问题：子肥鱼是**进程内 sprite**，独立设置进程（``--settings``，
settings.lock 单实例）里点「一键退出子肥鱼」无法跨进程删 sprite；已退役的
旧回退链（runtime 标记/slot 锁 + taskkill 子进程）在进程内形态下找不到任何
目标，按钮必然静默失效。

机制：设置进程把一条指令原子写进配置目录的运行时状态文件，主进程（overlay
壳 / legacy AppShell）经 config 目录 watcher + 定时轮询消费，命中后执行各自的
``clear_spawned_pets`` / ``exit_pet``。4.4b 起**两个拓扑共用这一条通道**（多进程
多宠退役层删除后不再有跨进程子宠；见 modern_settings_dialog._on_clear_spawned_pets）。

第二条指令 ``CMD_APPLY_SETTINGS_RESET``（issue #220 M10）走同一条通道，语义是
「设置页刚把全部设置恢复默认，请做一次重置级运行态对齐」（= 位置回默认落位）：
位置类键 ``rx/ry`` 被重置成 null（没有位置记忆），运行期不跟着对齐的话，本次退出
``save_position()`` 会把重置前的旧位置写回磁盘、重置被静默抵消。

第三条指令 ``CMD_PROFILE_RESET``（G1b）是**每宠档案**（slot 作用域）的「恢复默认」
运行态同步：偏好/尺寸/角色按新值刷新，**位置保留**（本宠 reset 不搬家）。它与上面
那条不是同义反复——两者的位置语义相反，复用一个就会让"保位置"当场失效（见该常量
的注释）。

硬约束（本模块承载的纯逻辑部分）：

- **原子写**：临时文件 + ``os.replace``（命名与 ``overlay_spawn_state`` /
  ``Config.save`` 同惯例），读取方永远看到完整 JSON 或旧内容；
- **有界 pending**：单文件最多 32 条，满队列拒写；writer/read/claim/ack 使用
  同一目录锁。保留 version1 单条外观，多条附 pending 列表。兼容 consume 只移除
  本条；壳 claim 后锁外应用、按 id/claim 确认，失败移队尾最多三次；30s claim 租期
  防消费者中断永久占住条目。损坏/未知/陈旧条目丢弃并记录，读失败保留重试；
- **损坏/未知指令静默跳过**：返回 None + 一行日志，绝不抛给 GUI；
- **陈旧丢弃**：``issued_at`` 超过 ``DEFAULT_MAX_AGE_S`` 的指令视为"桌宠当时
  没在跑"，丢弃并留日志（否则设置页关掉桌宠后再开，会突然清空全部子肥鱼）；
- **身份**：``target`` 为 slot 编号 = 只退/只对齐那一只（``0`` 是合法身份：主宠的档案）；
  ``None`` = 全部子肥鱼。

拓扑门 ``is_overlay_topology`` 也收口在本模块：设置进程必须按拓扑分流，而
``pet/__main__.py --settings`` 禁止导入重量级模块（pet.app / overlay_shell，
会连带素材库/ffmpeg/托盘），所以拓扑读取的唯一实现放在这个零 Qt 模块，
``overlay_shell.is_overlay_topology`` 只做转发（对外入口名不变）。

拓扑输入有三个：

1. ``DSH_PET_PARENT_TOPOLOGY``：**拉起本设置进程的那个主进程的运行中拓扑**
   （值 ``overlay`` / ``legacy``，父进程在 ``AppShell._launch_settings_process``
   注入；见 :data:`ENV_PARENT_TOPOLOGY`）。它回答的是"我现在正在被谁编辑、那个
   进程在跑哪套渲染路径"，**优先于下面两条**——设置子进程必须按运行中的那个主进程
   分流，不能按磁盘上"下一次启动的配置"（用户勾了捕获模式但没重启时，盘上是
   legacy、运行中的主进程仍是 overlay；按盘分流会把子宠页的设置写进
   ``config-slot-N.json``，而 overlay 主进程对非 per-slot 键只读主配置）；
2. ``PET_RENDER_TOPOLOGY`` 显式 env = dev 逃生门，**显式值优先**（测试与开发
   都要能压过用户配置）；
3. 主配置 ``config.json`` 的 ``stream_capture_mode``：勾选「直播捕获兼容」即
   **重启级切换**到 legacy 单窗 PetWindow 路径（overlay 单合成窗不给 OBS
   枚举的窗口）。**进程内快照一次**（T5：启动时定死，不做运行时切换）——
   否则用户在设置页勾上以后，正在跑的这一进程会中途翻拓扑。

父进程那条是**记录事实**不是做选择：它的值就是父进程自己（含上面第 2、3 条输入）
定下来的运行中判定，正常路径上三条不可能互相矛盾；父进程没发话（旧主进程 /
手工单开设置页）时判定链原样回退到第 2、3 条。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path

from . import config_write_lock as cwl

MAX_PENDING_COMMANDS = 32
MAX_COMMAND_ATTEMPTS = 3
CLAIM_LEASE_S = 30.0

# 配置目录口径复用 ``config`` 的既有解析（``app.main`` 的
# ``config_dir = _default_base() / APP_DIR_NAME`` 同源）。模块级导入是有意的：
# 拓扑门要在**测试里**能被钉到临时目录（conftest 的隔离夹具替换本模块的
# ``_default_base``），函数内 ``from .config import ...`` 会绕开这层注入。
# ``config`` 是零 Qt 轻模块，独立设置进程本来就 import 它（``Config``），
# 不会让本模块失去"两侧都能 import"的定位。
from .config import APP_DIR_NAME, _default_base

logger = logging.getLogger(__name__)

# T5：开发期一次性分流 flag（不进 Config/设置页/schema）。显式值优先于配置。
ENV_TOPOLOGY = "PET_RENDER_TOPOLOGY"
TOPOLOGY_OVERLAY = "overlay"
TOPOLOGY_LEGACY = "legacy"

#: 父进程（拉起本设置子进程的那个主进程）**运行中**拓扑的陈述：``overlay`` /
#: ``legacy``。与 ``PET_RENDER_TOPOLOGY`` 严格分开——后者是 dev 逃生门（开发者
#: 声明"本进程要跑哪套渲染路径"），本条是主进程告诉子进程"我现在正跑着哪一套"，
#: 记录事实而非选择，故在判定链里排在最前（见 :func:`is_overlay_topology`）。
#: 写入点唯一：``AppShell._launch_settings_process`` 拉起设置子进程时注入。
ENV_PARENT_TOPOLOGY = "DSH_PET_PARENT_TOPOLOGY"

#: T4：持久键 ``stream_capture_mode`` 也是拓扑输入（勾选 = 重启级切到 legacy
#: 单窗路径，OBS 才能枚举到窗口）。键名与 ``config.py`` 的 schema 同字面量。
STREAM_CAPTURE_KEY = "stream_capture_mode"

#: 拓扑判定的配置快照（T5：进程内只读一次）。按**配置文件路径**分键：生产进程
#: 只有 ``<config_dir>/config.json`` 一条路径（等于"进程内快照一次"），测试用
#: 临时目录各自独立、互不污染。清空见 ``reset_topology_snapshot``。
_CAPTURE_SNAPSHOT: dict[Path, bool] = {}

# 指令文件：运行时状态文件，刻意不带 config- 前缀——既不进旧 slot 配置 glob
# （agent_link/config 迁移），也不与 runtime 标记 glob 相撞。与
# overlay-active-pets.json 平级。
COMMAND_FILENAME = "overlay-settings-command.json"
COMMAND_VERSION = 1

# 已知指令：设置页「一键退出子肥鱼」（全部子肥鱼 / 指定 slot 身份）、
# 「恢复默认设置」后的运行态对齐（issue #220 M10：位置回默认落位等，
# 见 pet/settings_reset_defaults.py）、以及 G1b 新增的**每宠档案恢复默认**
# （``profile_reset``：偏好/尺寸/角色的运行态同步，**保留位置**）。
CMD_EXIT_SPAWNED_PETS = "exit_spawned_pets"
CMD_APPLY_SETTINGS_RESET = "apply_settings_reset"
#: G1b：**每宠档案**（slot 作用域）的「恢复默认」运行态同步。
#:
#: 为什么必须是**独立**指令而不是复用 ``apply_settings_reset``：后者的语义就是
#: "位置回默认落位"（消费者按它重摆窗口），而本宠 reset 的口径是**保留位置**
#: （``docs/PRODUCT-NEXT-STEPS-SPEC-2026-10-09.md`` §五：位置不属"恢复默认"，"回到
#: 默认落位"是独立操作）。复用会当场把这只宠搬走，与档案里保留下来的位置键矛盾；
#: 给旧指令加 ``preserve_position`` 之类的开关同样不行——过渡期在跑的老消费者会
#: **忽略**未知字段而照旧重定位（最坏情况：用户没要求却被搬家）。
#:
#: OverlayShell 已接子 slot；slot0 明确失败结束而非无限阻塞队列。
#: AppShell 的档案 reset 消费仍属后续整合，不把新命令误当旧 reset。
CMD_PROFILE_RESET = "profile_reset"
KNOWN_COMMANDS = frozenset(
    {CMD_EXIT_SPAWNED_PETS, CMD_APPLY_SETTINGS_RESET, CMD_PROFILE_RESET})


# 指令保鲜窗口：超过即视为"写下时主进程不在跑"。
DEFAULT_MAX_AGE_S = 120.0

_SLOT_INSTANCE_RE = re.compile(r"^slot-(\d+)$")


def _topology_env_verdict(name: str) -> bool | None:
    """拓扑 env 的判定：``legacy`` → 非 overlay；其它非空值 → overlay。

    未设置 / 空白 → None（不是判定，只表示"这条 env 没有发话"）。大小写与首尾空白
    不敏感。父进程陈述与 dev 逃生门**共用这一份解析**：两条通道的取值口径必须一致，
    否则"同一个值在两个进程里判出相反结果"。
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    return raw != TOPOLOGY_LEGACY


def _explicit_env_verdict() -> bool | None:
    """显式 env 逃生门的判定（``PET_RENDER_TOPOLOGY``）。"""
    return _topology_env_verdict(ENV_TOPOLOGY)


def _parent_env_verdict() -> bool | None:
    """父进程运行中拓扑的陈述（``DSH_PET_PARENT_TOPOLOGY``）；没发话 → None。"""
    return _topology_env_verdict(ENV_PARENT_TOPOLOGY)


def env_forces_legacy() -> bool:
    """显式 ``PET_RENDER_TOPOLOGY=legacy``？（dev 逃生门，运行期即时切换）。

    设置页文案要用它区分「本进程就是 legacy，开关即时生效」与「默认路径：开关
    是重启级拓扑输入」（见 ``settings_pet_controls.stream_capture_hint``）。父进程
    陈述不参与这条——它说的是"运行中拓扑是什么"，不是"捕获开关是否即时生效"。
    """
    return _explicit_env_verdict() is False


def topology_config_path() -> Path:
    """主配置 ``config.json`` 的路径（复用 config 的既有目录解析，不另拼一份）。

    ``app.main`` 的 ``config_dir = _default_base() / APP_DIR_NAME`` 是同一口径。
    取**主配置**而不是当前 instance 的槽位配置：捕获模式是进程级（重启后整个
    进程只起主宠物）的重启级开关，槽位配置里的同名键不参与拓扑判定。
    """
    return _default_base() / APP_DIR_NAME / "config.json"


def _read_stream_capture_mode(path: Path) -> bool:
    """读 ``config.json`` 的捕获开关；文件缺失/损坏/不可读一律 False（overlay）。

    读配置绝不允许拖垮启动：设置目录可能是只读盘、被别的进程占住，或用户手改
    成半截 JSON。布尔清洗复用 ``config._bool_or_default``（手改 ``"true"`` /
    ``"1"`` / ``"on"`` 同样算开启），不另造一套解析。
    """
    from .config import _bool_or_default

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("拓扑门：捕获模式配置不可读，按未开启处理: %s", path,
                     exc_info=True)
        return False
    if not isinstance(data, dict):
        logger.warning("拓扑门：配置不是对象，捕获模式按未开启处理: %s", path)
        return False
    # 直播捕获模块未激活（功能页）：拓扑判定按未开启处理——开着捕获开关但
    # 停了模块的用户回落 overlay（模块停用是更外层的意图）。
    from . import feature_activation

    if not feature_activation.is_active("stream_capture"):
        return False
    return bool(_bool_or_default(data.get(STREAM_CAPTURE_KEY), False))


def stream_capture_mode_enabled(path: Path | str | None = None) -> bool:
    """主配置是否开启「直播捕获兼容」（True = 本进程应走 legacy 单窗）。

    ``path`` 缺省 = ``topology_config_path()``（生产唯一入参）；显式传入只为
    测试/诊断。首次读取即快照，之后同路径的判定不再回读磁盘（T5）。
    """
    target = Path(path) if path is not None else topology_config_path()
    cached = _CAPTURE_SNAPSHOT.get(target)
    if cached is None:
        cached = _read_stream_capture_mode(target)
        _CAPTURE_SNAPSHOT[target] = cached
    return cached


def reset_topology_snapshot() -> None:
    """清掉拓扑配置快照（仅测试/诊断用；生产进程启动后只读一次就定死）。"""
    _CAPTURE_SNAPSHOT.clear()


def is_overlay_topology() -> bool:
    """当前进程是否 overlay 拓扑（T5：唯一读取实现，启动时定死）。

    判定顺序：

    1. ``DSH_PET_PARENT_TOPOLOGY``（父进程运行中拓扑的陈述；只有设置子进程会收到）：
       优先于其余输入——这个设置页编辑的就是那个正在跑的进程，按它分流才不会出现
       "保存成功、运行期无效"；
    2. ``PET_RENDER_TOPOLOGY`` 显式值（dev 逃生门）；
    3. 否则读主配置的 ``stream_capture_mode``：开启 → legacy（捕获模式重启级
       切换，T4），关闭/读不到 → overlay（默认拓扑）。

    不做运行时切换：配置判定在首次读取时快照（``stream_capture_mode_enabled``），
    设置页勾选只影响**下一次启动**。父进程那条陈述同样由它的启动快照给出
    （:func:`runtime_topology_env_value`），故也不需要运行时重判。
    """
    parent = _parent_env_verdict()
    if parent is not None:
        return parent
    explicit = _explicit_env_verdict()
    if explicit is not None:
        return explicit
    return not stream_capture_mode_enabled()


def runtime_topology_env_value() -> str:
    """本进程作为**父进程**拉起设置子进程时要注入的 :data:`ENV_PARENT_TOPOLOGY` 值。

    = 本进程运行中的拓扑（同一个 :func:`is_overlay_topology`，不是第二份判据）：
    主进程启动路径已读过一次并快照定死，之后用户在设置页动捕获开关只改磁盘，这条
    陈述不变——正是"子进程按运行中的父进程分流"所需要的事实。
    """
    return TOPOLOGY_OVERLAY if is_overlay_topology() else TOPOLOGY_LEGACY


def capture_switch_is_restart_level() -> bool:
    """本进程的「重启级」判定：拓扑由主配置捕获开关造成？

    True = 本次启动的（legacy）单窗形态就是 ``config.json`` 的捕获开关造成的，
    所以那个键是**重启级**开关：运行期热切换（``PetWindow.refresh_pet_settings``
    → ``window_screen.set_stream_capture_mode``）不得执行，否则用户在设置页点一下
    就会把正在被 OBS 捕获的窗口当场变回 Tool 窗口（直播断源），与设置页
    「重启后生效」的文案自相矛盾。

    显式 ``PET_RENDER_TOPOLOGY=legacy``（dev 逃生门）下主配置捕获开关为假 →
    False → 保留原有的运行期即时切换语义（那是开发期分流，不是用户开关）。
    默认 overlay 进程同样读 False，但那边本来就没有 PetWindow 消费它。
    """
    return stream_capture_mode_enabled()


def effective_stream_capture_mode(window_value) -> bool:
    """窗口能力该用的捕获开关（主配置压过窗口自己那份配置）。

    ``PetWindow`` 读的是**自己那份** ``Config``：``--slot N`` 启动出来的窗读
    ``config-slot-N.json``（``window.py`` 的窗旗标构造）。而拓扑只看主配置，
    于是"主配置 true + slot 配置 false"会让窗口落在 legacy 单窗形态里却仍是
    Tool 窗口——OBS 依旧枚举不到，正是捕获模式要解决的症状。故主配置开启时
    以它为准（不改写 slot 配置，只影响本次窗口能力的有效值）；主配置关闭时
    按窗口自己那份配置（env 逃生门下仍是逐窗可切）。
    """
    if stream_capture_mode_enabled():
        return True
    return bool(window_value)


def slot_from_instance_id(instance_id) -> int | None:
    """``"slot-3"`` → 3；主身份/非法值 → None（纯逻辑，设置页分流用）。"""
    match = _SLOT_INSTANCE_RE.match(str(instance_id or "").strip())
    if match is None:
        return None
    return int(match.group(1))


def command_path(config_dir: Path | str) -> Path:
    """指令文件路径：``<config_dir>/overlay-settings-command.json``。"""
    return Path(config_dir) / COMMAND_FILENAME


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_target(target) -> int | None:
    """target 合法化：``None``（= 该指令的"全部"口径）或 **>= 0 的 int**；其它 → None。

    下界是 0（不是 1）：身份空间含 ``slot0``（主宠的**档案**身份），而每宠 reset 的指令
    必须能点名它——``write_command(target=0)`` 在旧口径下被当成非法值**直接拒写**，
    用户点了重置却没有任何动静。负数 / bool / 字符串 / 浮点仍然是非法值（类型不放宽）。
    """
    if target is None:
        return None
    if isinstance(target, bool) or not isinstance(target, int):
        return None
    return target if target >= 0 else None


def write_command(config_dir: Path | str, command: str, *, target=None,
                  now: float | None = None, issuer_pid: int | None = None) -> bool:
    """原子写一条指令（tmp + ``os.replace``）。返回是否落盘成功。

    写失败只返回 False + 留日志，不抛：调用方是设置页按钮回调，环境不可写
    （只读盘/目录被文件占住）不该炸掉设置窗口。
    """
    name = str(command or "")
    if name not in KNOWN_COMMANDS:
        logger.warning("指令通道：拒绝写入未知指令 %r", command)
        return False
    clean_target = _normalize_target(target)
    if target is not None and clean_target is None:
        logger.warning("指令通道：拒绝写入非法 target %r", target)
        return False
    path = command_path(config_dir)
    payload = {
        "version": COMMAND_VERSION,
        "command": name,
        "target": clean_target,
        "issued_at": time.time() if now is None else float(now),
        "issuer_pid": os.getpid() if issuer_pid is None else int(issuer_pid),
    }
    try:
        with cwl.config_write_lock(config_dir):
            entries = _load_entries(path, now=payload["issued_at"], max_age_s=DEFAULT_MAX_AGE_S)
            if entries is None:
                return False
            if len(entries) >= MAX_PENDING_COMMANDS:
                logger.warning("指令通道：队列已满（%s），拒绝写入，不覆盖旧指令", MAX_PENDING_COMMANDS)
                return False
            payload["id"] = uuid.uuid4().hex
            payload["attempts"] = 0
            entries.append(payload)
            _store_entries(path, entries)
    except (OSError, cwl.ConfigWriteError):
        logger.warning("指令通道：写指令文件失败: %s", path, exc_info=True)
        return False
    return True


def _read_text(path: Path) -> str | None:
    """读指令文件正文；缺失返回空串语义（None=读不到，调用方区分处理）。

    返回 ``""`` 表示"文件不存在"（正常常态），返回 None 表示读失败
    （占用/权限/编码）——两者对调用方的处置不同：前者静默，后者保留重试。
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeError:
        logger.warning("指令通道：指令编码损坏，已跳过: %s", path)
        return "{broken"
    except OSError:
        logger.warning("指令通道：指令文件不可读，留给下一轮重试: %s", path)
        return None


def _parse(raw: str, *, now: float, max_age_s: float) -> dict | None:
    """校验指令正文：损坏/未知/缺字段/陈旧一律 None + 日志（绝不抛）。"""
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("指令通道：指令文件损坏，已跳过")
        return None
    if not isinstance(data, dict):
        logger.warning("指令通道：指令不是对象，已跳过")
        return None
    if data.get("version") != COMMAND_VERSION:
        logger.warning("指令通道：指令版本不匹配 (%r)，已跳过", data.get("version"))
        return None
    name = data.get("command")
    if not isinstance(name, str) or name not in KNOWN_COMMANDS:
        logger.warning("指令通道：未知指令 %r，已跳过", name)
        return None
    issued_at = _finite(data.get("issued_at"))
    if issued_at is None:
        logger.warning("指令通道：指令缺少合法 issued_at，已跳过")
        return None
    if now - issued_at > float(max_age_s):
        logger.warning("指令通道：陈旧指令已丢弃 (%.1fs 前)",
                       now - issued_at)
        return None
    target = data.get("target")
    if target is not None and _normalize_target(target) is None:
        logger.warning("指令通道：指令 target 非法 (%r)，已跳过", target)
        return None
    return {
        "version": COMMAND_VERSION,
        "command": str(name),
        "target": _normalize_target(target),
        "issued_at": issued_at,
        "issuer_pid": data.get("issuer_pid"),
    }


def _load_entries(path, *, now, max_age_s):
    raw = _read_text(path)
    if raw is None:
        return None
    if not raw:
        return []
    try:
        document = json.loads(raw)
    except ValueError:
        logger.warning("指令通道：指令文件损坏，已跳过")
        return []
    source = document.get("pending", [document]) if isinstance(document, dict) else [document]
    if not isinstance(source, list) or len(source) > MAX_PENDING_COMMANDS:
        logger.warning("指令通道：pending 非法，已跳过")
        return []
    entries = []
    seen = set()
    for item in source:
        parsed = _parse(json.dumps(item), now=now, max_age_s=max_age_s)
        if parsed is None:
            continue
        identity = item.get("id") or uuid.uuid4().hex
        if not isinstance(identity, str) or identity in seen:
            logger.warning("指令通道：重复/非法 id，已跳过")
            continue
        seen.add(identity)
        parsed["id"] = identity
        attempts = item.get("attempts", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts < MAX_COMMAND_ATTEMPTS:
            logger.warning("指令通道：非法/耗尽 attempts，失败结束")
            continue
        parsed["attempts"] = attempts
        claim = item.get("claim")
        if isinstance(claim, str) and _finite(item.get("claimed_at")) is not None:
            parsed["claim"] = claim
            parsed["claimed_at"] = float(item["claimed_at"])
        entries.append(parsed)
    return entries


def _store_entries(path, entries):
    if not entries:
        path.unlink(missing_ok=True)
        return
    document = dict(entries[0])
    if len(entries) > 1 or "claim" in document:
        document["pending"] = entries
    cwl.atomic_write_json(path, document)


def _available(entry, now):
    return "claim" not in entry or now - entry["claimed_at"] >= CLAIM_LEASE_S


def read_command(config_dir: Path | str, *, now: float | None = None,
                 max_age_s: float = DEFAULT_MAX_AGE_S) -> dict | None:
    """锁内诊断读队头，不改变 pending 或 claim。"""
    current = time.time() if now is None else float(now)
    try:
        with cwl.config_write_lock(config_dir):
            entries = _load_entries(command_path(config_dir), now=current, max_age_s=max_age_s)
            return dict(entries[0]) if entries else None
    except (OSError, cwl.ConfigWriteError):
        logger.warning("指令通道：读指令失败，保留重试", exc_info=True)
        return None


def consume_command(config_dir: Path | str, *, now: float | None = None,
                    max_age_s: float = DEFAULT_MAX_AGE_S) -> dict | None:
    """兼容消费：锁内取一条并移除，只移除自己的条目，不碰后来的写入。"""
    current = time.time() if now is None else float(now)
    path = command_path(config_dir)
    try:
        with cwl.config_write_lock(config_dir):
            entries = _load_entries(path, now=current, max_age_s=max_age_s)
            if entries is None:
                return None
            index = next((i for i, entry in enumerate(entries) if _available(entry, current)), None)
            command = entries.pop(index) if index is not None else None
            _store_entries(path, entries)
            return command
    except (OSError, cwl.ConfigWriteError):
        logger.warning("指令通道：消费失败，保留重试", exc_info=True)
        return None


def claim_command(config_dir: Path | str, *, now: float | None = None,
                  max_age_s: float = DEFAULT_MAX_AGE_S) -> dict | None:
    """短锁内领取一条，30 秒租期支持消费者中断后重试；应用必须在锁外。"""
    current = time.time() if now is None else float(now)
    path = command_path(config_dir)
    try:
        with cwl.config_write_lock(config_dir):
            entries = _load_entries(path, now=current, max_age_s=max_age_s)
            if entries is None:
                return None
            command = next((entry for entry in entries if _available(entry, current)), None)
            if command is not None:
                command["claim"] = uuid.uuid4().hex
                command["claimed_at"] = current
            _store_entries(path, entries)
            return dict(command) if command is not None else None
    except (OSError, cwl.ConfigWriteError):
        logger.warning("指令通道：claim 失败，保留重试", exc_info=True)
        return None


def finish_command(config_dir: Path | str, command: dict, *, retry=False) -> bool:
    """只确认已领取的 id/claim；失败重试移队尾，最多三次，过期失败有日志。"""
    path = command_path(config_dir)
    try:
        with cwl.config_write_lock(config_dir):
            entries = _load_entries(path, now=time.time(), max_age_s=DEFAULT_MAX_AGE_S)
            if entries is None:
                return False
            index = next((i for i, entry in enumerate(entries)
                          if entry["id"] == command.get("id")
                          and entry.get("claim") == command.get("claim")), None)
            if index is None:
                return False
            entry = entries.pop(index)
            if retry:
                entry["attempts"] += 1
                entry.pop("claim", None)
                entry.pop("claimed_at", None)
                if entry["attempts"] < MAX_COMMAND_ATTEMPTS:
                    entries.append(entry)
                else:
                    logger.warning("指令通道：%s target=%s 重试耗尽，失败结束（未报告成功）",
                                   entry["command"], entry["target"])
            _store_entries(path, entries)
            return True
    except (OSError, cwl.ConfigWriteError):
        logger.warning("指令通道：确认失败，保留租期重试", exc_info=True)
        return False
