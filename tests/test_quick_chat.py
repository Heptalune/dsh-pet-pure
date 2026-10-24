# -*- coding: utf-8 -*-
"""QuickChatBubble 气泡外形 + 角色同步（B07）回归。"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pet.config import Config
from pet.quick_chat import _surface_radius
from pet.speech_bubble import BUBBLE_STYLE_PRESETS


def test_breath_bubble_preset_gets_rounded_fallback_radius():
    """breath_bubble 是 speech_bubble 专属有机形（preset 里 radius=0）：
    quick_chat 不支持该形状，直接按 0 渲染会变直角方框——同一
    self_talk_bubble_style 设置两套观感。必须给大圆角近似回退。"""
    preset = BUBBLE_STYLE_PRESETS["breath_bubble"]
    assert preset.get("shape") == "breath_bubble"
    assert _surface_radius(preset) > 0


def test_regular_preset_uses_own_radius():
    """普通预设用自己的 radius（回退不得影响既有外观）。"""
    preset = BUBBLE_STYLE_PRESETS["classic_top"]
    assert _surface_radius(preset) == float(preset.get("radius", 14))


# ---------------------------------------------------------------- 角色同步（B07）
def test_switch_character_moves_session_and_prompt_to_new_character(tmp_path):
    """B07：切角色后气泡必须按新角色取会话、按新角色构造 prompt。

    气泡的角色是构造期一次性快照（``character_id`` + 会话 + 系统提示全按它取），
    主宠切角色不同步就等于聊天面永远停在旧角色。
    """
    app = QApplication.instance() or QApplication([])
    from pet.quick_chat import QuickChatBubble

    bubble = QuickChatBubble(Config(base=tmp_path))
    try:
        assert bubble.character_id == "shenshen"
        bubble.switch_character("nova")
        assert bubble.character_id == "nova"
        # 会话按新角色取（旧角色的会话不再沿用）
        assert bubble.session.character_id == "nova"
        assert bubble.session.system_prompt == bubble.prompt_builder.effective_system_prompt(
            bubble.settings, "nova")
        # prompt 链路跟着新角色走（#250 起 system 末尾会附加时间信息，比对前缀）
        messages = bubble.prompt_builder.build_messages(
            bubble.settings, bubble.character_id, [], "在吗")
        assert messages[0]["content"].startswith(bubble.session.system_prompt)
    finally:
        bubble.close()
        app.processEvents()


def test_switch_character_ignores_empty_and_same_character(tmp_path):
    """空身份/同角色不动作（会话不被无谓重建，避免丢掉当前会话）。"""
    app = QApplication.instance() or QApplication([])
    from pet.quick_chat import QuickChatBubble

    bubble = QuickChatBubble(Config(base=tmp_path))
    try:
        session = bubble.session
        bubble.switch_character("")
        assert bubble.character_id == "shenshen" and bubble.session is session
        bubble.switch_character("shenshen")
        assert bubble.session is session
    finally:
        bubble.close()
        app.processEvents()


# ---------------------------------------------------------------- 切角色作废在途请求
class _BlockingProvider:
    """可控网络替身（只替换网络边界）：请求进来卡住，测试放行后才产出一条回答。"""

    def __init__(self, reply: str = "旧角色的回答") -> None:
        self.reply = reply
        self.started = threading.Event()
        self.release = threading.Event()

    def stream(self, messages, config, cancel):
        self.started.set()
        while not self.release.wait(timeout=0.02):
            if cancel.is_set():
                return
        if cancel.is_set():
            return
        yield self.reply


def _pump_until(predicate, *, timeout: float = 10.0) -> bool:
    """宽预算轮询 + 事件泵（worker→主线程的信号是队列投递，必须跑事件循环）。"""
    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def _send_blocking_question(bubble, question: str = "你在吗"):
    """真 ChatService/QThread + 真会话存储：发一条问题并卡在请求中。"""
    provider = _BlockingProvider()
    bubble.service.provider = provider
    bubble.input.setText(question)
    bubble._send()
    assert provider.started.wait(timeout=10), "前提：请求已进入 provider"
    assert bubble._active_request_id is not None, "前提：请求在途"
    return provider


def test_switch_character_drops_inflight_answer(tmp_path):
    """切角色必须作废在途请求：旧角色的回答不许落进新角色的会话也不许上屏。

    旧行为：``switch_character`` 只换身份 + 重取会话，不清 ``_active_request_id``；
    旧角色那次请求的完成信号在切换之后到达时 ID 仍然匹配，回答被 append 进**新**
    角色的 session 并显示在新角色的气泡里。对照完整聊天窗
    （``chat/widgets.py::switch_character`` 清 request ID + 停旧请求）同款闸门。
    """
    app = QApplication.instance() or QApplication([])
    from pet.quick_chat import QuickChatBubble

    bubble = QuickChatBubble(Config(base=tmp_path))
    try:
        provider = _send_blocking_question(bubble)
        # 打点「停止信号」：它是第四类按 request-ID 认领的信号，这条用例造的就是它的
        # 迟到投递（替身收到 cancel 收场 → worker emit stopped_by_user → 队列投递）
        stopped = []
        bubble.service.stopped.connect(lambda rid: stopped.append(rid))

        bubble.switch_character("nova")
        assert bubble._active_request_id is None, "切角色即作废在途请求（ID 闸归零）"
        assert bubble.hint_label.text() == "", "旧请求不会再回来，思考中提示必须撤掉"

        provider.release.set()
        assert _pump_until(lambda: len(stopped) == 1), \
            "前提：停止信号确实被投递到了气泡（否则下面几条断言会空过）"
        assert not bubble.service.busy, "旧 worker 未收尾"
        # 迟到信号（完成/增量/错误/停止）全部被 ID 闸丢弃：既不落库也不上屏
        assert bubble.hint_label.text() == "", "迟到的停止信号不许把气泡改成「已停止」"
        assert bubble.output.text() == "", "旧角色的回答不许显示在新角色的气泡里"
        assert not [m for m in bubble.session.messages if m.role == "assistant"], \
            "旧角色的回答不许写进新角色的会话"
        assert bubble.session.character_id == "nova"
    finally:
        bubble.close()
        app.processEvents()


# ---------------------------------------------------------------- 迟到信号（astra #1）
class _LateProvider:
    """可控网络替身：请求卡住，放行后产出**终态**。

    与 ``_BlockingProvider`` 的分工：那个收到 cancel 就直接返回（信号根本没被造出来），
    只验到"没有信号时不出错"；本替身放行后一定会 emit 出信号——``"completed"`` 先产出
    一条增量再正常收尾（worker 依次 emit 增量 + 完成），``"error"`` 直接抛异常
    （worker emit 错误）。emit 在 worker 线程发生、投递到 GUI 线程要等事件循环，
    两者之间正好是"先切角色、再投递"的窗口。
    """

    def __init__(self, outcome: str = "completed") -> None:
        self.outcome = outcome
        self.started = threading.Event()
        self.release = threading.Event()

    def stream(self, messages, config, cancel):
        self.started.set()
        self.release.wait(timeout=10)
        if self.outcome == "error":
            raise RuntimeError("旧角色的网络错误")
        yield "迟到的增量"


def _release_and_wait_worker_exit(bubble, provider) -> bool:
    """放行替身，并等到 worker 线程的 ``run()`` 真的退出。

    ``QThread.finished`` 走直连（在 worker 线程执行）——它一响就说明增量/完成/错误
    这些信号全部 emit 完毕、队列投递已挂在 GUI 线程事件队列里且尚未被处理。
    """
    done = threading.Event()
    bubble.service._worker.finished.connect(lambda: done.set(), Qt.DirectConnection)
    provider.release.set()
    return done.wait(timeout=10)


def _send_late_question(bubble, outcome: str = "completed"):
    """发一条问题，返回 ``(provider, phase, forwarded)``。

    ``forwarded`` 记下 service 那几类信号**到达气泡时**的测试阶段（``in_flight`` /
    ``after_switch``）：既证明信号真的到过气泡（否则"没上屏"的断言可以空过），也证明
    它是在切角色**之后**才被投递的。``phase`` 由用例在切换前翻成 ``after_switch``。
    """
    provider = _LateProvider(outcome)
    bubble.service.provider = provider
    phase = {"now": "in_flight"}
    forwarded: list[tuple[str, str]] = []
    for name in ("delta", "finished", "error"):
        getattr(bubble.service, name).connect(
            lambda *args, _name=name: forwarded.append((_name, phase["now"])))
    bubble.input.setText("你在吗")
    bubble._send()
    assert provider.started.wait(timeout=10), "前提：请求已进入 provider"
    assert bubble._active_request_id is not None, "前提：请求在途"
    return provider, phase, forwarded


def _assert_late_signals_dropped(bubble, forwarded, expected: set[str]) -> None:
    """断言 ``expected`` 那几类信号在切换后被投递到气泡、且一个副作用都没留下。"""
    delivered = {name for name, _ in forwarded}
    assert _pump_until(lambda: expected <= {name for name, _ in forwarded}), \
        f"前提：{expected} 必须真的被投递过（实际 {delivered}）"
    assert all(where == "after_switch" for _, where in forwarded), \
        f"前提：投递必须发生在切角色之后（实际 {forwarded}）"
    assert bubble.output.text() == "", "迟到信号不许上屏"
    assert bubble._reply_full == ""
    assert bubble.hint_label.text() == ""
    assert not [m for m in bubble.session.messages if m.role == "assistant"], \
        "迟到的完成信号不许落进新角色的会话"
    assert bubble.session.character_id == "nova"


def test_switch_character_drops_queued_late_delta_and_completion(tmp_path):
    """astra #1：增量 + 完成信号**先 emit、后切角色、再投递**，必须被 ID 闸丢弃。

    换会话会换掉 ``session``，迟到完成信号若还认领成功，回答就落进**新**角色的会话
    并显示在新角色的气泡里——既有用例没造出这条信号，等于这条路径没被动态验过。
    """
    app = QApplication.instance() or QApplication([])
    from pet.quick_chat import QuickChatBubble

    bubble = QuickChatBubble(Config(base=tmp_path))
    try:
        provider, phase, forwarded = _send_late_question(bubble)
        assert _release_and_wait_worker_exit(bubble, provider), \
            "前提：worker 已把增量 + 完成 emit 进队列"

        phase["now"] = "after_switch"
        bubble.switch_character("nova")

        _assert_late_signals_dropped(bubble, forwarded, {"delta", "finished"})
    finally:
        bubble.close()
        app.processEvents()


def test_switch_character_drops_queued_late_error(tmp_path):
    """astra #1：错误信号同样先 emit 再切角色 → 不许把「请求失败：…」写上新角色气泡。"""
    app = QApplication.instance() or QApplication([])
    from pet.quick_chat import QuickChatBubble

    bubble = QuickChatBubble(Config(base=tmp_path))
    try:
        provider, phase, forwarded = _send_late_question(bubble, "error")
        assert _release_and_wait_worker_exit(bubble, provider), \
            "前提：worker 已把错误 emit 进队列"

        phase["now"] = "after_switch"
        bubble.switch_character("nova")

        _assert_late_signals_dropped(bubble, forwarded, {"error"})
    finally:
        bubble.close()
        app.processEvents()


def test_switch_character_without_inflight_request_only_swaps_session(tmp_path):
    """非回归：空闲切角色行为不变（只换身份 + 重取会话，不触碰在途状态字段）。"""
    app = QApplication.instance() or QApplication([])
    from pet.quick_chat import QuickChatBubble

    bubble = QuickChatBubble(Config(base=tmp_path))
    try:
        assert bubble._active_request_id is None
        bubble.switch_character("nova")
        assert bubble.character_id == "nova"
        assert bubble.session.character_id == "nova"
        assert bubble._active_request_id is None
    finally:
        bubble.close()
        app.processEvents()

def test_reply_renders_markdown(tmp_path):
    """快捷气泡的助手回复按 Markdown 渲染（对齐现代聊天窗 #249 的口径）——
    用户反馈 v4.3.0 快捷气泡里 markdown 原文直出（**、## 符号裸露）。"""
    app = QApplication.instance() or QApplication([])
    from pet.quick_chat import QuickChatBubble

    bubble = QuickChatBubble(Config(base=tmp_path))
    try:
        bubble._set_reply_text("重点**加粗**收尾")
        bubble._render_reply()
        assert 'font-weight:700' in bubble.output.text() and "加粗" in bubble.output.text(), \
            f"markdown 未渲染: {bubble.output.text()[:80]}"  # Qt 写 span font-weight 而非 <strong>
    finally:
        bubble.close()
        app.processEvents()
