"""G2 事件标识与单次声响 / 不重放（端到端）。

规范 §三「全局事件、提醒与去重」的机器验收：**同一逻辑事件只允许一次正文落点、
至多一次提示音**；去重键 = 来源 + 会话 + 逻辑事件标识（有源 ID 时），无源 ID 时
用**读取游标 + 运行代次**；缓存有界且关监测清理；**不按文本去重**；显式用户冷却
照旧可以静音新事件。

本文件走**真实摄入路径**：真 ``DshMonitor``（真 ``DirGlobTailer`` /
``ByteOffsetTailer`` / 真 ``DshStateConverger``）读真实桥目录里的小 JSONL →
真 ``AgentLinkManager`` → 假窗口 spy 正文（``show_bubble`` / ``show_alert`` 按
alert_id 合并，与真实 ``OverlayShell.show_alert`` 同语义）与音效 spy（不真实发声）。
只有"直接调 ``_emit_sound``"这类单元级断言是不够的——R2 只覆盖冷却窗，
证明不了**重复源事件跨冷却**不重播。

红→绿口径：本文件在补上摄入去重账本与交互重投递就地更新之前必须红（见
``.scratch/product-g2-events-final-20261010/`` 的 red/green 日志与消融副本）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet import agent_link, click_sound, harness_launcher  # noqa: E402
from pet.agent_link import AgentLinkManager, EventIdentityLedger  # noqa: E402
from pet.config import Config  # noqa: E402

app = QApplication.instance() or QApplication([])

_SOUND_PATHS = {
    "builtin:agent-start": "start.wav",
    "builtin:agent-done": "done.wav",
    "builtin:agent-error": "error.wav",
}


class FakeWin:
    """假窗口：正文经 ``show_bubble``/``show_alert`` 落到 spy 列表。

    ``show_alert`` 按 ``alert_id`` 合并（同一 id 就地更新文本，不再入队）——
    与真实 ``OverlayShell.show_alert``（R7 ``test_r7_same_id_merges``）同语义：
    同一个提醒 id 最多一份可见。
    """

    _bubble_busy_until = 0.0
    _sticky_bubble_active = False
    cats = {"acts": ["写代码", "吃Token"]}
    idles = ["待机呼吸"]

    def __init__(self) -> None:
        self.bubbles: list[str] = []
        self.alerts: list[dict] = []
        self._alert_current = None
        self._alert_queue: list[dict] = []

    def isVisible(self) -> bool:  # noqa: N802
        return True

    def mark_activity(self) -> None:
        pass

    def request_link_anim(self, name) -> None:
        pass

    def request_link_idle(self) -> None:
        pass

    def show_bubble(self, text, duration_ms=3000, **_kw) -> None:
        self.bubbles.append(str(text))

    def show_alert(self, text, *, subtitle="", duration_ms=0, buttons=None,
                   sticky=True, alert_id="", priority=0, alert_type="",
                   metadata=None) -> None:
        text = str(text)
        if alert_id:
            for item in [self._alert_current, *self._alert_queue]:
                if item is not None and item.get("id") == alert_id:
                    item["text"] = text          # 同 id 合并/更新，不入队
                    return
        item = {"id": alert_id, "text": text, "sticky": sticky}
        self.alerts.append(item)
        self._alert_queue.append(item)
        if self._alert_current is None:
            self._alert_current = self._alert_queue.pop(0)

    def resolve_alert(self, alert_id) -> None:
        if self._alert_current and self._alert_current.get("id") == alert_id:
            self._alert_current = None
            if self._alert_queue:
                self._alert_current = self._alert_queue.pop(0)
        else:
            self._alert_queue = [q for q in self._alert_queue if q.get("id") != alert_id]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """真 manager（真 DshMonitor，worker 不启动、手动驱动 _poll）+ 假窗口 + spy。"""
    sounds: list[str] = []

    for value, name in _SOUND_PATHS.items():
        (tmp_path / name).write_bytes(b"RIFF")

    monkeypatch.setattr(agent_link, "resolve_builtin_sound",
                        lambda p: (tmp_path / _SOUND_PATHS[str(p)])
                        if str(p) in _SOUND_PATHS else None)
    # 音效钉在播放漏斗层（winmm 直放 / Qt 后端都逃不掉）
    for fn in ("play_sound", "play_with_winmm", "play_with_qt"):
        monkeypatch.setattr(click_sound._pool, fn,
                            lambda path, volume=1.0: sounds.append(Path(path).name) or True)
    monkeypatch.setattr(harness_launcher, "is_running", lambda port: True)
    monkeypatch.setattr(harness_launcher, "desktop_process_running", lambda: True)

    cfg = Config(base=tmp_path / "cfg")
    # 注意**不**打开 agent_link.dsh：打开会让 apply_config 真的启动监视器 worker
    # （与手动驱动 _poll 的测试争抢 tailer、并把代次推到 1）。白盒置 _running=True
    # 即等效"通道已启用"，见 build()。
    cfg.data["agent_link"].update({
        "sound_enabled": True,
        "sound_cooldown_seconds": 2.0,
        "sound_start_path": "builtin:agent-start",
        "sound_done_path": "builtin:agent-done",
        "sound_error_path": "builtin:agent-error",
    })
    cfg.save()

    clock = [1000.0]

    def build(*, prefill: bool = True):
        win = FakeWin()
        mgr = AgentLinkManager(win, cfg, min_interval=0.0, clock=lambda: clock[0])
        mon = mgr.monitors["dsh"]
        # 白盒：等效 agent_link.dsh 已启用——迟到信号隔离闸只放行「监视器在跑」
        # 的信号，手动驱动 _poll 的测试必须置真。
        mon._running = True
        mon.events_dir.mkdir(parents=True, exist_ok=True)
        if prefill:
            # 等效「监视器已经跑过一轮」：跳过 backfill 防护，之前写入的记录本拍可读。
            mon._tailer._initial_backfill_done = True
        # 本文件不测扫描节流，所以把它关掉（scan_interval=0 即「每拍全量枚举」，是产品合法
        # 取值，见 test_agent_link.py::test_scan_interval_elapsed_rescans_without_directory_change）。
        # 原因：节流下的「新文件当拍发现」压在**目录 mtime 变化**上，而那不是确定前提——
        # 建文件后目录时间戳在 Windows 上惰性更新（本机实测：40 次里只有 22-33 次读得回变化，
        # 仓库既有探针实测 25/50），低精度文件系统上整拍都不变，新文件只能等 scan_interval
        # （默认 5s）兜底。本文件的「第二个写者 / 新实例」用例要的是**当拍摄入**，不该赌文件
        # 系统时序；节流契约本身由 test_agent_link.py::TestDirGlobTailer 那一族负责。
        mon._tailer.scan_interval = 0.0
        return mgr, mon, win

    yield build, sounds, clock, app


def _write(bridge_dir: Path, name: str, *records) -> None:
    with (bridge_dir / name).open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _pump(seconds: float = 1.2) -> None:
    """跑满一段事件循环（完成确认 800ms 窗需要真实定时器，不用固定 sleep 猜时序）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


# ======================================================================
# 1. 源 ID 路径：重复投递跨冷却仍不得二次发正文
# ======================================================================
def test_duplicate_tool_call_across_throttle_window_shows_body_once(harness, tmp_path):
    """同一 ``callId`` 的记录被投递两次（第二个桥写者/重放）：只允许一个正文落点。

    过程汇报气泡自带三重节流（同文案 60s），所以**只有越过 60s 再投递**才能把
    "身份去重"与"性能节流"分开——本用例正是这么做的：没有身份账本时第二次
    投递会照常冒出一个正文（见 scratch 的 red 日志）。
    """
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    record = {"event": "tool/call", "tool": "read", "callId": "c-1", "sessionId": "s-1"}
    # 记录会同时喂多条通道（raw_record / normalized_event / activity…）：身份闸在
    # 摄入边界挡一次，各通道都看不到第二次——这是"一次事件多通道不双响"的机制。
    raw_seen: list[object] = []
    normalized_seen: list[object] = []
    mon.raw_record.connect(lambda _a, r: raw_seen.append(r))
    mon.normalized_event.connect(lambda e: normalized_seen.append(e))
    try:
        _write(mon.events_dir, "dsh-1001.jsonl", record)
        mon._poll()
        first = len(win.bubbles)
        assert first >= 1, "首投必须出正文"
        raw_after_first = len(raw_seen)
        norm_after_first = len(normalized_seen)

        clock[0] += mgr._ACTIVITY_SAME_LABEL + 1.0        # 越过同文案 60s 节流
        clock[0] += mgr._ACTIVITY_GLOBAL_MIN + 1.0
        _write(mon.events_dir, "dsh-1002.jsonl", record)  # 同一逻辑事件：第二个写者
        mon._poll()
        assert len(win.bubbles) == first, "重复源事件不得二次发正文"
        assert (len(raw_seen), len(normalized_seen)) == (raw_after_first, norm_after_first), \
            "重复源事件不得再进任何通道（原始记录 / 语义层）"
        assert clock[0] - 1000.0 > mgr._ACTIVITY_SAME_LABEL, "断言必须发生在节流窗之外"
    finally:
        mgr.shutdown()


def test_duplicate_question_request_keeps_one_pending_and_closes(harness, tmp_path):
    """同一 ``callId`` 的问题请求重复投递：只登记一条，``resolved`` 能真的收掉它。

    修复前第二次登记会新开一条 hint 条目（无 rpcId 路径）→ 两次 pending、
    一次 resolved 只关得掉一条，另一条变成**关不掉的常驻提醒**。
    """
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    record = {"event": "question/requested", "callId": "c-9", "sessionId": "s-1",
              "questions": [{"id": "q1", "question": "选哪个？", "options": [{"label": "A"}]}]}
    try:
        _write(mon.events_dir, "dsh-1001.jsonl", record)
        mon._poll()
        assert len(mgr.pending_interactions_for("dsh")) == 1

        clock[0] += 300.0
        _write(mon.events_dir, "dsh-1002.jsonl", record)
        mon._poll()
        pending = mgr.pending_interactions_for("dsh")
        assert len(pending) == 1, f"重复源事件不得登记第二条：{sorted(pending)}"
        assert len([a for a in win.alerts if a["id"]]) == 1, "同一提醒 id 只允许一份"

        _write(mon.events_dir, "dsh-1002.jsonl",
               {"event": "question/resolved", "callId": "c-9", "sessionId": "s-1"})
        mon._poll()
        assert mgr.pending_interactions_for("dsh") == {}, "resolved 必须真的收掉那条"
    finally:
        mgr.shutdown()


def test_duplicate_approval_request_keeps_one_pending_and_closes(harness, tmp_path):
    """仅带 ``approvalId``（无 rpcId）的审批重复投递：只登记一条且可被身份关闭。"""
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    record = {"event": "approval/request", "tool": "bash",
              "approvalId": "ap-1", "sessionId": "s-1"}
    try:
        _write(mon.events_dir, "dsh-1001.jsonl", record)
        mon._poll()
        clock[0] += 300.0
        _write(mon.events_dir, "dsh-1002.jsonl", record)
        mon._poll()
        assert len(mgr.pending_interactions_for("dsh")) == 1

        _write(mon.events_dir, "dsh-1002.jsonl",
               {"event": "approval/resolved", "approvalId": "ap-1", "sessionId": "s-1"})
        mon._poll()
        assert mgr.pending_interactions_for("dsh") == {}
    finally:
        mgr.shutdown()


# ======================================================================
# 2. 无源 ID 路径：读取游标（同一份字节被读第二次）
# ======================================================================
def test_reread_same_bytes_does_not_replay_turn_body_or_sound(harness, tmp_path):
    """写者截断后把同一段内容写回 → tailer 从头部重读同一份字节：不得重播。

    ``turn/start`` / ``turn/end`` 只有 sessionId、没有源 ID，身份只能来自
    **读取游标**（文件身份 + 行首字节偏移）。重读会把同一回合重新喂进呈现管线：
    修复前 start/done 音效各再响一次、完成气泡再弹一次（跨音效冷却与 60s 节流
    都拦不住——它们是节流，不是身份）。
    """
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    bridge_file = mon.events_dir / "dsh-1001.jsonl"
    try:
        mon._probe_online()                       # 在线基线 → idle
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/start", "sessionId": "s-1"})
        mon._poll()
        clock[0] += 10.0                          # 越过 2s 音效冷却
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/end", "sessionId": "s-1"})
        mon._poll()
        _pump(1.2)
        first_bubbles = len(win.bubbles)
        first_sounds = list(sounds)
        assert first_sounds.count("start.wav") == 1, first_sounds
        assert first_sounds.count("done.wav") == 1, first_sounds
        assert first_bubbles >= 2, win.bubbles

        content = bridge_file.read_bytes()
        clock[0] += 120.0                         # 远超音效冷却与过程汇报节流
        bridge_file.write_bytes(b"")               # 截断：tailer 见 size<offset 归零
        mon._poll()
        bridge_file.write_bytes(content)           # 同一段字节写回 → 同一游标重投
        mon._poll()
        _pump(1.2)

        replayed_bubbles = win.bubbles[first_bubbles:]
        replayed_sounds = sounds[len(first_sounds):]
        assert (replayed_bubbles, replayed_sounds) == ([], []), (
            f"重读同一份字节不得重播正文/音效：正文+{replayed_bubbles} 音效+{replayed_sounds}")

        # 反例对照：同一文件**后续新增**的一行（新游标）必须照常呈现——
        # 去重挡的是"同一份字节被读第二次"，不是"同一会话的下一轮"。
        clock[0] += 10.0
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/start", "sessionId": "s-1"})
        mon._poll()
        assert "start.wav" in sounds[len(first_sounds):], \
            f"合法的下一轮不得被去重吃掉：{sounds}"
    finally:
        mgr.shutdown()


def test_rotation_new_file_first_line_is_not_treated_as_duplicate(harness, tmp_path):
    """文件轮转（同路径新文件、第一条记录 offset=0）不得被当成重复丢掉。

    游标里带**文件身份**：轮转后 ``file_id`` 变了，同一路径的 offset=0 与旧文件
    的 offset=0 不是同一次读取（只按 `(路径, offset)` 去重的实现会在这里误杀）。
    """
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    bridge_file = mon.events_dir / "dsh-1001.jsonl"
    try:
        mon._probe_online()
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/start", "sessionId": "s-1"})
        mon._poll()
        clock[0] += 10.0
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/end", "sessionId": "s-1"})
        mon._poll()
        _pump(1.2)
        clock[0] += 10.0

        # 轮转：旧文件改名（只留一代），同路径起新文件后立刻写入
        os.replace(bridge_file, mon.events_dir / "dsh-1001.jsonl.1")
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/start", "sessionId": "s-1"})
        mon._poll()
        assert mgr._last_raw.get("dsh") in ("thinking", "working"), \
            f"轮转后新文件的首条记录必须被摄入：{mgr._last_raw}"
        clock[0] += 10.0
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/end", "sessionId": "s-1"})
        mon._poll()
        _pump(1.2)
        assert sounds.count("done.wav") == 2, f"轮转后的第二回合必须照常出完成音效：{sounds}"
    finally:
        mgr.shutdown()


# ======================================================================
# 3. 同文本不同事件不得按文本去重
# ======================================================================
def test_same_text_distinct_source_ids_are_both_delivered(harness, tmp_path):
    """两条**不同身份**的同文本事件各自合法：正文与提醒都按各自 id 存在。

    文本不进去重键——同一句话重复说两次可以是两条合法事件（规范 §三）。
    """
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "tool/call", "tool": "read", "callId": "c-1", "sessionId": "s-1"})
        mon._poll()
        first = len(win.bubbles)
        clock[0] += mgr._ACTIVITY_SAME_LABEL + mgr._ACTIVITY_GLOBAL_MIN + 2.0
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "tool/call", "tool": "read", "callId": "c-2", "sessionId": "s-1"})
        mon._poll()
        assert len(win.bubbles) == first + 1, "不同 callId 的同文本事件必须各自呈现"

        # 同文本、不同 rpcId 的两条审批：两条 pending，各自可关
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "approval/request", "tool": "bash", "rpcId": "r-1", "sessionId": "s-1"},
               {"event": "approval/request", "tool": "bash", "rpcId": "r-2", "sessionId": "s-1"})
        mon._poll()
        assert len(mgr.pending_interactions_for("dsh")) == 2, "不同 rpcId 的审批必须各占一条"
    finally:
        mgr.shutdown()


def test_identical_activity_text_direct_call_after_cooldown_still_surfaces(harness, tmp_path):
    """既有的 60s 同文案节流**不是**身份去重：跨过冷却后直接投递仍须呈现。

    这是 R7 既有契约（``test_g2_handoff_and_repeat`` 的 agent_link 两条）在本文件
    的同口径复述：摄入去重只认**身份**，不得退化成"同文本一律只留一条"。
    """
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    try:
        mgr._on_agent_activity("dsh", "read")
        assert len(win.bubbles) == 1
        clock[0] += 30.0
        mgr._on_agent_activity("dsh", "read")
        assert len(win.bubbles) == 1, "冷却窗内按既有政策节流"
        clock[0] += mgr._ACTIVITY_SAME_LABEL + 1.0
        mgr._on_agent_activity("dsh", "read")
        assert len(win.bubbles) == 2, "越过冷却必须照旧呈现（节流政策未被去重取代）"
    finally:
        mgr.shutdown()


def test_new_event_within_explicit_sound_cooldown_is_still_muted(harness, tmp_path):
    """显式用户冷却照旧静音**新**事件：去重不是冷却的替代品。"""
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    try:
        mgr._emit_sound("done", "dsh")
        assert sounds.count("done.wav") == 1
        clock[0] += 1.0                       # 冷却窗内（配置 2s）
        mgr._emit_sound("done", "dsh")
        assert sounds.count("done.wav") == 1, "冷却窗内新事件按政策静音"
        clock[0] += 2.5
        mgr._emit_sound("done", "dsh")
        assert sounds.count("done.wav") == 2, "越过冷却照旧可播"
    finally:
        mgr.shutdown()


# ======================================================================
# 4. 一次事件多通道不双响
# ======================================================================
def test_one_record_one_body_and_one_sound_across_channels(harness, tmp_path):
    """单条记录同时喂语义层/原始记录/状态/工具通道：正文与音效各只允许一次。"""
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    try:
        mon._probe_online()
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "AgentStatus", "state": "working", "tool": "read",
                "callId": "c-3", "sessionId": "s-1"})
        mon._poll()
        assert sounds.count("start.wav") == 1, f"一次事件只允许一次提示音：{sounds}"
        assert len(win.bubbles) == 2, f"开始干事 + 过程汇报各一条，不得更多：{win.bubbles}"
    finally:
        mgr.shutdown()


# ======================================================================
# 5. 消费点：同一交互的重投递就地更新（不经监视器也要成立）
# ======================================================================
def test_register_interaction_same_identity_updates_in_place(harness, tmp_path):
    """``_on_approval_request`` 直接重投递同一身份：只是就地更新，不再开第二条。

    摄入账本挡的是监视器路径；登记本身也必须幂等——否则任何别的调用者
    （第二个监视器实例、未来传输层）再触发一次就会留下关不掉的常驻提醒。
    """
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    try:
        payload = {"tool": "bash", "rpcId": "r-7", "sessionId": "s-1"}
        mgr._on_approval_request("dsh", payload)
        first = mgr.pending_interactions_for("dsh")
        assert len(first) == 1
        iid = next(iter(first))

        mgr._on_approval_request("dsh", payload)
        second = mgr.pending_interactions_for("dsh")
        assert len(second) == 1, f"同一身份不得登记第二份：{sorted(second)}"
        assert next(iter(second)) == iid, "重复登记必须复用原 interaction_id"
        assert len([a for a in win.alerts if a["id"]]) == 1, "同一提醒 id 只允许一份"

        mgr._on_approval_resolved("dsh", {"rpcId": "r-7"})
        assert mgr.pending_interactions_for("dsh") == {}
    finally:
        mgr.shutdown()


def test_register_interaction_distinct_sessions_not_merged(harness, tmp_path):
    """同一 rpcId 跨会话复用（畸形/并发传输）：会话是身份的一部分，两条各自独立。"""
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    try:
        mgr._on_approval_request("dsh", {"tool": "bash", "rpcId": "r-8", "sessionId": "s-1"})
        mgr._on_approval_request("dsh", {"tool": "bash", "rpcId": "r-8", "sessionId": "s-2"})
        assert len(mgr.pending_interactions_for("dsh")) == 2
    finally:
        mgr.shutdown()


# ======================================================================
# 6. 账本本身：有界 / TTL / 关监测清理 / 重启不重播 history
# ======================================================================
def test_ledger_is_bounded_and_ttl_expires():
    """账本有上限、有 TTL：超出即淘汰，过期即允许重放（诚实边界，不承诺 exactly-once）。"""
    ledger = EventIdentityLedger(max_entries=3, ttl_s=60.0)
    for i in range(10):
        assert ledger.note(("k", i)) is False
    assert len(ledger) == 3, "账本必须有上限"
    assert ledger.note(("k", 9)) is True, "刚投递过的身份应判为重复"
    assert ledger.note(("k", 0)) is False, "最老的条目已被淘汰：允许重放（有界账本的边界）"

    expired = EventIdentityLedger(max_entries=8, ttl_s=0.0)
    assert expired.note(("x",)) is False
    assert expired.note(("x",)) is False, "TTL 到期后允许重放：本轮不承诺更久"


def test_ledger_cleared_when_monitor_stops(harness, tmp_path):
    """关监测即清账本（规范 §三）：通道不再摄入，账本不得留着。"""
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "tool/call", "tool": "read", "callId": "c-5", "sessionId": "s-1"})
        mon._poll()
        assert len(mon._event_ledger) == 1
        mon.begin_stop()
        assert len(mon._event_ledger) == 0
    finally:
        mgr.shutdown()


def test_restart_does_not_replay_history_but_new_events_surface(harness, tmp_path):
    """重启不重播 history（tailer backfill 防护），且新代次不继承旧账本。"""
    build, sounds, clock, _app = harness
    mgr, mon, win = build(prefill=False)   # 不预置 backfill：模拟真实启动
    try:
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/start", "sessionId": "s-1"},
               {"event": "turn/end", "sessionId": "s-1"})
        mon._probe_online()
        assert mon._tailer.read_new_lines() == [], "首次读取必须 backfill 掉历史"
        mon._poll()
        assert win.bubbles == [] and sounds == [], "启动那一拍不得重播 history"

        # 新代次写入的新事件照常呈现（旧账本不得跨代次留存）
        clock[0] += 10.0
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/start", "sessionId": "s-1"})
        mon._poll()
        assert "start.wav" in sounds, f"新代次的新事件必须呈现：{sounds}"
    finally:
        mgr.shutdown()


def test_stale_generation_delivery_after_stop_is_rejected(harness, tmp_path):
    """监视器停止后，旧代次的记录不得再驱动任何正文/音效。"""
    build, sounds, clock, _app = harness
    mgr, mon, win = build()
    try:
        mon._probe_online()
        stale_gen = mon._gen
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/start", "sessionId": "s-1"})
        mon._poll(gen=stale_gen)
        assert "start.wav" in sounds
        bubbles_before = len(win.bubbles)
        sounds_before = list(sounds)

        mon.begin_stop()               # 代次作废（_emit_gen = -1）
        clock[0] += 30.0
        _write(mon.events_dir, "dsh-1001.jsonl",
               {"event": "turn/start", "sessionId": "s-1"},
               {"event": "turn/end", "sessionId": "s-1"})
        mon._poll(gen=stale_gen)       # 旧代次迟到投递
        _pump(1.2)
        assert len(win.bubbles) == bubbles_before, "旧代次不得再落正文"
        assert sounds == sounds_before, "旧代次不得再出声"
    finally:
        mgr.shutdown()
