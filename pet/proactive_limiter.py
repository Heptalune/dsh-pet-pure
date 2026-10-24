# -*- coding: utf-8 -*-
"""主动识屏频控与熔断门禁 — ProactiveLimiter 与有效配置计算。

批6-1 从 proactive.py 整体迁出（纯搬移，逻辑/默认值/时序零改动）：
- 合法参数范围与默认值常量（PRESET_DEFAULTS / DEFAULT_PROACTIVE_CONFIG）；
- clamp 辅助与 effective_proactive_config（有效运行时配置计算）；
- ProactiveLimiter（跨实例共享状态、每日上限、冷却、熔断、dry_run 隔离）。

依赖方向：proactive -> proactive_limiter，本模块不得反向 import pet.proactive。
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config import atomic_replace_with_retry


# 合法参数范围与默认值常量定义（依据实施手册 §2 与 §3）
PRESET_DEFAULTS: dict[str, dict[str, int]] = {
    "quiet": {"dwell_seconds": 90, "cooldown_minutes": 10, "daily_cap": 8},
    "balanced": {"dwell_seconds": 45, "cooldown_minutes": 5, "daily_cap": 15},
    "active": {"dwell_seconds": 20, "cooldown_minutes": 3, "daily_cap": 25},
}

DEFAULT_PROACTIVE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "dry_run": False,
    "preset": "balanced",
    "allow_when_mouse_through": True,
    "whitelist": [],
    "dwell_seconds": 45,
    "require_idle": False,
    "min_idle_seconds": 30,
    "cooldown_minutes": 5,
    "daily_cap": 15,
    "min_request_interval_seconds": 60,
    "change_threshold": 8,
    "prefer_free_provider": True,
    "pre_cue": True,
}


def _clamp(val: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, num))


def _clamp_int(val: Any, default: int, minimum: int, maximum: int) -> int:
    return round(_clamp(val, default, minimum, maximum))


def _state_str(value: Any) -> str:
    """状态文本字段：只接受 str（None/数字等一律判非法）。"""
    if not isinstance(value, str):
        raise TypeError(f"非字符串状态字段: {value!r}")
    return value


def _state_int(value: Any) -> int:
    """状态整数字段：bool 判非法（True 静默变 1 属错误类型穿透），其余交给 int()。"""
    if isinstance(value, bool):
        raise TypeError("bool 不是合法整数状态字段")
    return int(value)


def _state_float(value: Any) -> float:
    """状态数值字段：bool 判非法（同 _state_int），其余交给 float()。"""
    if isinstance(value, bool):
        raise TypeError("bool 不是合法数值状态字段")
    return float(value)


#: 状态文件逐字段类型契约（缺陷 17）：键 → 转换器。
#:
#: 背景：加载只判 ``isinstance(raw, dict)`` 时，「合法 JSON + 错字段类型」
#: （典型 ``{"count": null}``）会整体穿透进内存状态，随后在 allow/consume_budget
#: 的裸 ``int()``/``float()`` 处以 TypeError 打断调用链（额度门直接抛异常，
#: 而不是按契约返回 False）。这里逐字段试转换，任一字段失败即整份状态回退
#: 默认——「半个文件可信」比整个不可信更危险。
_STATE_FIELDS: dict[str, Callable[[Any], Any]] = {
    "date": _state_str,
    "count": _state_int,
    "last_trigger": _state_float,
    "last_request": _state_float,
    "consecutive_failures": _state_int,
    "paused_until_date": _state_str,
}


def effective_proactive_config(raw: dict | None) -> dict[str, Any]:
    """计算主动识屏的有效运行时配置。

    - 以 DEFAULT_PROACTIVE_CONFIG 为基础；
    - 根据 preset 填充 dwell_seconds、cooldown_minutes、daily_cap；
    - 合并用户 raw 字典中的自定义配置；
    - 所有数值 clamp 到手册 §2 合法范围；
    - require_idle 为 False 时，effective 配置中 min_idle_seconds 视为 0（保留原始键不变）；
    - 非法 preset 回退为 'balanced'。
    """
    result = dict(DEFAULT_PROACTIVE_CONFIG)
    raw = raw if isinstance(raw, dict) else {}

    preset = str(raw.get("preset", result["preset"])).strip().lower()
    if preset not in PRESET_DEFAULTS and preset != "custom":
        preset = "balanced"
    result["preset"] = preset

    # 预设覆盖三项（custom 不覆盖）
    if preset in PRESET_DEFAULTS:
        result.update(PRESET_DEFAULTS[preset])

    # 用户手动配置项覆盖
    for k, v in raw.items():
        if k in DEFAULT_PROACTIVE_CONFIG and v is not None:
            result[k] = v

    # 确保 preset 在非法情况下已被规范化
    result["preset"] = preset

    # 规范化与范围 clamp
    result["enabled"] = bool(result.get("enabled", False))
    result["dry_run"] = bool(result.get("dry_run", False))
    result["allow_when_mouse_through"] = bool(result.get("allow_when_mouse_through", True))
    result["require_idle"] = bool(result.get("require_idle", False))
    result["prefer_free_provider"] = bool(result.get("prefer_free_provider", True))
    result["pre_cue"] = bool(result.get("pre_cue", True))

    whitelist = result.get("whitelist")
    if isinstance(whitelist, list):
        result["whitelist"] = [str(item).strip() for item in whitelist if str(item).strip()]
    else:
        result["whitelist"] = []

    # clamp 数值范围（手册 §2）
    # dwell_seconds: 15 ~ 600 (默认 45)
    # min_idle_seconds: 0 ~ 3600 (默认 30)
    # cooldown_minutes: 1 ~ 120 (默认 5)
    # daily_cap: 1 ~ 9999 (默认 15；用户自定义不设硬顶，约等于不限)
    # min_request_interval_seconds: 30 ~ 3600 (默认 60)
    # change_threshold: 0 ~ 32 (默认 8)
    result["dwell_seconds"] = _clamp_int(result.get("dwell_seconds"), 45, 15, 600)
    # cooldown 允许 0.5 分钟粒度（用户反馈整分钟太粗）
    result["cooldown_minutes"] = _clamp(result.get("cooldown_minutes"), 5.0, 0.5, 120.0)
    result["daily_cap"] = _clamp_int(result.get("daily_cap"), 15, 1, 9999)
    result["min_request_interval_seconds"] = _clamp_int(
        result.get("min_request_interval_seconds"), 60, 30, 3600
    )
    result["change_threshold"] = _clamp_int(result.get("change_threshold"), 8, 0, 32)

    raw_min_idle = _clamp_int(result.get("min_idle_seconds"), 30, 0, 3600)
    result["min_idle_seconds"] = raw_min_idle if result["require_idle"] else 0

    return result


class ProactiveLimiter:
    """主动识屏频控与熔断门禁管理器。

    状态文件：<config.dir>/proactive_screen_state.json（dry_run 模式使用独立文件）
    - 状态文件跨实例共享，daily_cap / 最小间隔 / 冷却为全局上限；
    - 支持 dry_run 模式：仅维护 dry-run 状态与 60s 最小间隔，绝不消耗用户当日真实额度与熔断状态；
    - 支持可注入时钟与日期（便于单测）；
    - 采用 .tmp + 原子替换持久化；损坏回退全新状态。
    """

    def __init__(
        self,
        state_path: Path | str,
        cfg: dict | None,
        *,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time,
        today: Callable[[], str] | None = None,
    ) -> None:
        self.raw_state_path = Path(state_path)
        self.dry_run = dry_run
        # dry_run 模式使用独立的 dryrun_state 文件，防止污染真实状态
        if self.dry_run:
            self.state_path = self.raw_state_path.with_name("proactive_screen_dryrun_state.json")
        else:
            self.state_path = self.raw_state_path
        self.cfg = effective_proactive_config(cfg)
        self._clock = clock
        self._today_fn = today or (lambda: datetime.date.today().isoformat())

    def update_config(self, cfg: dict | None, dry_run: bool | None = None) -> None:
        """更新内部缓存的有效配置与 dry_run 模式。"""
        self.cfg = effective_proactive_config(cfg)
        if dry_run is not None and dry_run != self.dry_run:
            self.dry_run = dry_run
            if self.dry_run:
                self.state_path = self.raw_state_path.with_name("proactive_screen_dryrun_state.json")
            else:
                self.state_path = self.raw_state_path

    def _default_state(self) -> dict[str, Any]:
        return {
            "date": self._today_fn(),
            "count": 0,
            "last_trigger": 0.0,
            "last_request": 0.0,
            "consecutive_failures": 0,
            "paused_until_date": "",
        }

    @contextlib.contextmanager
    def _locked(self):
        """跨进程互斥（多开共用一份频控状态）：Windows 用 msvcrt，POSIX 用 flock。

        锁文件随 state_path 派生；拿不到锁时静默降级为无锁（读改写竞态退化为
        极少数情况下的计数偏差，不影响单实例正确性）。
        """
        fh = None
        try:
            fh = open(self.state_path.with_suffix(self.state_path.suffix + ".lock"), "a+b")
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)  # append 模式初始位置在 EOF，锁/解锁必须落在同一字节
                # 非阻塞+短重试：allow/try_acquire 会在 GUI 线程（_on_frame_ready）调用，
                # 不能用 LK_LOCK 的 ~10s 阻塞重试；锁持有时间是微秒级，100ms 内必拿到，
                # 拿不到则降级无锁（竞态退化为计数偏差，不影响正确性主线）。
                for _ in range(5):
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.02)
                else:
                    fh.close()
                    fh = None
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            if fh is not None:
                fh.close()
                fh = None
        try:
            yield
        finally:
            if fh is not None:
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                fh.close()

    def _load_state(self) -> dict[str, Any]:
        """读取状态，跨天自动重置，损坏/字段类型非法一律回退默认状态。"""
        current_today = self._today_fn()
        raw_state: Any = None

        if self.state_path.is_file():
            try:
                raw_state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw_state = None  # 文件损坏或不可读，使用默认状态

        state = self._coerce_state(raw_state)

        # 规则 1：跨天重置 count 与熔断状态
        if state.get("date") != current_today:
            state["date"] = current_today
            state["count"] = 0
            state["consecutive_failures"] = 0
            state["paused_until_date"] = ""

        return state

    def _coerce_state(self, raw: Any) -> dict[str, Any]:
        """把盘上原始对象逐字段转换成合法状态（缺陷 17）。

        非 dict、或任一字段转换失败 → 整份回退默认状态（``_default_state``）；
        缺键保留默认值；未知键原样保留（旧行为是全量 dict.update，保留可避免
        丢未来新增字段）。
        """
        if not isinstance(raw, dict):
            return self._default_state()
        state = self._default_state()
        for key, convert in _STATE_FIELDS.items():
            if key not in raw:
                continue
            try:
                state[key] = convert(raw[key])
            except (TypeError, ValueError):
                return self._default_state()
        for key, value in raw.items():
            if key not in state:
                state[key] = value
        return state

    def _save_state(self, state: dict[str, Any]) -> bool:
        """原子写入状态文件，返回是否写成功（tmp 名带 PID，避免多实例抢临时文件）。

        写盘失败**不再静默吞**：记 warning 并返回 False，调用方据此外报/降级
        （consume_budget 在持久化失败时 fail-closed，见缺陷 17）。替换走
        ``config.atomic_replace_with_retry``（骑过 Windows 读句柄造成的瞬时共享
        冲突，与 Config.save 同一路径）；任何出口都清掉 .tmp，失败残留会污染
        配置目录。
        """
        tmp = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                atomic_replace_with_retry(tmp, self.state_path)
            finally:
                tmp.unlink(missing_ok=True)
        except OSError as exc:
            logging.warning('主动识屏状态写入失败: %s (%s)', self.state_path, exc)
            return False
        return True

    def allow(self) -> tuple[bool, str]:
        """判定当前是否允许发起主动识屏请求（跨进程加锁，判定期间状态不被并发改写）。

        规则判定顺序（手册 §4.3）：
        1. 跨天重置（由 _load_state 处理）；
        2. paused_until_date == today -> 拒绝（当日熔断）；
        3. count >= daily_cap -> 拒绝（达到每日上限）；
        4. now - last_request < min_request_interval_seconds -> 拒绝（请求间隔过短）；
        5. now - last_trigger < cooldown_minutes * 60 -> 拒绝（冷却中）；
        6. 否则放行。

        返回: (allowed: bool, reason: str)
        """
        with self._locked():
            return self._allow_unlocked()

    def _allow_unlocked(self) -> tuple[bool, str]:
        state = self._load_state()
        now = self._clock()
        current_today = self._today_fn()

        if state.get("paused_until_date") == current_today:
            return False, "paused_by_circuit_breaker"

        daily_cap = int(self.cfg.get("daily_cap", 15))
        if int(state.get("count", 0)) >= daily_cap:
            return False, "daily_cap_reached"

        min_req_interval = float(self.cfg.get("min_request_interval_seconds", 60))
        last_req = float(state.get("last_request", 0.0))
        if (now - last_req) < min_req_interval:
            return False, "min_request_interval_cooldown"

        cooldown_sec = float(self.cfg.get("cooldown_minutes", 5)) * 60.0
        last_trig = float(state.get("last_trigger", 0.0))
        if (now - last_trig) < cooldown_sec:
            return False, "cooldown_active"

        return True, "ok"

    def try_acquire(self) -> tuple[bool, str]:
        """原子版 allow + record_attempt：判定与盖章在同一把锁内完成，
        多开实例不会同时通过判定后再互相覆盖 last_request（lost update）。"""
        with self._locked():
            ok, reason = self._allow_unlocked()
            if ok:
                state = self._load_state()
                state["last_request"] = self._clock()
                self._save_state(state)
            return ok, reason

    def record_attempt(self) -> None:
        """记录一次请求尝试（更新 last_request 时戳）。"""
        with self._locked():
            state = self._load_state()
            state["last_request"] = self._clock()
            self._save_state(state)

    def consume_budget(self) -> bool:
        """每次真实 HTTP 请求前调用：消耗一次当日请求预算。

        预算（count）按真实请求次数计费——一次触发里的多次重试各自占用额度，
        不再只记一次。返回 False 表示当日预算已耗尽，调用方应停止重试。

        持久化失败同样返回 False（fail-closed，缺陷 17）：额度记不上账还放行
        = 当日上限形同虚设（重启后计数回退），调用方（``vision.consume_budget``
        钩子）按既有的「预算不可用」分支抛 VisionError 停止本次请求。
        """
        with self._locked():
            state = self._load_state()
            now = self._clock()
            daily_cap = int(self.cfg.get("daily_cap", 15))
            if int(state.get("count", 0)) >= daily_cap:
                return False
            state["count"] = int(state.get("count", 0)) + 1
            state["last_request"] = now
            return self._save_state(state)

    def record_success(self) -> None:
        """记录一次成功的主动关怀（更新 last_trigger, last_request，清空失败计数）。

        注意：预算（count）已由 consume_budget 在每次真实 HTTP 请求前消耗，
        此处不再累加，避免一次请求被重复计费。
        """
        with self._locked():
            state = self._load_state()
            now = self._clock()
            state["last_trigger"] = now
            state["last_request"] = now
            state["consecutive_failures"] = 0
            self._save_state(state)

    def record_failure(self) -> bool:
        """记录一次请求失败。

        若连续失败次数达到 3 次，触发当日熔断（paused_until_date=today）。
        返回: 是否触发了当日熔断。
        """
        with self._locked():
            state = self._load_state()
            now = self._clock()
            state["last_request"] = now
            fails = int(state.get("consecutive_failures", 0)) + 1
            state["consecutive_failures"] = fails

            tripped = False
            if fails >= 3:
                state["paused_until_date"] = self._today_fn()
                tripped = True

            self._save_state(state)
            return tripped
