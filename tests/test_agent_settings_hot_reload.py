# -*- coding: utf-8 -*-
"""外部设置保存 → 运行期热应用：进程级共享联动读方 / 主动识屏（overlay 拓扑）。

包态合成事件验证（``.scratch/product-release-dsh-input-20261011/REPORT.md`` §9
OBS-1）实测：overlay 拓扑下外部（独立设置进程 / 手改）把 ``config.json`` 的
``agent_link.dsh`` 改成 false，磁盘已变而 DshMonitor 照旧运行。成因是
``AppShell._apply_external_config_change`` 的应用链在 overlay 下走空：

- 逐窗 ``win.refresh_pet_settings()`` —— overlay 拓扑 ``instances[].win`` 恒为
  None（单合成窗里没有 PetWindow），循环体一次都不执行；该链的终点
  ``sync_optional_services`` → ``manager.apply_config``（真正启停 DshMonitor）
  因此没有调用点；
- ``OverlayShell.refresh_settings()``（overlay 的窗口能力重应用）不含 agent_link
  也不含 proactive。

本文件在**真 AppShell + 真 Config + 真 config watcher + 真共享子系统**上把这条
链钉住：外部写盘 → 300ms 去抖 → ``_apply_external_config_change`` → 共享读方
启停 / 在飞完成确认退役 / 无重启重开。

装配口径与 ``tests/test_background_service_gates.py`` 同款：只有"起桌宠"那一层
（合成窗 / 托盘 / 会话结束探测器 / 灵动岛 / 可选服务）置空。出机边界（loopback
端口探活、pnpm 执行、真实 ``~/.dsh/profiles``、harness 拉起/收口）全部钉在替身
上——本用例会真的把 DshMonitor 启起来，绝不允许它去连用户机器上的真实 dsh web
（38080）或读用户的真实 profile。
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import pet.agent_link as agent_link_mod
import pet.app as app_mod
import pet.overlay_shell as overlay_shell_mod
from pet import harness_launcher
from pet.app import AppShell
from pet.config import Config

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 边界替身
@pytest.fixture
def out_of_box(tmp_path, monkeypatch):
    """把一切出机边界换成替身，并记录是否被走到。

    - ``is_running`` 恒 False 且不建任何 socket：真实 loopback 探活会去连
      ``38080``——那正是用户机器上真实 dsh web 的端口（包态报告 §6 的
      「dummyport 误连用户 loopback」偏离，不许重演）；
    - ``DSH_PROFILE_HOME`` 指向空目录：``_real_profiles()`` 恒空 →
      自检链根本走不到 pnpm；
    - ``_run_pnpm*`` 一律记录（不为空即失败，见 ``assert_no_process_side_effects``）；
    - harness 拉起/收口记录：自启策略只按原门判定，不许被本改动放大。
    """
    probed: list[int] = []
    monkeypatch.setattr(harness_launcher, "is_running",
                        lambda port: probed.append(port) or False)
    monkeypatch.setattr(harness_launcher, "desktop_process_running", lambda: False)
    stopped: list[int] = []
    monkeypatch.setattr(harness_launcher, "stop_self_launched_harness",
                        lambda: stopped.append(1) or [])
    launches: list[dict] = []
    monkeypatch.setattr(harness_launcher, "launch_harness",
                        lambda **kw: launches.append(kw) or [])
    home = tmp_path / "dsh-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_link_mod, "DSH_PROFILE_HOME", home)
    pnpm: list[tuple] = []
    monkeypatch.setattr(agent_link_mod, "_run_pnpm",
                        lambda *a, **k: pnpm.append(a) or (1, "blocked-by-test"))
    monkeypatch.setattr(agent_link_mod, "_run_pnpm_repairing_specs",
                        lambda *a, **k: pnpm.append(a) or (1, "", []))
    return SimpleNamespace(probed=probed, launches=launches, pnpm=pnpm, stopped=stopped)


def assert_no_process_side_effects(env) -> None:
    """没有任何真实进程/网络副作用：pnpm 没跑过，端口探活整体走替身。

    ``env.probed`` 非空 = 监视器的每一次端口检查都落在替身上（``is_running``
    被整体替换，函数体内不建任何 socket）——这正是包态报告 §6 那条偏离
    （假端口失守 → 真连到用户 loopback 38080）不可能重演的证据；空表反而说明
    监测没跑起来。
    """
    assert env.pnpm == [], "测试不得执行真实 pnpm"
    assert env.probed, "端口探活必须走替身（表为空说明探活没发生）"
    assert set(env.probed) <= {3080, 19387, 19389, 38080}, \
        f"探测端口超出了文档化的候选集: {sorted(set(env.probed))}"


# ---------------------------------------------------------------- 壳替身
class _OverlayShellStub:
    """overlay 壳替身：占据「起桌宠那一层」（真壳要建素材库/合成窗）。

    呈现面与真壳同名——``presentation_targets`` 在 overlay 下返回的就是这个壳，
    共享代理的扇出落点因此可断言。``refresh_settings`` 记录调用（真壳的同名方
    法在 AppShell 的 overlay 分支里被扇出到，被测的接线不依赖壳内实现）。
    """

    def __init__(self, qapp, instance, **kwargs):
        self.qapp = qapp
        self.instance = instance
        self.kwargs = kwargs
        self.started = 0
        self.refreshed = 0
        self.bubbles: list[str] = []
        self.alerts: list[str] = []
        self._alert_current = None
        self._alert_queue = None
        self._sticky_bubble_active = False
        self._bubble_suppressed = False
        self._bubble_busy_until = 0.0

    def start(self):
        self.started += 1

    def set_file_interpret_offer(self, offer):
        pass

    def refresh_settings(self):
        self.refreshed += 1

    # ---- 共享代理的呈现扇出面 ----
    def isVisible(self):
        return True

    def show_bubble(self, text, duration_ms=4500, **kwargs):
        self.bubbles.append(text)

    def show_alert(self, text, **kwargs):
        self.alerts.append(text)
        return True

    def resolve_alert(self, alert_id):
        pass

    def hide_bubble(self):
        pass

    def clear_alerts(self):
        pass

    def mark_activity(self):
        pass

    def request_link_idle(self):
        pass

    def request_link_anim(self, name):
        pass


class _LegacyWindowStub:
    """legacy 窗替身：``refresh_pet_settings`` 走窗链的终点。

    真窗链是 ``PetWindow.refresh_pet_settings`` → ``sync_optional_services`` →
    ``manager.apply_config``（window.py:3902 → window_optional_services.py:362）。
    这里只保留终点，用来验证 legacy 拓扑下**不会**出现第二次应用（共享管理器
    在 legacy 是被注入窗的同一份实例）。
    """

    def __init__(self, config, manager):
        self.cfg = config
        self.agent_link_manager = manager
        self.applies: list[int] = []

    def refresh_pet_settings(self):
        self.agent_link_manager.apply_config()
        self.applies.append(1)

    def isVisible(self):
        return True

    def show_bubble(self, text, duration_ms=4500, **kwargs):
        pass


def _start_shell(tmp_path, monkeypatch, *, dsh=True, topology="overlay", win=None):
    """真 ``AppShell.start()``，只有「起桌宠」那一层置空。"""
    monkeypatch.setenv("PET_RENDER_TOPOLOGY", topology)
    config = Config(base=tmp_path)
    agent_cfg = dict(config.get("agent_link") or {})
    agent_cfg["dsh"] = bool(dsh)
    config.set("agent_link", agent_cfg)
    assert config.save(), "落盘：外部变更监视的前提"
    shell = AppShell(app, config)
    shell.instance._create_ui = lambda cid: None
    shell.instance._apply_spawn_offset = lambda: None
    shell.instance._sync_animation_prewarm = lambda: None
    shell.instance._refresh_chat_windows = lambda: None
    shell._apply_balance_timer = lambda: None
    shell._sync_dynamic_island = lambda: None
    shell._install_session_watcher = lambda: None
    shell._sync_todo_service = lambda: None
    shell._sync_chime_service = lambda: None
    shell._sync_festival_service = lambda: None
    if win is not None:
        shell.instance.win = win
    monkeypatch.setattr(app_mod.QTimer, "singleShot", lambda *a, **k: None)
    shell.start()
    return shell, config


@pytest.fixture
def overlay_shell(tmp_path, monkeypatch, out_of_box):
    """真 AppShell + 真共享子系统 + 真 watcher，overlay 拓扑，联动已开。"""
    monkeypatch.setattr(overlay_shell_mod, "OverlayShell", _OverlayShellStub)
    shell, config = _start_shell(tmp_path, monkeypatch, dsh=True)
    assert shell._overlay_shell is not None, "用例前置：overlay 拓扑已接管"
    return SimpleNamespace(shell=shell, config=config, env=out_of_box,
                           ui=shell._overlay_shell, tmp=tmp_path)


def _save_external(config) -> None:
    """外部保存（有界重试：Windows os.replace 偶发被瞬时占用拒绝）。"""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if config.save():
            return
        time.sleep(0.05)
    raise AssertionError("外部保存重试 10s 仍失败")


def _pump(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _wait_until(predicate, *, budget: float = 10.0, what: str = "") -> None:
    """宽预算轮询（禁固定 sleep 赌时序；CI runner 慢数倍）。"""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{budget}s）：{what or predicate}")


def _dsh_monitor(shell):
    return shell._shared.agent_link.monitors["dsh"]


def _set_link(config, on: bool) -> None:
    """外部写盘：改 ``agent_link.dsh`` 并落盘（独立设置进程的那一步）。"""
    agent_cfg = dict(config.get("agent_link") or {})
    agent_cfg["dsh"] = bool(on)
    config.set("agent_link", agent_cfg)
    _save_external(config)


def _complete_turn(monitor, identity: str, step: str) -> None:
    """投递一条 live 回合收尾（桥接 ``turn/end`` 的生产信号形状）。

    走监视器自己的信号，不直调管理器私有方法：``DshMonitor._on_parsed_record``
    在真实桥目录读到记录时发的是同一条 (agent_key, payload)。
    """
    _complete_turn.seq += 1
    monitor.dsh_turn_completed.emit("dsh", {
        "identity": (identity, step, _complete_turn.seq),
        "sessionId": identity,
        "step": step,
        "source_event": "turn/end",
        "gen": monitor._emit_gen,
    })


_complete_turn.seq = 0


# ---------------------------------------------------------------- 外部改配置 → 共享读方启停
def test_overlay_external_disable_stops_shared_dsh_monitor(overlay_shell, monkeypatch):
    """overlay：外部改 ``agent_link.dsh=false`` → 共享 DshMonitor 必须停。

    红前：overlay 下应用链没有窗可走，共享管理器一次 ``apply_config`` 都没有，
    磁盘已是 false 而读方照旧运行（包态 OBS-1）。
    """
    shell, config, env = overlay_shell.shell, overlay_shell.config, overlay_shell.env
    manager = shell._shared.agent_link
    monitor = _dsh_monitor(shell)
    assert monitor._running is True, "用例前置：联动开着时读方在跑"

    applies: list[str] = []
    real_apply = manager.apply_config
    monkeypatch.setattr(manager, "apply_config",
                        lambda: applies.append("agent_link") or real_apply())
    proactive_applies: list[str] = []
    real_proactive = shell._shared.proactive.apply_config
    monkeypatch.setattr(shell._shared.proactive, "apply_config",
                        lambda: proactive_applies.append("proactive") or real_proactive())

    _set_link(config, False)
    _wait_until(lambda: monitor._running is False, what="外部写盘后读方停表")

    assert monitor._running is False
    assert monitor._worker is None or not monitor._worker.is_alive()
    # 「接进应用级共享 apply_config 一次」：不重复启动/停服务/线程。
    assert applies == ["agent_link"], f"共享 apply_config 必须恰好一次，实为 {applies}"
    assert proactive_applies == ["proactive"], "主动识屏共用同一条应用链，不许漏"
    assert overlay_shell.ui.refreshed >= 1, "overlay 壳的窗口能力重应用仍要走到"
    _pump(0.3)
    assert applies == ["agent_link"], "去抖窗口之后不得再应用一次"
    assert manager.monitors["dsh"]._running is False
    assert_no_process_side_effects(env)


def test_overlay_external_disable_retires_inflight_done_confirmation(overlay_shell):
    """关闭联动立即作废在飞的 800ms 完成确认：不再弹「已完成本轮任务」。

    红前：读方没停 → 确认按时到期弹泡（包态报告 S5 的 L01 语义在 overlay 拓扑
    下漏掉了；探针实测 `bubbles = ['DSH 已完成本轮任务。']`）。
    """
    shell, config = overlay_shell.shell, overlay_shell.config
    manager = shell._shared.agent_link
    monitor = _dsh_monitor(shell)

    _complete_turn(monitor, "sess-pending", "1")
    assert "dsh" in manager._done_pending, "用例前置：完成确认已排定（800ms 窗内）"

    _set_link(config, False)
    _wait_until(lambda: monitor._running is False, what="外部写盘后读方停表")
    assert "dsh" not in manager._done_pending, "关闭必须立即退役在飞确认"
    _pump(1.6)  # 越过 800ms 确认窗
    assert overlay_shell.ui.bubbles == [], "关闭后不得再提醒「已完成本轮任务」"


def test_overlay_external_reenable_restarts_monitor_without_app_restart(overlay_shell):
    """关 → 开全程不重启 app：新代次重开，且新代次的真实收尾照常提醒。"""
    shell, config = overlay_shell.shell, overlay_shell.config
    monitor = _dsh_monitor(shell)
    gen_before = monitor._emit_gen

    _set_link(config, False)
    _wait_until(lambda: monitor._running is False, what="关")
    _set_link(config, True)
    _wait_until(lambda: monitor._running is True, what="开（无需重启 app）")

    assert shell._shared.agent_link.monitors["dsh"] is monitor, \
        "重开复用同一份监视器对象，不得新建服务/线程"
    assert overlay_shell.ui.bubbles == [], "重开之前不得有残留提醒"
    assert monitor._worker is not None and monitor._worker.is_alive()
    assert monitor._emit_gen > gen_before, "重开必须是新代次（旧代次簿记已退役）"

    _complete_turn(monitor, "sess-reopen", "1")
    _wait_until(lambda: bool(overlay_shell.ui.bubbles), what="新代次的完成提醒")
    assert any("完成" in b or "干完活" in b for b in overlay_shell.ui.bubbles), \
        f"新代次收尾必须照常提醒，实为 {overlay_shell.ui.bubbles}"


def test_overlay_external_change_does_not_expand_harness_autostart(overlay_shell):
    """自启策略只按原门判定：热应用既不新拉起服务，也不改变自启门读数。"""
    shell, config, env = overlay_shell.shell, overlay_shell.config, overlay_shell.env
    assert shell._harness_autostart_wanted() is False, "用例前置：默认不开自启"

    _set_link(config, False)
    _wait_until(lambda: _dsh_monitor(shell)._running is False, what="关")
    _wait_until(lambda: bool(env.stopped), what="关联动后的 harness 收口线程")
    _pump(0.5)
    assert env.launches == [], "关联动不得拉起 dsh 服务"
    assert env.stopped == [1], "自拉起 dsh 的收口仍按原策略恰一次（不得被新接线放大）"
    assert shell._harness_autostart_wanted() is False

    _set_link(config, True)
    _wait_until(lambda: _dsh_monitor(shell)._running is True, what="开")
    _pump(0.5)
    assert env.launches == [], "重开联动只恢复读方，不得连带拉起 dsh 服务"
    assert env.stopped == [1], "重开不产生第二次收口"
    assert shell._harness_autostart_wanted() is False, "自启门只读 harness_autostart + 联动门"


def test_late_duplicate_notification_does_not_restop_harness(overlay_shell):
    """首轮应用后迟到的同内容重发 → 不得第二次收口自拉起 dsh（也不得拉起）。

    mac CI 那一轮的形态：``_set_link(config, False)`` 之后 ``env.stopped`` 成了
    ``[1, 1]``——同一次保存的重复通知迟到于首轮应用之后，第二次
    ``_apply_external_config_change`` 又派了一次 harness 收口线程。与
    ``test_background_service_gates`` 里的同族用例同一个成因（文件侧 arm 无
    签名门），这里钉住它落在 DSH 联动链上的后果：收口恰一次、不新拉起服务。
    """
    shell, config, env = overlay_shell.shell, overlay_shell.config, overlay_shell.env
    watcher = shell._config_watcher
    _set_link(config, False)
    _wait_until(lambda: _dsh_monitor(shell)._running is False, what="关")
    _wait_until(lambda: bool(env.stopped), what="关联动后的 harness 收口线程")
    _pump(0.4)

    watcher.fileChanged.emit(str(shell.config.path))  # 迟到的重复通知（内容未变）
    _pump(1.0)  # 去抖窗口（300ms）+ 收口线程调度的余量
    assert env.stopped == [1], f"迟到的重复通知不得再收口一次，实为 {env.stopped}"
    assert env.launches == [], "关联动不得拉起 dsh 服务"
    assert _dsh_monitor(shell)._running is False, "读方保持关闭"
    assert shell._harness_autostart_wanted() is False
    assert_no_process_side_effects(env)


def test_legacy_external_change_applies_shared_manager_once(tmp_path, monkeypatch, out_of_box):
    """legacy 逐位不变：窗链照旧应用一次，应用级不得再补一次（无重复副作用）。"""
    shell, config = _start_shell(tmp_path, monkeypatch,
                                 dsh=True, topology="legacy")
    win = _LegacyWindowStub(config, shell._shared.agent_link)
    shell.instance.win = win
    assert _dsh_monitor(shell)._running is True, "用例前置：联动开着时读方在跑"

    _set_link(config, False)
    _wait_until(lambda: _dsh_monitor(shell)._running is False, what="legacy 关读方")
    assert len(win.applies) == 1, f"legacy 窗链应恰好应用一次，实为 {len(win.applies)}"
    assert_no_process_side_effects(out_of_box)
