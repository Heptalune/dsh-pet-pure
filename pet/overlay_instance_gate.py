# -*- coding: utf-8 -*-
"""D4：桌宠进程单实例门（PHASE4_DESIGN §4 / §5 4.2c）。

问题（评审 GLM 发现）：overlay 拓扑裸退役 slot 锁后，「双击桌面宠物」的二次
启动语义没了——两个 overlay 进程各自恢复全部 sprite、位置回写互踩。本模块用
QLockFile（与 ``pet/__main__.py`` 的 ``settings.lock``、frameseq 的
``.converting.lock`` 同一套语义）做进程级互斥：第二个实例在**任何配置目录副作用
之前**（不抢 slot-1、不落种 config-slot-N.json）拒绝启动。

作用域（T4 Phase A 扩到两个拓扑）：**不再按拓扑分流**——默认 overlay 拓扑与
捕获模式的 legacy 单窗拓扑都抢同一把门。捕获模式下的桌宠也是完整实例（建
PetWindow、恢复位置、落盘配置），双开同样互踩，且第二条启动在用户看来就是"又
起了一只完整桌宠"，与「重启时多只桌宠先收拢为主宠物」的单窗口径直接冲突。锁名
仍叫 ``overlay-instance.lock``（历史名，跨版本稳定，改名只会留下孤儿锁文件），
语义已是"桌宠进程单实例门"。拓扑判定入口仍是 ``overlay_shell.is_overlay_topology``，
本模块不再消费它（见 ``tests/test_overlay_instance_gate.py`` 的双拓扑用例）。

崩溃残留（stale lock）完全交给 QLockFile 既有判定，不自己猜：
- 残留文件里的持有者 pid 已死（同主机）→ 立刻接管（实测 Qt 6.11.2：kill 掉
  持有进程后，新的 QLockFile ``tryLock(0)`` 直接成功）；
- 活着的持有进程绝不被抢锁（即使锁文件 mtime 已老）；
- 锁文件根本不存在（配置目录不可写之类的环境错误）→ 放行启动并留痕，口径同
  ``app.py::_settings_process_running`` 的「拿不到锁但锁文件不存在 = 环境错误」
  ——坏环境下桌宠必须还能起来，宁可少一层保护。

留痕：接管/拒绝/放行都写一行 logging（开发运行可见 stderr），并追加到
``<config_dir>/pet-overlay-gate.log``。打包产物是 pythonw 无控制台的 GUI 进程，
stderr 不存在，「双击了但没反应」必须留有落盘证据；该文件名落在既有
``pet-*.log*`` 清理命名空间内，随 ``_cleanup_old_pet_logs`` 的 7 天保留策略回收。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import QLockFile

logger = logging.getLogger(__name__)

# 锁文件名：进程级唯一，落在 config 目录（与 settings.lock 平级）。名字里的
# "overlay" 是历史（门最早只在 overlay 拓扑生效），语义已是"桌宠进程单实例"。
LOCK_NAME = "overlay-instance.lock"
# 留痕文件：pet-* 命名空间内，随既有 7 天日志清理回收。
TRACE_NAME = "pet-overlay-gate.log"
# QLockFile 的陈旧判定窗口（与 settings.lock 同值；判定还叠加 pid 存活检查）。
STALE_LOCK_MS = 30_000

# 抢门失败（= 已有桌宠实例在运行）时的退出码：0 = 静默退出，对齐
# pet/__main__.py 的 settings.lock 惯例（「已有设置进程持有 settings.lock，
# 本次退出」→ return 0）。双击桌面宠物场景下第二实例不该让外壳/父进程把它当
# 启动失败报错；留痕由日志与 pet-overlay-gate.log 承担。
DUPLICATE_EXIT_CODE = 0


def acquire_overlay_instance_gate(config_dir: Path | str) -> "OverlayInstanceGate":
    """启动链入口（``app.main``）：无条件建门（两个拓扑都抢，见模块 docstring）。

    返回门对象：``gate.acquired`` 为假即「已有实例在运行」，调用方留痕后按
    ``DUPLICATE_EXIT_CODE`` 退出；为真则须在退出路径（正常退出 / 会话结束）
    调 ``gate.release()``。
    """
    gate = OverlayInstanceGate(config_dir)
    gate.acquire()
    return gate


def _trace(config_dir: Path | str, message: str) -> None:
    """把一行事件追加到留痕文件；任何失败都静默（留痕绝不拖垮启动/拒绝路径）。"""
    try:
        path = Path(config_dir) / TRACE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        logger.debug("桌宠单实例进程门留痕失败: %s", message, exc_info=True)


class OverlayInstanceGate:
    """桌宠单实例进程门（QLockFile 语义，跨进程独占；两个拓扑共用）。

    生命周期：``acquire()`` → 进程存活期间持有 → ``release()``（幂等）。
    未持有时 ``release()`` 是 no-op——绝不动别人的锁文件。
    """

    def __init__(self, config_dir: Path | str, *, lock_name: str = LOCK_NAME,
                 stale_lock_ms: int = STALE_LOCK_MS) -> None:
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / lock_name
        self._lock = QLockFile(str(self.path))
        self._lock.setStaleLockTime(int(stale_lock_ms))
        self._held = False
        self._acquired = False

    # ------------------------------------------------------------ 状态
    @property
    def held(self) -> bool:
        """是否真正持有锁（决定 release 是否有事可做）。"""
        return self._held

    @property
    def acquired(self) -> bool:
        """True = 允许本进程继续启动（抢到门，或环境错误下放行）。"""
        return self._acquired

    def holder_pid(self) -> int:
        """锁文件记录的持有者 pid（0 = 读不到）。"""
        return self.holder_info()[0]

    def holder_info(self) -> tuple[int, str, str]:
        """锁文件记录的 (pid, hostname, appname)；异常一律归一为 (0, '', '')。"""
        try:
            pid, hostname, appname = self._lock.getLockInfo()
            return int(pid or 0), str(hostname or ""), str(appname or "")
        except Exception:
            return 0, "", ""

    # ------------------------------------------------------------ 抢门 / 释放
    def acquire(self) -> bool:
        """尝试抢门（非阻塞）。返回 True = 允许启动。

        - 抢到锁 → ``held``/``acquired`` 均真；
        - 锁文件存在但拿不到 → 已有实例在运行，``acquired`` 假（调用方留痕退出）；
        - 锁文件不存在 → 环境错误，放行启动（``acquired`` 真、``held`` 假）。
        """
        if self._acquired:
            return True
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # 目录都建不出来时交给下面的"锁文件不存在"分支判环境错误
        try:
            locked = bool(self._lock.tryLock(0))
        except Exception:
            logger.exception("桌宠单实例进程门抢锁异常: %s", self.path)
            locked = False
        if locked:
            self._held = True
            self._acquired = True
            logger.info("桌宠单实例进程门已获取: %s", self.path)
            _trace(self.config_dir, f"acquired pid={os.getpid()} lock={self.path}")
            return True
        if not self.path.exists():
            # 环境错误（配置目录不可写等）：锁文件根本没建起来，不是"已有实例"。
            # 放行启动，否则坏环境下桌宠会永久静默起不来。
            self._acquired = True
            logger.warning(
                "桌宠单实例进程门不可用（锁文件不存在，按环境错误放行）: %s", self.path)
            _trace(self.config_dir, f"gate-unavailable pid={os.getpid()} lock={self.path}")
            return True
        self._acquired = False
        logger.warning(
            "已有实例持有进程门，拒绝启动: %s (holder pid=%s)",
            self.path, self.holder_pid())
        _trace(self.config_dir,
               f"refused pid={os.getpid()} holder={self.holder_pid()} lock={self.path}")
        return False

    def release(self) -> None:
        """释放门（幂等）。未持有时为 no-op（不触碰持有者的锁文件）。"""
        self._acquired = False
        if not self._held:
            return
        self._held = False
        try:
            self._lock.unlock()
        except Exception:
            logger.exception("桌宠单实例进程门释放失败: %s", self.path)
