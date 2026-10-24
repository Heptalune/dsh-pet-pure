# -*- coding: utf-8 -*-
"""F02/A03 收尾：交互身份（来源 + kind + 会话 + 身份命名空间/值）必须**不碰撞**，
收尾帧只在**唯一候选**时才允许关闭。

两条残留（先红后绿，日志见 `.scratch/product-final-fix-resolved2-20261010/`）：

1. **缺 sessionId 的旧 resolved 帧 + 多候选**：A/B 两会话各有一条同 rpcId 的审批时，
   旧实现「取第一个匹配」先关一条，旧直通通道随后又看到「新的唯一候选」把另一条也
   关掉（一条记录关两条）。归属未知不得当成"任意一条都能删"：多候选一律不动，
   唯一候选仍按旧帧兼容关闭（缺 session 的旧桥不许把提醒变成关不掉的常驻）。
2. **三来源同 kind/rpcId/session 登记互相覆盖**：存储键只有 ``kind:rpcId``，撞键后
   补一个会话后缀只是把覆盖推迟到第三条（DSH/Claude/自定义源依次登记后只剩两条，
   匹配前事实已经丢了）。键必须**先**写进完整身份（来源 + kind + 会话 + 身份命名
   空间/值）——这正是规范 §三「去重键 = 来源 + 会话 + 逻辑事件标识」；键分量转义后
   拼接，对身份是单射（不靠时间/文本 hash 兜，不做"同文本去重"）。

本文件走**真实摄入路径**：真 ``DshMonitor``（真 tailer）读 ``tmp_path`` 里的 JSONL →
真 ``AgentLinkManager`` → 假窗口 spy（``show_alert``/``resolve_alert`` 按 alert_id
记录）。其它来源走各自监视器的真实 ``approval_requested`` / ``approval_resolved``
信号（manager 构造时接的就是这两个槽），不是绕过接线直接调 manager 私有方法。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet import harness_launcher  # noqa: E402
from pet.agent_link import AgentLinkManager  # noqa: E402
from pet.config import Config  # noqa: E402

app = QApplication.instance() or QApplication([])

SOURCES = ("dsh", "claude", "cursor")


class _AlertWin:
    """提醒宿主替身：按 ``alert_id`` 记录投递与关闭（与真实 owner 同语义）。"""

    def __init__(self) -> None:
        self.alerts: dict[str, str] = {}
        self.resolved: list[str] = []
        self.bubbles: list[str] = []
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

    def isVisible(self) -> bool:  # noqa: N802
        return True

    def show_alert(self, text, *, subtitle="", duration_ms=0, buttons=None,
                   sticky=True, alert_id="", priority=0, alert_type="",
                   metadata=None) -> None:
        if alert_id:
            self.alerts[str(alert_id)] = str(text)

    def resolve_alert(self, alert_id) -> None:
        self.resolved.append(str(alert_id))
        self.alerts.pop(str(alert_id), None)

    def show_bubble(self, text, duration_ms=3200, **_kw) -> None:
        self.bubbles.append(str(text))

    def clear_alerts(self) -> None:
        self.cleared += 1
        self.alerts.clear()

    def hide_bubble(self) -> None:
        pass

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
    （离线口径的反向回归只在 ``test_final_interaction_regressions.py`` 保留一条。）
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
    # 本文件不测扫描节流，所以把它关掉（scan_interval=0 即「每拍全量枚举」，是产品合法
    # 取值，见 test_agent_link.py::test_scan_interval_elapsed_rescans_without_directory_change）。
    # 原因：节流下的「新文件当拍发现」压在**目录 mtime 变化**上，而那不是确定前提——
    # 建文件后目录时间戳在 Windows 上惰性更新（本机实测：40 次里只有 22-33 次读得回变化，
    # 仓库既有探针实测 25/50），低精度文件系统上整拍都不变，新文件只能等 scan_interval
    # （默认 5s）兜底。本文件要测的是身份 / 收尾语义，不该赌文件系统时序；节流契约本身
    # 由 test_agent_link.py::TestDirGlobTailer 那一族负责。
    mon._tailer.scan_interval = 0.0
    return mgr, mon


def _request(mgr, source: str, *, rpc_id: str, session_id: str, tool: str = "bash") -> None:
    """按该来源的真实接线登记一条审批请求（dsh 走真 JSONL + 真 ``_poll``）。"""
    payload = {"event": "approval/request", "tool": tool, "rpcId": rpc_id,
               "sessionId": session_id}
    if source == "dsh":
        _write(mgr.monitors["dsh"].events_dir, "dsh-1001.jsonl", payload)
        mgr.monitors["dsh"]._poll()
    else:
        mgr.monitors[source].approval_requested.emit(source, payload)


def _resolve(mgr, source: str, payload: dict) -> None:
    """按该来源的真实接线投递一条收尾帧（dsh 走真 JSONL + 真 ``_poll``）。"""
    if source == "dsh":
        _write(mgr.monitors["dsh"].events_dir, "dsh-1001.jsonl", payload)
        mgr.monitors["dsh"]._poll()
    else:
        mgr.monitors[source].approval_resolved.emit(source, payload)


def _pending(mgr, agent_key: str = "dsh") -> dict:
    return mgr.pending_interactions_for(agent_key)


def _sessions_of(mgr, agent_key: str = "dsh") -> list[str]:
    return sorted(str(item.get("session_id")) for item in _pending(mgr, agent_key).values())


# ======================================================================
# 1. 三来源 × 两会话：同 kind/rpcId/session 不再互相覆盖
# ======================================================================
def test_three_sources_two_sessions_same_rpc_id_stay_distinct(tmp_path):
    """三个来源 × 两个会话登记同一 ``kind/rpcId``：六条各自独立，谁也不覆盖谁。

    旧实现：键只有 ``kind:rpcId``，撞键后补会话后缀 → 第二条来源复用第三条的键，
    第三条直接把第二条覆盖掉（登记后只剩两条，来源集合只剩 {dsh, cursor}）。
    身份在登记那一刻就丢了，收尾匹配再精确也救不回来。
    """
    win = _AlertWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        for session in ("S1", "S2"):
            for source in SOURCES:
                _request(mgr, source, rpc_id="same", session_id=session)

        pending = mgr._pending_interactions
        assert len(pending) == 6, (
            f"3 来源 × 2 会话必须各占一条，实际 {len(pending)}：{sorted(pending)}")
        pairs = {(item["agent_key"], item["session_id"]) for item in pending.values()}
        assert pairs == {(source, session) for source in SOURCES
                         for session in ("S1", "S2")}, f"身份组合不完整：{pairs}"
        assert len(set(pending)) == 6, "键必须互不相同（无覆盖）"
        for item in pending.values():
            assert "same" in item["alert_id"], f"键要含源身份值：{item['alert_id']}"
    finally:
        mgr.shutdown()


def test_resolved_reaches_only_its_own_source_and_session(tmp_path):
    """逐条收尾：每个 (来源, 会话) 只关自己那条，别人那条必须还在。

    dsh 的收尾走真 JSONL + 真 ``_poll``（同一条记录会同时喂语义层与旧直通通道），
    其它来源走各自监视器的真实 ``approval_resolved`` 信号。
    """
    win = _AlertWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        for session in ("S1", "S2"):
            for source in SOURCES:
                _request(mgr, source, rpc_id="same", session_id=session)

        _resolve(mgr, "claude", {"rpcId": "same", "sessionId": "S1"})
        assert _sessions_of(mgr, "claude") == ["S2"], "claude/S1 只许关掉自己那条"
        assert len(mgr._pending_interactions) == 5
        assert _sessions_of(mgr, "dsh") == ["S1", "S2"], "别的来源那条不得被误关"

        _resolve(mgr, "dsh", {"event": "approval/resolved", "rpcId": "same",
                               "sessionId": "S2"})
        assert _sessions_of(mgr, "dsh") == ["S1"], "dsh/S2 只许关掉自己那条"
        assert len(mgr._pending_interactions) == 4
        assert win.resolved and len(set(win.resolved)) == 2, (
            f"两次收尾各按自己的 alert_id 关闭：{win.resolved}")
    finally:
        mgr.shutdown()


# ======================================================================
# 2. 缺 sessionId 的旧帧 + 多候选：不动任何一条
# ======================================================================
def test_session_less_resolved_with_two_candidates_closes_nothing(tmp_path, caplog):
    """A/B 同 rpcId，旧帧不带 sessionId（归属未知）：一条都不许关。

    旧实现「取第一个匹配」先关 A，旧直通通道随后把 B 也关掉——用户还在等的那条
    提醒被收掉（复现见 astra ``followup_probe.py`` 的 ``resolved_without_session``：
    修复前 remaining=[] / closed=2）。
    """
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "shared", "sessionId": "A"},
               {"event": "approval/request", "tool": "bash", "rpcId": "shared", "sessionId": "B"})
        mon._poll()
        assert _sessions_of(mgr) == ["A", "B"]

        with caplog.at_level(logging.WARNING, logger="dsh-pet-standalone"):
            _write(mon.events_dir, "dsh-1001.jsonl",
                   {"event": "approval/resolved", "rpcId": "shared"})
            mon._poll()

        assert _sessions_of(mgr) == ["A", "B"], \
            f"归属未知时多候选一律不动，实际剩余 {_sessions_of(mgr)}"
        assert win.resolved == [], f"显示 owner 不得收到任何关闭：{win.resolved}"
        assert "候选" in caplog.text and "不批准" in caplog.text, \
            f"歧义必须明确留痕（日志里说清不动任何一条）：{caplog.text!r}"
    finally:
        mgr.shutdown()


def test_three_candidates_session_less_resolved_closes_nothing(tmp_path):
    """三个会话同 rpcId：旧帧既不能任取一条，第二条通道也不能再关下一条。"""
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        for session in ("A", "B", "C"):
            _write(mon.events_dir, "dsh-1001.jsonl",
                   {"event": "approval/request", "tool": "bash",
                    "rpcId": "shared", "sessionId": session})
        mon._poll()
        assert len(_pending(mgr)) == 3

        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/resolved", "rpcId": "shared"})
        mon._poll()

        assert len(_pending(mgr)) == 3, f"三条候选都必须在：{sorted(_pending(mgr))}"
        assert win.resolved == []
    finally:
        mgr.shutdown()


def test_session_less_resolved_with_single_candidate_still_closes(tmp_path):
    """旧帧兼容不能破：该来源+身份下**只有一条**候选时，缺 session 的收尾帧仍关得掉。"""
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash",
                "rpcId": "only", "sessionId": "A"})
        mon._poll()
        (iid,) = tuple(_pending(mgr))

        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/resolved", "rpcId": "only"})
        mon._poll()

        assert _pending(mgr) == {}, "唯一候选：缺 session 的旧帧必须照旧关得掉"
        assert win.resolved == [f"interaction:{iid}"], win.resolved
    finally:
        mgr.shutdown()


# ======================================================================
# 3. 多轮：歧义 → 唯一候选过渡（含"第二通道不追加关闭"）
# ======================================================================
def test_unique_candidate_transition_over_rounds(tmp_path):
    """一条身份在多轮收尾帧下的完整口径：

    r1 缺 session 的 ``approval/resolved``（多候选）→ 0 关；
    r2 同 id 的另一条记录（``approval/decided``，仍缺 session）→ 0 关（歧义没变）；
    r3 带 session=A 的收尾 → 只关 A（1 关，第二条通道对同一记录 no-op）；
    r4 缺 session 的第三通道记录（``user_action``）→ 此时唯一候选，兼容关闭 B（累计 2 关）；
    r5 陈旧的 ``interaction/resolved`` → 无候选，no-op。

    每一步都同时校验「关了几条」与「谁还在」：一条记录只允许消费一次，且第二次
    通道不得在候选集合缩小后把**另一条**当成新唯一候选关掉。
    """
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        for session in ("A", "B"):
            _write(mon.events_dir, "dsh-1001.jsonl",
                   {"event": "approval/request", "tool": "bash",
                    "rpcId": "r", "sessionId": session})
        mon._poll()
        assert _sessions_of(mgr) == ["A", "B"]

        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/resolved", "rpcId": "r"})
        mon._poll()
        assert (_sessions_of(mgr), len(win.resolved)) == (["A", "B"], 0), "r1 歧义不动"

        _write(mon.events_dir, "dsh-1002.jsonl",
               {"event": "approval/decided", "rpcId": "r"})
        mon._poll()
        assert (_sessions_of(mgr), len(win.resolved)) == (["A", "B"], 0), "r2 仍歧义"

        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/resolved", "rpcId": "r", "sessionId": "A"})
        mon._poll()
        assert (_sessions_of(mgr), len(win.resolved)) == (["B"], 1), "r3 只关 A 一条"

        _write(mon.events_dir, "dsh-1002.jsonl",
               {"event": "user_action", "action": "approval_decided", "rpcId": "r"})
        mon._poll()
        assert (_sessions_of(mgr), len(win.resolved)) == ([], 2), "r4 唯一候选兼容关闭"

        _write(mon.events_dir, "dsh-1002.jsonl",
               {"event": "interaction/resolved", "kind": "approval", "rpcId": "r",
                "sessionId": "A"})
        mon._poll()
        assert (_sessions_of(mgr), len(win.resolved)) == ([], 2), "r5 陈旧帧 no-op"
    finally:
        mgr.shutdown()


# ======================================================================
# 4. 身份命名空间：同值不同字段不是同一条
# ======================================================================
def test_identity_namespaces_get_distinct_keys_and_closes(tmp_path):
    """同来源同会话下 ``rpcId="same"`` 与 ``approvalId="same"`` 是两条身份。

    键必须带身份**命名空间**（字段名）：只按值拼键会让两条同值不同字段的交互在
    存储层就撞在一起；收尾帧也只许按同字段关闭（rpcId 帧不碰 approvalId 条目）。
    """
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "approvalId": "same",
                "sessionId": "s-1"})
        mon._poll()
        _write(mon.events_dir, "dsh-1002.jsonl",
               {"event": "approval/request", "tool": "pwsh", "rpcId": "same",
                "sessionId": "s-1"})
        mon._poll()

        pending = _pending(mgr)
        assert len(pending) == 2, f"两条身份必须各占一条：{sorted(pending)}"
        assert len(set(pending)) == 2, "键必须互不相同（含身份命名空间）"

        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/resolved", "rpcId": "same", "sessionId": "s-1"})
        mon._poll()

        remaining = list(_pending(mgr).values())
        assert len(remaining) == 1, "rpcId 收尾只关 rpcId 那条"
        assert remaining[0]["approval_id"] == "same" and not remaining[0]["rpc_id"]
    finally:
        mgr.shutdown()


def test_same_value_different_sources_never_share_a_key(tmp_path):
    """同值同上名字段但来源不同：键必须分开（来源是身份的组成部分）。"""
    win = _AlertWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        for source in SOURCES:
            _request(mgr, source, rpc_id="v", session_id="s-1")
        keys = list(mgr._pending_interactions)
        assert len(keys) == 3 and len(set(keys)) == 3, f"三来源键互不相同：{keys}"
        for source in SOURCES:
            assert len(_pending(mgr, source)) == 1, f"{source} 只许有一条自己的"
    finally:
        mgr.shutdown()


def test_resolution_without_a_source_never_wildcards(tmp_path):
    """来源未知的收尾帧不得通配任何来源（来源是精确判据，不是可选上下文）。"""
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "x",
                "sessionId": "s-1"})
        mon._poll()
        assert len(_pending(mgr)) == 1

        mgr._on_approval_resolved("", {"rpcId": "x", "sessionId": "s-1"})
        assert len(_pending(mgr)) == 1, "空来源不得关掉 dsh 的交互"
        assert win.resolved == []
    finally:
        mgr.shutdown()


# ======================================================================
# 5. 更新路径：同一交互的重投递不改 id、不加条
# ======================================================================
def test_alert_id_stays_stable_across_redelivery(tmp_path):
    """同一逻辑交互（含"首帧带会话、重投递帧缺会话"）必须复用同一键与 alert_id。

    键进身份是为了不碰撞，不是为了"每次登记都换一个 id"：更新路径按身份就地复用，
    否则窗口按 alert_id 合并的语义会被打散（同一提醒会出现第二份可见正文）。
    """
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "r-1",
                "sessionId": "s-1"})
        mon._poll()
        (iid,) = tuple(_pending(mgr))
        alert_id = mgr._pending_interactions[iid]["alert_id"]

        # 第二个写者重放同一条（不同文件）：仍是同一条、同一个 alert_id
        _write(mon.events_dir, "dsh-1002.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "r-1",
                "sessionId": "s-1"})
        mon._poll()
        assert list(_pending(mgr)) == [iid], "重放不得新开第二条"
        assert mgr._pending_interactions[iid]["alert_id"] == alert_id

        # 缺 session 的旧帧重投递（同一 rpcId）：仍是同一条，不是第二条身份
        mgr._on_approval_request("dsh", {"tool": "bash", "rpcId": "r-1"})
        assert list(_pending(mgr)) == [iid], "会话未知的重投递必须就地更新"
        assert mgr._pending_interactions[iid]["alert_id"] == alert_id
        assert win.alerts and list(win.alerts) == [alert_id], \
            f"同一提醒只允许一份可见正文：{win.alerts}"
    finally:
        mgr.shutdown()


# ======================================================================
# 6. 无身份旧路径兜底不破（同样只在唯一候选时关）
# ======================================================================
def test_identity_less_fallback_keeps_unique_only_semantics(tmp_path):
    """无任何身份的旧路径兜底：唯一一条无 rpcId 的提示才关，多候选不动。"""
    win = _AlertWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        assert mgr._register_interaction("dsh", kind="approval", text="降级提示") is not None
        assert mgr._register_interaction("dsh", kind="approval", text="降级提示二") is not None
        assert len(_pending(mgr)) == 2

        mgr._on_approval_resolved("dsh", {})
        assert len(_pending(mgr)) == 2, "两条无身份提示：多候选不得任取一条"
        assert win.resolved == []

        first = next(iter(_pending(mgr)))
        mgr._resolve_interaction(first)
        mgr._on_approval_resolved("dsh", {})
        assert _pending(mgr) == {}, "剩唯一一条时旧路径兜底仍照旧关闭"
    finally:
        mgr.shutdown()


@pytest.mark.parametrize("kind", ["approval", "question"])
def test_identity_less_fallback_is_scoped_to_its_source(tmp_path, kind):
    """无身份兜底也按来源隔离：别的 agent 的空帧不得关本 agent 的提示。"""
    win = _AlertWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        assert mgr._register_interaction("claude", kind=kind, text="提示") is not None
        if kind == "approval":
            mgr._on_approval_resolved("dsh", {})
        else:
            mgr._on_question_resolved("dsh", {})
        assert len(_pending(mgr, "claude")) == 1, "跨来源的空帧不得误关"
        assert win.resolved == []
    finally:
        mgr.shutdown()
# ======================================================================
# 7. 兼容通道（user_action / cordis）必须携带来源 + 同字段匹配
# ======================================================================
def _user_action(action: str, **ids) -> dict:
    return {"event": "user_action", "action": action, **ids}


def test_user_action_record_closes_only_its_own_source(tmp_path):
    """astra 复现（真 ``_poll``）：DSH 的 user_action 收尾只许关 dsh 自己那条。

    `user_action` 记录同时喂语义层与旧直通通道（``_on_user_action``）。语义层按
    来源精确关掉 dsh 之后，旧通道若不携带来源，就只剩 claude 一个候选而把它也
    关掉——一条记录跨来源误关另一条仍在等待的审批（复现见
    `.scratch/product-final-review-astra-20261010/followup2_probe.py`：
    修复前 remaining=[] / closed=2，应为 remaining=[(claude,S)] / closed=1）。

    只登记两个来源（不是三个）：三来源时旧通道会因"多候选"侥幸不动，正好盖住
    这个缺陷——跨来源误关发生在"删掉第一条后只剩唯一错误候选"的那一拍。
    """
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        for source in ("dsh", "claude"):
            _request(mgr, source, rpc_id="same", session_id="S")
        assert len(mgr._pending_interactions) == 2

        _write(mon.events_dir, "dsh-1001.jsonl",
               _user_action("approval_resolved", rpcId="same", sessionId="S"))
        mon._poll()

        remaining = [(item["agent_key"], item["session_id"])
                     for item in mgr._pending_interactions.values()]
        assert remaining == [("claude", "S")], (
            f"只许关掉 dsh 自己那条，实际剩余 {remaining}")
        assert len(win.resolved) == 1, f"显示 owner 只许收到一次关闭：{win.resolved}"
    finally:
        mgr.shutdown()


def test_user_action_three_sources_leaves_the_other_two(tmp_path):
    """三来源同会话：dsh 的 user_action 之后，另两条必须都还在（只按来源判定）。"""
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        for source in SOURCES:
            _request(mgr, source, rpc_id="same", session_id="S")
        assert len(mgr._pending_interactions) == 3

        _write(mon.events_dir, "dsh-1001.jsonl",
               _user_action("approval_resolved", rpcId="same", sessionId="S"))
        mon._poll()

        remaining = sorted((item["agent_key"], item["session_id"])
                           for item in mgr._pending_interactions.values())
        assert remaining == [("claude", "S"), ("cursor", "S")], \
            f"只许关掉 dsh 自己那条，实际剩余 {remaining}"
        assert len(win.resolved) == 1, f"显示 owner 只许收到一次关闭：{win.resolved}"
    finally:
        mgr.shutdown()


def test_user_action_across_three_sources_and_two_sessions(tmp_path):
    """3 来源 × 2 会话共 6 条，两条 user_action 记录各只关自己那一条。"""
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        for session in ("S1", "S2"):
            for source in SOURCES:
                _request(mgr, source, rpc_id="same", session_id=session)
        assert len(mgr._pending_interactions) == 6

        _write(mon.events_dir, "dsh-1001.jsonl",
               _user_action("approval_decided", rpcId="same", sessionId="S1"))
        mon._poll()
        pairs = {(item["agent_key"], item["session_id"])
                 for item in mgr._pending_interactions.values()}
        assert ("dsh", "S1") not in pairs and len(pairs) == 5, f"只关 dsh/S1：{sorted(pairs)}"
        assert ("claude", "S1") in pairs and ("cursor", "S1") in pairs, \
            "同会话的其它来源不得被连带关掉"
        assert len(win.resolved) == 1

        _write(mon.events_dir, "dsh-1002.jsonl",
               _user_action("approval_resolved", rpcId="same", sessionId="S2"))
        mon._poll()
        pairs = {(item["agent_key"], item["session_id"])
                 for item in mgr._pending_interactions.values()}
        assert pairs == {("claude", "S1"), ("cursor", "S1"),
                         ("claude", "S2"), ("cursor", "S2")}, f"只关 dsh/S2：{sorted(pairs)}"
        assert len(win.resolved) == 2
    finally:
        mgr.shutdown()


def test_user_action_identity_namespaces_do_not_cross(tmp_path):
    """同值不同命名空间不是同一条：rpcId 帧不碰 approvalId/callId 条目，反之亦然。

    旧兼容通道把 ``rpcId`` 与 ``requestId``、``approvalId`` 与 ``callId`` 合并成
    互通匹配（"同值即可"），一个字段的收尾帧会关掉另一个字段上的交互。
    """
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        mgr._on_approval_request("dsh", {"tool": "bash", "rpcId": "same", "sessionId": "S"})
        mgr._on_approval_request("dsh", {"tool": "bash", "approvalId": "same", "sessionId": "S"})
        mgr._on_approval_request("dsh", {"tool": "bash", "callId": "same", "sessionId": "S"})
        mgr._on_approval_request("dsh", {"tool": "bash", "requestId": "same", "sessionId": "S"})
        assert len(mgr._pending_interactions) == 4, sorted(mgr._pending_interactions)

        # rpcId 帧：只关 rpcId 那条
        _write(mon.events_dir, "dsh-1001.jsonl",
               _user_action("approval_resolved", rpcId="same", sessionId="S"))
        mon._poll()
        left = {item.get("rpc_id") or item.get("approval_id") or item.get("call_id")
                or item.get("request_id") for item in mgr._pending_interactions.values()}
        assert None not in left, f"不该有关键字段全空的条目：{left}"
        kinds_left = sorted(_field_of(item) for item in mgr._pending_interactions.values())
        assert kinds_left == ["approval_id", "call_id", "request_id"], \
            f"rpcId 帧只许关 rpcId 那条：{kinds_left}"

        # approvalId 帧：只关 approvalId 那条
        _write(mon.events_dir, "dsh-1002.jsonl",
               _user_action("approval_decided", approvalId="same", sessionId="S"))
        mon._poll()
        kinds_left = sorted(_field_of(item) for item in mgr._pending_interactions.values())
        assert kinds_left == ["call_id", "request_id"], f"approvalId 帧只许关同字段：{kinds_left}"

        # callId 帧（question 语义）不得关掉 approval 上的 callId 条目
        _write(mon.events_dir, "dsh-1002.jsonl",
               _user_action("question_resolved", callId="same", sessionId="S"))
        mon._poll()
        kinds_left = sorted(_field_of(item) for item in mgr._pending_interactions.values())
        assert kinds_left == ["call_id", "request_id"], \
            f"question 收尾不得关 approval 条目：{kinds_left}"
    finally:
        mgr.shutdown()


def _field_of(item: dict) -> str:
    for field in ("rpc_id", "approval_id", "call_id", "request_id"):
        if item.get(field):
            return field
    return "?"


def test_user_action_without_session_is_ambiguous_not_arbitrary(tmp_path):
    """兼容通道同样受"多候选不动"约束：缺 session 的旧帧不能任取一条关掉。"""
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        for source in SOURCES:
            _request(mgr, source, rpc_id="same", session_id="S1")
            _request(mgr, source, rpc_id="same", session_id="S2")
        assert len(mgr._pending_interactions) == 6

        _write(mon.events_dir, "dsh-1001.jsonl",
               _user_action("approval_resolved", rpcId="same"))
        mon._poll()

        assert len(mgr._pending_interactions) == 6, \
            "dsh 自己那两条会话也无法判定：一条都不许关"
        assert win.resolved == []
    finally:
        mgr.shutdown()


def test_user_action_without_a_source_never_closes_anything(tmp_path):
    """来源未知的兼容收尾必须拒绝，不许通配别的来源。"""
    win = _AlertWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        _request(mgr, "dsh", rpc_id="same", session_id="S")
        mgr._on_user_action("", _user_action("approval_resolved", rpcId="same", sessionId="S"))
        assert len(mgr._pending_interactions) == 1, "空来源的 user_action 不得关 dsh 的条目"
        assert win.resolved == []
    finally:
        mgr.shutdown()


def test_user_action_identity_less_legacy_fallback_still_unique_only(tmp_path):
    """无 id 的旧 user_action：只在该来源唯一一条无身份提示时才关（不任取）。"""
    win = _AlertWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        assert mgr._register_interaction("dsh", kind="approval", text="降级提示") is not None
        assert mgr._register_interaction("claude", kind="approval", text="别人的提示") is not None

        mgr._on_user_action("dsh", _user_action("approval_decided"))
        assert _pending(mgr) == {}, "该来源唯一一条无身份提示：旧入口兼容关闭"
        assert len(_pending(mgr, "claude")) == 1, "不得跨来源"

        assert mgr._register_interaction("claude", kind="approval", text="提示一") is not None
        assert mgr._register_interaction("claude", kind="approval", text="提示二", rpc_id="r") is not None
        before = len(win.resolved)
        mgr._on_user_action("claude", _user_action("approval_resolved"))
        pending = _pending(mgr, "claude")
        identity_less = [iid for iid, item in pending.items() if not item.get("rpc_id")]
        assert len(identity_less) == 2, f"两条无身份候选：{sorted(pending)}"
        assert len(pending) == 3, "多候选无身份帧不得任取一条（带 rpcId 的也不许被兜底吃掉）"
        assert len(win.resolved) == before, "没有任何一条应被关闭"
        assert "approval:claude:rpc_id:r" in pending, "带身份的条目不受无身份兜底影响"
    finally:
        mgr.shutdown()


def test_cordis_resolved_closes_only_its_own_source(tmp_path):
    """cordis 收尾同样按来源：dsh 的 ``request-run-resolved`` 不得关 claude 的同 id 条目。"""
    win = _AlertWin()
    mgr, mon = _build(tmp_path, win)
    try:
        # claude 侧走真实 handler 登记；dsh 侧走真 JSONL + 真 _poll
        mgr._on_cordis_request("claude", {"name": "插件", "purpose": "运行",
                                          "requiresApproval": True, "requestId": "req-1"})
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "cordis/request-run", "requestId": "req-1", "agentId": "sess-9",
                "requiresApproval": True, "payload": {"name": "插件", "purpose": "运行"}})
        mon._poll()
        assert len(_pending(mgr, "dsh")) == 1 and len(_pending(mgr, "claude")) == 1

        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "cordis/request-run-resolved", "requestId": "req-1",
                "agentId": "sess-9"})
        mon._poll()

        assert _pending(mgr, "dsh") == {}, "dsh 自己的 cordis 交互应关闭"
        assert len(_pending(mgr, "claude")) == 1, "claude 的同 id 交互不得被跨来源关掉"
        assert len(win.resolved) == 1
    finally:
        mgr.shutdown()


def test_cordis_resolved_legacy_entry_points(tmp_path):
    """只有旧入口直发（不经 monitor）时的 cordis 口径：按来源、按唯一候选。"""
    win = _AlertWin()
    mgr, _mon = _build(tmp_path, win)
    try:
        mgr._on_cordis_request("dsh", {"name": "插件", "purpose": "运行",
                                       "requiresApproval": True, "requestId": "req-1"})
        mgr._on_cordis_request("claude", {"name": "插件", "purpose": "运行",
                                          "requiresApproval": True, "requestId": "req-1"})
        assert len(mgr._pending_interactions) == 2

        mgr._on_cordis_resolved("claude", {"requestId": "req-1"})
        assert _pending(mgr, "claude") == {}, "claude 的收尾应只关 claude 自己那条"
        assert len(_pending(mgr, "dsh")) == 1, "dsh 的同 id 交互不得被关"

        # 未知 id / 空来源：不得任取
        mgr._on_cordis_resolved("dsh", {"requestId": "req-unknown"})
        mgr._on_cordis_resolved("", {"requestId": "req-1"})
        assert len(_pending(mgr, "dsh")) == 1, "未知 id / 空来源的 cordis 收尾不得误关"

        mgr._on_cordis_resolved("dsh", {"requestId": "req-1", "agentId": "sess-9"})
        assert _pending(mgr, "dsh") == {}, "唯一候选 + 旧帧（会话未知）仍兼容关闭"
    finally:
        mgr.shutdown()
