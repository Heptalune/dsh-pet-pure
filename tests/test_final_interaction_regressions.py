# -*- coding: utf-8 -*-
"""终审收口：阻塞型交互（审批/问题）的**唯一 resolved 摄入**、有界 pending 真值表、
隐藏期请求的恢复投递，以及 worker 在 C++ 属主已析构时的发射边界。

三组独立问题（各自先红后绿）：

- **A03 双通道误收尾**：同一条 ``approval/resolved`` 记录同时喂语义层
  （``normalized_event``）与旧直通通道（``approval_resolved``）。旧通道只按
  rpcId/callId 匹配、不看来源与会话，于是同一 rpcId 被跨会话复用时**第二条**
  审批被误关（用户仍需处理的提醒被收掉）。修法 = 两条通道共用一个按
  「来源 + 会话 + 身份」匹配的实现，第二通道对同一条记录结构性 no-op。
- **A05 隐藏期到达的请求没有恢复路径**：``MultiWindowProxy`` 只把提醒投给可见窗，
  全部隐藏/无宠期间到达的审批在显示 owner 侧直接落空，恢复显示后也不会回来。
  修法 = 请求（状态类提醒）不因不可见被整个丢掉（交给应用级 owner），pending
  真值留在 manager 表里，恢复可见时按同一 interaction_id 补投（窗口按 id 合并，
  最多一份可见正文）。
- **A06 pending 真值表无界**：显示队列有界（``ALERT_QUEUE_LIMIT`` = 64），但
  manager 的 ``_pending_interactions`` 只增不减。修法 = 明确容量 + 淘汰最低优先
  里最旧的一条并留痕；淘汰**只影响本地呈现**（不批准/不拒绝、不写回上游），
  被淘汰项在显示 owner 上按同一 alert_id 清理，且其 resolved 到达时是 no-op。
- **线程异常**：worker 首轮在 ``_worker_started`` 里发射时若 C++ 属主已被析构，
  ``signal.emit`` 抛 ``RuntimeError: Signal source has been deleted`` 冒泡出线程
  （pytest 记为 ``PytestUnhandledThreadExceptionWarning``）。修法 = 发射前/失败时
  判定信号源是否已失效，失效则退役该代次并 debug 留痕，**不吞其它异常**。

本文件（含 pytest 用例）不创建真实窗口、不启动监视器 worker（除线程边界两条）、
不读写用户配置：临时 JSONL 只落在 ``tmp_path``。
"""
from __future__ import annotations

import gc
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet import harness_launcher  # noqa: E402
from pet.agent_link import AgentLinkManager, BaseAgentMonitor  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.multi_window_shared import MultiWindowProxy  # noqa: E402

app = QApplication.instance() or QApplication([])


def _wait_until(pred, timeout=10.0):
    """事件驱动等待（不 sleep 猜时序）：处理 Qt 事件直到条件成立或硬超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


class _MergingWin:
    """提醒宿主替身：``show_alert`` 按 ``alert_id`` 合并（同 OverlayShell 语义）。

    ``visible=False`` 模拟「全部隐藏 / 无宠」：真实 owner（OverlayShell 的提醒队列
    + ``window_alerts``）在不可见宿主上不把条目挂上屏，本替身同样只在可见时把
    条目记进 ``visible_by_id``，其余情况只记录**收到过这次投递**（``alerts``）。
    """

    def __init__(self, visible: bool = True) -> None:
        self._visible = bool(visible)
        self.alerts: list[dict] = []
        self.visible_by_id: dict[str, str] = {}
        self.anonymous: list[str] = []
        self.resolved: list[str] = []
        self.bubbles: list[str] = []
        self.hidden_calls = 0
        self.cleared = 0
        self._alert_current = None
        self._alert_queue: list = []
        self._sticky_bubble_active = False
        self._bubble_suppressed = False
        self._bubble_busy_until = 0.0
        self._dragging = False
        self._physics_mode = None
        self._click_effect_phase = 0
        self.mouse_through = False
        self.cats = {"acts": ["写代码"]}

    # ---- 可见性 ----
    def isVisible(self) -> bool:  # noqa: N802
        return self._visible

    def show(self) -> None:
        self._visible = True

    def hide(self) -> None:
        self._visible = False

    # ---- 提醒面（宿主代价：真实 owner 在不可见宿主上不排队）----
    def show_alert(self, text, *, subtitle="", duration_ms=0, buttons=None,
                   sticky=True, alert_id="", priority=3, alert_type="watchdog",
                   metadata=None) -> None:
        self.alerts.append({
            "text": str(text), "alert_id": str(alert_id), "sticky": bool(sticky),
            "priority": int(priority), "alert_type": str(alert_type),
            "duration_ms": int(duration_ms), "buttons": buttons,
        })
        if not self._visible:
            return
        if alert_id:
            self.visible_by_id[str(alert_id)] = str(text)
        else:
            self.anonymous.append(str(text))

    def show_bubble(self, text, duration_ms=3200, **_kw) -> None:
        self.bubbles.append(str(text))

    def resolve_alert(self, alert_id) -> None:
        self.resolved.append(str(alert_id))
        self.visible_by_id.pop(str(alert_id), None)

    def clear_alerts(self) -> None:
        self.cleared += 1
        self.visible_by_id.clear()

    def hide_bubble(self) -> None:
        self.hidden_calls += 1

    # ---- 共享 proxy 需要的聚合面 / 生命周期面 ----
    def request_link_anim(self, name) -> None:
        pass

    def request_link_idle(self) -> None:
        pass

    def set_link_next_provider(self, provider) -> None:
        pass

    def mark_activity(self) -> None:
        pass

    def clear_pending_link_anim(self) -> None:
        pass

    def hold_bubble(self, seconds) -> None:
        self._bubble_busy_until = seconds

    def on_look_synced(self, user_text, reply) -> None:
        pass

    def deleteLater(self) -> None:
        pass


class _ProxyShell:
    """``MultiWindowProxy`` 的只读 shell 替身（legacy 拓扑：扇出 = instances[].win）。"""

    def __init__(self, config, windows) -> None:
        self.config = config
        self.instances = [SimpleNamespace(win=w) for w in windows]
        self._shared = None


def _iid(kind: str, agent: str, field: str, value: str, session: str = "") -> str:
    """产品交互 id 的期望形状：kind + 来源 + 身份命名空间/value (+ 会话)。

    身份必须**先在键里**（见 ``AgentLinkManager._interaction_key``）：只按
    ``kind:rpcId`` 存会让不同来源/会话的同 id 交互在存储层就互相覆盖。
    """
    return f"{kind}:{agent}:{field}:{value}" + (f":{session}" if session else "")


def _write(events_dir: Path, name: str, *records) -> None:
    with (events_dir / name).open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


@pytest.fixture(autouse=True)
def _dsh_probe_pinned_online(monkeypatch):
    """把 DSH 探活（真连 3080/19387/38080 + 枚举 OS 进程）钉成「在线」。

    ``DshMonitor._poll`` 每 2 拍探活一次，本模块手动驱动 ``_poll`` 且都要记录被
    消费——不钉就取决于跑测试这台机器上有没有 DSH：本机有 ⇒ 假绿，CI 没有 ⇒
    第 2 拍转 offline、``dismiss_all_interactions()`` 清空 pending ⇒ 一批断言红。
    只钉网络/OS 边界，产品探活行为不改；副作用是真 socket 不再发生。
    真实离线口径由 ``test_offline_probe_override_...`` 在用例体内覆盖（后打的补丁生效）。
    """
    monkeypatch.setattr(harness_launcher, "is_running", lambda port: True)
    monkeypatch.setattr(harness_launcher, "desktop_process_running", lambda: True)


def _build(tmp_path, win):
    """真 manager + 真 DshMonitor（worker 不启动、手动驱动 ``_poll``）。"""
    cfg = Config(base=tmp_path / "cfg")
    mgr = AgentLinkManager(win, cfg, min_interval=0.0)
    mon = mgr.monitors["dsh"]
    # 白盒：等效「通道已启用」——手动驱动 _poll 的测试必须置真（迟到信号隔离闸）。
    mon._running = True
    mon.events_dir.mkdir(parents=True, exist_ok=True)
    mon._tailer._initial_backfill_done = True
    return mgr, mon


def _build_proxy(tmp_path, windows):
    cfg = Config(base=tmp_path / "cfg")
    proxy = MultiWindowProxy(_ProxyShell(cfg, windows))
    mgr = AgentLinkManager(proxy, cfg, min_interval=0.0)
    mon = mgr.monitors["dsh"]
    mon._running = True
    mon.events_dir.mkdir(parents=True, exist_ok=True)
    mon._tailer._initial_backfill_done = True
    return mgr, mon, proxy


# ======================================================================
# A03：唯一 resolved 摄入（来源 + 会话 + 身份），第二通道结构性 no-op
# ======================================================================
def test_one_resolved_record_closes_only_its_own_session(tmp_path):
    """一条 ``approval/resolved``（会话 A + 共享 rpcId）只许关掉 A 的审批。

    语义层按会话已经正确删 A；旧直通通道若再按 rpcId 扫一遍就会连 B 一起删
    （用户仍需处理的审批提醒被收掉）。两条通道共享同一份「来源+会话+身份」
    匹配后，第二条通道对同一条记录自然找不到候选 = 结构性 no-op。
    """
    win = _MergingWin()
    mgr, mon = _build(tmp_path, win)
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "shared-rpc",
                "sessionId": "A"},
               {"event": "approval/request", "tool": "bash", "rpcId": "shared-rpc",
                "sessionId": "B"})
        mon._poll()
        assert len(mgr.pending_interactions_for("dsh")) == 2, "两个会话各一条审批"

        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/resolved", "sessionId": "A", "rpcId": "shared-rpc"})
        mon._poll()

        remaining = list(mgr.pending_interactions_for("dsh").values())
        assert [item["session_id"] for item in remaining] == ["B"], (
            f"只允许关掉会话 A 的那条，实际剩余 {[i.get('session_id') for i in remaining]}")
        assert win.resolved == [f"interaction:{_iid('approval', 'dsh', 'rpc_id', 'shared-rpc', 'A')}"], (
            f"显示 owner 只许收到一次按该 id 的关闭：{win.resolved}")
    finally:
        mgr.shutdown()


def test_question_resolved_is_isolated_across_sessions(tmp_path):
    """question 同样按会话隔离：会话 B 的问题不得被会话 A 的收尾帧关掉。"""
    win = _MergingWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        for session in ("A", "B"):
            mgr._on_question_request("dsh", {"rpcId": "q-shared", "sessionId": session,
                                            "questions": []})
        assert len(mgr.pending_interactions_for("dsh")) == 2

        mgr._on_question_resolved("dsh", {"rpcId": "q-shared", "sessionId": "A"})

        remaining = list(mgr.pending_interactions_for("dsh").values())
        assert [item["session_id"] for item in remaining] == ["B"]
    finally:
        mgr.shutdown()


@pytest.mark.parametrize("kind", ["approval", "question"])
def test_legacy_resolved_is_scoped_to_its_source(tmp_path, kind):
    """旧直通通道也必须按来源隔离：别的 agent 的同名 id 收尾帧不得误关本 agent。"""
    win = _MergingWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        if kind == "approval":
            mgr._on_approval_request("claude", {"tool": "bash", "rpcId": "r-x", "sessionId": "s-1"})
            mgr._on_approval_resolved("dsh", {"rpcId": "r-x"})
        else:
            mgr._on_question_request("claude", {"rpcId": "r-x", "sessionId": "s-1"})
            mgr._on_question_resolved("dsh", {"rpcId": "r-x"})

        pending = mgr.pending_interactions_for("claude")
        assert len(pending) == 1, f"{kind}：跨 agent 的收尾帧不得关掉别人的常驻提醒"
        assert win.resolved == []
    finally:
        mgr.shutdown()


def test_identified_legacy_resolved_without_session_still_closes(tmp_path):
    """带身份、载荷缺 sessionId 的旧帧仍必须能关掉带 session 的登记（不误伤配对）。"""
    win = _MergingWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        mgr._on_approval_request("dsh", {"tool": "bash", "approvalId": "ap-1", "sessionId": "s-1"})
        (iid,) = tuple(mgr.pending_interactions_for("dsh"))
        mgr._on_approval_resolved("dsh", {"approvalId": "ap-1"})
        assert mgr.pending_interactions_for("dsh") == {}
        assert win.resolved == [f"interaction:{iid}"]
    finally:
        mgr.shutdown()


def test_rpc_scoped_resolved_does_not_match_other_identity_field(tmp_path):
    """rpcId 收尾帧不得被 approvalId/callId 上同值字符串误匹配（身份字段按类型匹配）。"""
    win = _MergingWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        mgr._on_approval_request("dsh", {"tool": "bash", "approvalId": "same", "sessionId": "s-1"})
        mgr._on_approval_request("dsh", {"tool": "bash", "rpcId": "same", "sessionId": "s-1"})
        assert len(mgr.pending_interactions_for("dsh")) == 2

        mgr._on_approval_resolved("dsh", {"rpcId": "same", "sessionId": "s-1"})

        remaining = list(mgr.pending_interactions_for("dsh").values())
        assert len(remaining) == 1
        assert remaining[0]["approval_id"] == "same" and not remaining[0]["rpc_id"]
    finally:
        mgr.shutdown()


# ======================================================================
# A06：pending 真值表有明确上限 + 淘汰政策 + 本地淘汰无副作用
# ======================================================================
def test_pending_interaction_capacity_is_explicit():
    """容量是**写明的产品常量**，且与显示队列（64）同量级、不随事件数增长。"""
    cap = AgentLinkManager._PENDING_INTERACTION_MAX
    assert isinstance(cap, int) and cap > 0
    assert cap == 256, "容量必须显式（本批取 256：4× 显示队列上限，仍是有界硬限）"
    assert cap >= 64, "不得低于显示 owner 的队列上限（否则本地真值先于呈现被淘汰）"


def test_pending_table_is_bounded_and_evicts_oldest_lowest(tmp_path):
    """超限时淘汰**最低优先级里最旧**的一条并留痕；本地呈现按同一 id 清理。"""
    win = _MergingWin()
    mgr, _mon = _build(tmp_path, win)
    cap = AgentLinkManager._PENDING_INTERACTION_MAX
    try:
        for index in range(cap + 5):
            mgr._on_approval_request("dsh", {"tool": "bash", "rpcId": f"r-{index}",
                                             "sessionId": "s-1"})

        pending = mgr.pending_interactions_for("dsh")
        assert len(pending) == cap, f"pending 真值表必须有界：{len(pending)}"
        r0 = _iid("approval", "dsh", "rpc_id", "r-0", "s-1")
        r4 = _iid("approval", "dsh", "rpc_id", "r-4", "s-1")
        assert r0 not in pending and r4 not in pending, \
            "被淘汰的必须是最旧的几条"
        assert _iid("approval", "dsh", "rpc_id", f"r-{cap + 4}", "s-1") in pending, \
            "最新登记必须保留"
        assert mgr._pending_interaction_overflow_count == 5, "淘汰必须留可诊断计数"
        # 淘汰只影响本地：显示 owner 按同一 alert_id 清掉那条（否则死提醒挂着）
        assert win.resolved.count(f"interaction:{r0}") == 1, \
            f"被淘汰项必须在显示 owner 上按同一 id 清理：{win.resolved[:3]}"
    finally:
        mgr.shutdown()


def test_resolved_of_locally_evicted_is_side_effect_free(tmp_path):
    """本地已淘汰项的 resolved 到达时是 no-op：不碰显示 owner、不复活、不写回上游。"""
    win = _MergingWin()
    mgr, _mon = _build(tmp_path, win)
    cap = AgentLinkManager._PENDING_INTERACTION_MAX
    try:
        for index in range(cap + 1):
            mgr._on_approval_request("dsh", {"tool": "bash", "rpcId": f"r-{index}",
                                             "sessionId": "s-1"})
        resolved_before = list(win.resolved)
        hidden_before = win.hidden_calls

        mgr._on_approval_resolved("dsh", {"rpcId": "r-0", "sessionId": "s-1"})

        assert mgr.pending_interactions_for("dsh").get(
            _iid("approval", "dsh", "rpc_id", "r-0", "s-1")) is None
        assert win.resolved == resolved_before, "淘汰项的收尾帧不得再次触动显示 owner"
        assert win.hidden_calls == hidden_before
    finally:
        mgr.shutdown()


def test_hint_ttl_expires_only_identity_less_entries(tmp_path):
    """TTL 只适用于**无身份**的降级提示：带身份的常驻状态不得被时间删掉。"""
    win = _MergingWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        hint_iid = mgr._register_interaction("dsh", kind="approval", text="降级提示")
        assert hint_iid and not mgr._pending_interactions[hint_iid].get("rpc_id")
        mgr._on_approval_request("dsh", {"tool": "bash", "rpcId": "keep", "sessionId": "s-1"})

        far_future = time.monotonic() + 100 * AgentLinkManager._PENDING_INTERACTION_HINT_TTL_S
        removed = mgr._prune_pending_interactions(now=far_future)

        assert removed == 1
        assert hint_iid not in mgr._pending_interactions, "无身份的降级提示按 TTL 收口"
        assert "approval:dsh:rpc_id:keep:s-1" in mgr._pending_interactions, \
            "带身份的常驻状态（长待批）不得被 TTL 随意删除"
        assert mgr.pending_interactions_for("dsh").get("approval:dsh:rpc_id:keep:s-1") is not None
    finally:
        mgr.shutdown()


def test_overflow_drops_locally_without_touching_the_source(tmp_path, monkeypatch):
    """洪泛下：本地表有界，桥接源文件逐字节不变，且没有任何远端批准/拒绝写回。"""
    win = _MergingWin()
    mgr, mon = _build(tmp_path, win)
    monkeypatch.setattr(harness_launcher, "is_running", lambda port: True)
    monkeypatch.setattr(harness_launcher, "desktop_process_running", lambda: True)
    cap = AgentLinkManager._PENDING_INTERACTION_MAX
    source = mon.events_dir / "dsh-1001.jsonl"
    try:
        for index in range(cap + 2):
            _write(mon.events_dir, "dsh-1001.jsonl",
                   {"event": "approval/request", "tool": "bash",
                    "rpcId": f"flood-{index}", "sessionId": "s-1"})
        before = source.read_bytes()
        mon._poll()
        after = source.read_bytes()

        assert after == before, "本地淘汰不得改动桥接源（不批准、不拒绝、不写回）"
        assert len(mgr.pending_interactions_for("dsh")) == cap
        assert mgr._pending_interaction_overflow_count == 2
    finally:
        mgr.shutdown()


# ======================================================================
# A05：隐藏/无宠期间到达的请求 → pending 真值保留 → 恢复可见补投
# ======================================================================
def test_hidden_request_stays_pending_and_surfaces_on_restore(tmp_path):
    """全部隐藏时到达的审批：真值留在 manager 表里，恢复可见后按同一 id 补投一次。"""
    win = _MergingWin(visible=False)
    mgr, mon, _proxy = _build_proxy(tmp_path, [win])
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "r-hid",
                "sessionId": "s-1"})
        mon._poll()

        pending = mgr.pending_interactions_for("dsh")
        assert len(pending) == 1, "隐藏期到达的请求必须进 pending 真值表"
        item = next(iter(pending.values()))
        assert item.get("delivered") is False, "没有可见呈现目标 → 不算已交付"
        assert [a["alert_id"] for a in win.alerts] == [f"interaction:{_iid('approval', 'dsh', 'rpc_id', 'r-hid', 's-1')}"], \
            "请求不得因为宿主不可见就被整条丢掉（要交给应用级 owner）"
        assert win.visible_by_id == {}, "不可见期间不上屏"

        win.show()
        mgr.resume()

        assert item.get("delivered") is True
        assert list(win.visible_by_id) == [f"interaction:{_iid('approval', 'dsh', 'rpc_id', 'r-hid', 's-1')}"], \
            f"恢复可见后必须补投同一 id，且只留一份：{win.visible_by_id}"

        # 已交付的条目不得在后续 resume 中重复投递
        delivered_calls = len(win.alerts)
        mgr.resume()
        mgr.resume()
        assert len(win.alerts) == delivered_calls, "已交付的常驻提醒不得重复入队"
    finally:
        mgr.shutdown()


def test_pending_request_surfaces_without_an_explicit_resume_call(tmp_path):
    """恢复路径不能只依赖 ``resume()``：真机上"全隐藏后重新生成宠物"等入口
    不经过它。有界自停的补投节拍必须在呈现目标回来后把同一 id 补上，并在交付完
    后停表（稳态零唤醒）。"""
    win = _MergingWin(visible=False)
    mgr, mon, _proxy = _build_proxy(tmp_path, [win])
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "r-timer",
                "sessionId": "s-1"})
        mon._poll()
        item = next(iter(mgr.pending_interactions_for("dsh").values()))
        assert item.get("delivered") is False
        assert win.visible_by_id == {}

        win.show()   # 宠物/整窗恢复可见——不调用 mgr.resume()

        assert _wait_until(
            lambda: win.visible_by_id.get(f"interaction:{_iid('approval', 'dsh', 'rpc_id', 'r-timer', 's-1')}"),
            timeout=6.0), "有界补投节拍必须把未交付的常驻交互补上"
        assert item.get("delivered") is True
        assert _wait_until(
            lambda: not mgr._pending_redeliver_timer.isActive(), timeout=6.0), \
            "交付完成后补投表必须自行停表（稳态零唤醒）"
    finally:
        mgr.shutdown()


def test_hidden_resolved_clears_pending_and_display(tmp_path):
    """无可见宠时的 resolved 仍必须真的删掉本地真值并按同一 id 清理显示 owner。"""
    win = _MergingWin(visible=False)
    mgr, mon, _proxy = _build_proxy(tmp_path, [win])
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "r-hid2",
                "sessionId": "s-1"})
        mon._poll()
        assert len(mgr.pending_interactions_for("dsh")) == 1

        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/resolved", "rpcId": "r-hid2", "sessionId": "s-1"})
        mon._poll()

        assert mgr.pending_interactions_for("dsh") == {}, "无宠时 resolved 也要收口"
        assert win.resolved == [f"interaction:{_iid('approval', 'dsh', 'rpc_id', 'r-hid2', 's-1')}"], \
            "显示 owner 必须按同一 id 收到关闭（不残留死提醒）"

        # 收口后恢复可见：不得复活已解决的提醒
        win.show()
        mgr.resume()
        assert win.visible_by_id == {}, "已 resolved 的提醒不得在恢复时复活"
    finally:
        mgr.shutdown()


def test_same_record_delivered_once_across_two_replays(tmp_path):
    """同一条记录被第二个桥写者重放：不重复登记、不重复投递、不重复正文。"""
    win = _MergingWin(visible=True)
    mgr, mon, _proxy = _build_proxy(tmp_path, [win])
    record = {"event": "approval/request", "tool": "bash", "rpcId": "r-once",
              "sessionId": "s-1"}
    try:
        _write(mon.events_dir, "dsh-1001.jsonl", record)
        mon._poll()
        calls_after_first = len(win.alerts)

        _write(mon.events_dir, "dsh-1002.jsonl", record)   # 第二个写者重放同一条
        mon._poll()

        assert len(mgr.pending_interactions_for("dsh")) == 1
        assert len(win.alerts) == calls_after_first, \
            f"重放不得再投一次正文：{win.alerts}"
        assert list(win.visible_by_id) == [f"interaction:{_iid('approval', 'dsh', 'rpc_id', 'r-once', 's-1')}"]
    finally:
        mgr.shutdown()


# ======================================================================
# 探活边界：共享桩只钉「在线」，真实离线口径由本用例覆盖
# ======================================================================
def test_offline_probe_override_clears_pending_and_stops_converging(tmp_path, monkeypatch):
    """钉回真实离线：共享桩不得盖住 offline 分支（它只表达「这台机器有 DSH」）。

    离线那一拍的可观测语义：offline 收敛撤掉屏上常驻提醒、清空本地 pending 真值，
    且此后的桥接记录不再进收敛器（``_on_parsed_record`` 早退）——这正是「记录被
    消费」要求探活在线的机制。
    """
    monkeypatch.setattr(harness_launcher, "is_running", lambda port: False)
    monkeypatch.setattr(harness_launcher, "desktop_process_running", lambda: False)
    win = _MergingWin()
    mgr, mon = _build(tmp_path, win)
    converged: list[tuple] = []
    mon.dsh_state_changed.connect(lambda *args: converged.append(args))
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "r-off",
                "sessionId": "s-1"})
        mon._poll()   # 第 1 拍不探活（_online 仍是 None）：记录照旧被消费
        # 收敛信号第 4 位是发出时的代次（F3：消费侧据此丢弃旧代次排队信号）
        assert ("", "waiting_approval", "approval/request", mon._emit_gen) in converged
        assert len(mgr.pending_interactions_for("dsh")) == 1

        mon._poll()   # 第 2 拍触发探活：钉成跑不通 ⇒ 真离线
        assert mon._online is False, "钉死的探活必须落到离线"
        assert ("waiting_approval", "offline", "", mon._emit_gen) in converged, \
            f"离线收敛必须发生：{converged}"
        assert mgr.pending_interactions_for("dsh") == {}, \
            "离线收敛清空本地 pending 真值（dismiss_all_interactions）"
        assert win.visible_by_id == {}, "离线必须把常驻提醒从屏上撤掉"

        converged.clear()
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "pwsh", "rpcId": "r-off2",
                "sessionId": "s-1"})
        mon._poll()
        assert converged == [], f"离线期间记录不得喂收敛器：{converged}"
    finally:
        mgr.shutdown()


# ======================================================================
# 线程边界：C++ 属主已析构时的发射必须停代/静默（不冒泡成后台未捕获异常）
# ======================================================================
def test_emit_retires_generation_when_signal_source_is_gone(tmp_path, monkeypatch):
    """信号源已随 C++ 属主析构：``_emit`` 必须停代并静默（debug 留痕），不抛。

    只作废 destroyed 兜底（真机上的竞态窗口：worker 首轮 ``_worker_started``
    与父链析构同时发生），让本用例只考察发射边界自身的行为。
    """
    monkeypatch.setattr(BaseAgentMonitor, "_destroyed_guard",
                        staticmethod(lambda mon: None))
    holder = QObject()
    mon = BaseAgentMonitor("dsh", tmp_path / "cfg")
    mon.setParent(holder)
    mon._POLL_INTERVAL_S = 0.05
    assert mon.start() is True
    worker = mon._worker
    assert worker is not None and worker.is_alive()

    del holder
    gc.collect()   # C++ 属主已析构，Python wrapper 仍在（worker 持有 bound method）

    mon._emit(mon.state_changed, ("dsh", "working"))   # 不得抛 RuntimeError

    assert mon._emit_gen == -1, "发射失效即作废本代次（不再向已销毁对象投递）"
    assert mon._running is False
    assert _wait_until(lambda: not worker.is_alive(), timeout=5.0), \
        "停代后 worker 必须自行退出"


def test_destroyed_worker_does_not_raise_out_of_thread(tmp_path, monkeypatch):
    """真实生命周期边界：父链析构正好落在 worker 首轮发射窗口内 → 线程内不得有
    未捕获异常（pytest 的 ``PytestUnhandledThreadExceptionWarning`` 即来自这里）。"""
    monkeypatch.setattr(harness_launcher, "is_running", lambda port: False)
    monkeypatch.setattr(harness_launcher, "desktop_process_running", lambda: False)
    cfg = Config(base=tmp_path)
    mgr = AgentLinkManager(None, cfg)
    mon = mgr.monitors["dsh"]
    mon._POLL_INTERVAL_S = 0.05
    entered = threading.Event()
    release = threading.Event()
    orig_probe = mon._probe_online

    def gated_probe():
        entered.set()
        assert release.wait(timeout=10.0), "测试释放信号未到达"
        return orig_probe()

    mon._probe_online = gated_probe
    uncaught: list = []
    original_hook = threading.excepthook
    threading.excepthook = lambda args: uncaught.append(args)
    try:
        assert mon.start() is True
        assert entered.wait(timeout=10.0), "worker 未进入首轮启动路径"
        worker = mon._worker
        del mgr
        gc.collect()          # C++ 属主析构（destroyed 兜底作废本代次）
        release.set()
        assert _wait_until(lambda: not worker.is_alive(), timeout=5.0)
    finally:
        release.set()
        threading.excepthook = original_hook

    assert uncaught == [], (
        "worker 线程不得抛出未捕获异常（发射边界必须停代/静默）："
        f"{[getattr(a.exc_value, '__class__', None).__name__ for a in uncaught]}")
