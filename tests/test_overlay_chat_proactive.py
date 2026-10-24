# -*- coding: utf-8 -*-
"""Phase 4.3 后半 offscreen 单测：共享子系统接入 sprite 世界 + 自说自话 + 快速对话。

背景（PHASE4_DESIGN T1/D0 的另一半）：overlay 拓扑下 ``instances[].win`` 恒为
None，进程级共享子系统（agent_link / proactive / 全屏 watcher）的扇出集合为空
= 联动与自说自话静默缺失。本刀让 ``OverlayShell`` 成为扇出目标，并补齐
PetWindow 的呈现面。

覆盖：
1. 扇出目标：``presentation_targets`` overlay = 壳 / legacy = 各窗，逐行不变；
2. 注入接线：AppShell.start() 把 ``shared.agent_link`` / ``shared.proactive``
   注入壳（见 test_overlay_shell.py 的拓扑分支用例）；
3. 自说自话：proactive 真实触发链（``_on_frame_ready``）文本落到主 sprite 头顶
   气泡，且经 proxy 聚合走 ``isVisible`` / ``hold_bubble``；
4. 气泡面：``show_bubble`` / ``show_alert`` 复用 window_alerts 的队列语义；
5. 显隐 pause/resume：``set_pet_visible`` 调监视器 pause/resume，且
   ``isVisible()`` 反映 overlay 可见性（proactive G1 的输入）；
6. 快速对话：气泡可点 → ``open_quick_chat`` 锚定被点 sprite，回车走既有
   ChatService 链路；
7. 无聊天变体降级：``pet.quick_chat`` 导入失败 / ``enable_chat=False`` →
   静默 False、气泡保持全穿透；
8. legacy 拓扑行为不变。

纪律：同步直调（不 sleep 赌时序）、假屏假 sprite 复用既有测试件
（tests/test_overlay_window_capabilities.py）；真实 Qt 事件循环由 QApplication
单例提供，全部 offscreen。
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import pet.vision as vision_mod
import tests.test_overlay_window_capabilities as cap
import tests.test_sprite_menu_facade as fac
from pet.config import Config
from pet.multi_window_shared import (
    MultiWindowProxy,
    SharedProactiveWatcher,
    presentation_targets,
)
from pet.overlay_shell import OverlayShell
from pet.sprite_behavior import STATE_ACTS
from pet.sprite_bubble import sprite_anchor_rect_global

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 替身
class StubMonitor:
    """proactive/agent_link 监视器替身：只记 pause/resume。"""

    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1


class _Host:
    """共享子系统眼里的 AppShell：只有 config/instances/_overlay_shell。"""

    def __init__(self, config, overlay_shell=None, windows=()) -> None:
        self.config = config
        self.instances = [type("_Inst", (), {"win": w})() for w in windows]
        self._overlay_shell = overlay_shell
        self._shared = None


class _NoChatInstance(cap.CapInstance):
    """无聊天打包变体（pet.chat 被 excludes 排除）的实例面。"""

    enable_chat = False


def _make_shell(tmp_path, values=None, *, real_config=False,
                watcher=None, manager=None, instance=None):
    config = Config(base=tmp_path) if real_config else cap.CapConfig(tmp_path, values)
    if instance is None:
        instance = cap.CapInstance(config)
    else:
        instance.config = config
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: cap.CapSprite(lib, pos, scale),
        agent_link_manager=manager, proactive_watcher=watcher)
    return shell, config


def _proactive_config(**over):
    values = {
        "proactive_screen": {
            "enabled": True,
            "whitelist": ["code"],
            "pre_cue": True,
            "dry_run": False,
        },
    }
    values.update(over)
    return values


# ---------------------------------------------------------------- 1. 扇出目标
def test_presentation_targets_overlay_is_shell_legacy_is_windows(tmp_path):
    """扇出目标挂接：legacy = instances[].win（逐行不变）；overlay = sprite 壳。"""
    w1, w2 = object(), object()
    legacy = _Host(cap.CapConfig(tmp_path), windows=(w1, w2))
    assert presentation_targets(legacy) == [w1, w2]

    shell, _ = _make_shell(tmp_path)
    overlay = _Host(cap.CapConfig(tmp_path), overlay_shell=shell)
    assert presentation_targets(overlay) == [shell]

    try:
        # legacy 宿主（无 _overlay_shell 属性）也要兼容：不能抛 AttributeError
        class _Bare:
            config = cap.CapConfig(tmp_path)
            instances = [type("_Inst", (), {"win": w1})()]

        assert presentation_targets(_Bare()) == [w1]
    finally:
        shell._delete_runtime_marker()


def test_proxy_fans_out_to_sprite_shell_bubble(tmp_path):
    """端到端：共享 manager 的 win = proxy → 呈现落到 sprite 头顶气泡。"""
    shell, config = _make_shell(tmp_path)
    proxy = MultiWindowProxy(_Host(config, overlay_shell=shell))
    try:
        shell.overlay.show()
        assert proxy._windows() == [shell]
        assert proxy.isVisible() is True
        proxy.show_bubble("主人在忙什么呀", duration_ms=1200)
        assert shell._speech_bubble is not None
        assert shell._speech_bubble._raw_text == "主人在忙什么呀"
    finally:
        shell._delete_runtime_marker()


def test_hidden_link_bubble_redirect_only_in_overlay_topology(tmp_path):
    """隐藏期联动气泡改道灵动岛：overlay 转发，legacy 逐位不变（None）。

    agent_link 的 ``_show_link_bubble`` 隐藏期会 ``redirect_hidden_bubble``；
    共享 proxy 此前不转发该属性 → overlay 拓扑下隐藏期联动气泡全丢。
    """
    shell, config = _make_shell(tmp_path)
    proxy = MultiWindowProxy(_Host(config, overlay_shell=shell))
    try:
        shell.overlay.show()
        assert proxy.hidden_bubble_redirect is None       # 可见期不改道
        shell.set_pet_visible(False)
        assert proxy.hidden_bubble_redirect is None       # 无岛反馈面注入 → 维持丢弃
        shell._instance.shell = type("_Shell", (), {
            "_island_feedback_bubble":
                staticmethod(lambda *a, **k: True)})()
        redirect = proxy.hidden_bubble_redirect
        assert callable(redirect) and redirect("隐藏期联动气泡") is True
    finally:
        shell._delete_runtime_marker()

    legacy = MultiWindowProxy(_Host(cap.CapConfig(tmp_path), windows=[object()]))
    assert legacy.hidden_bubble_redirect is None          # legacy 逐位不变


# ---------------------------------------------------------------- 2/3. 自说自话
def test_proactive_trigger_speaks_on_sprite_bubble(tmp_path, monkeypatch):
    """proactive 真实触发链 → 先兆气泡 + 气泡位占用，全部落在 sprite 壳上。

    以 ``_on_frame_ready``（真实触发入口）驱动：截图/dHash 由 worker 提供，
    这里直接喂 bytes 与 info；视觉请求线程换成不启动的替身（本用例只验呈现）。
    """
    shell, config = _make_shell(tmp_path, _proactive_config())
    watcher = SharedProactiveWatcher(
        MultiWindowProxy(_Host(config, overlay_shell=shell)), config)
    shell.proactive_watcher = watcher

    class _NoThread:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", _NoThread)
    monkeypatch.setattr(watcher.limiter, "try_acquire", lambda: (True, "ok"))
    monkeypatch.setattr(watcher, "_resolve_vision_provider",
                        lambda eff: (object(), "prompt"))
    try:
        shell.overlay.show()
        watcher._on_frame_ready(b"\xff\xd8jpeg", "code.exe | main.py", 42, 7,
                                {"process": "code", "title": "main.py"})
        assert shell._speech_bubble is not None
        assert shell._speech_bubble._raw_text == "让我看看……"
        # 气泡位占用（proactive hold_bubble(30) → 联动普通气泡让路）
        assert shell._bubble_busy_until > time.monotonic()
    finally:
        watcher.stop_all()
        shell._delete_runtime_marker()


def test_hidden_overlay_blocks_proactive_probe(tmp_path, monkeypatch):
    """显隐 → G1 守卫：隐藏后 proactive 内部 tick 不得再取前台窗口信息。"""
    shell, config = _make_shell(tmp_path, _proactive_config())
    watcher = SharedProactiveWatcher(
        MultiWindowProxy(_Host(config, overlay_shell=shell)), config)
    shell.proactive_watcher = watcher
    probes: list = []
    monkeypatch.setattr(vision_mod, "foreground_window_info",
                        lambda: (probes.append(1), None)[1])
    try:
        shell.overlay.show()
        watcher._on_tick()
        assert probes, "可见时必须进入 G5 探测（否则共享模式下识屏永不触发）"
        shell.set_pet_visible(False)
        assert shell.isVisible() is False
        probes.clear()
        watcher._on_tick()
        assert probes == [], "隐藏后 G1 必须拦下探测（限流状态不丢）"
    finally:
        watcher.stop_all()
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 4. 气泡/提醒面
def test_alert_queue_reuses_window_alerts_semantics(tmp_path):
    """提醒队列：show_alert 一次只展示一个，resolve_alert 推进下一条。"""
    shell, _ = _make_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.show_alert("审批 A", alert_id="a", buttons=[("同意", lambda: None)])
        assert shell._alert_current is not None
        shell.show_alert("提醒 B", alert_id="b", sticky=False)
        shell.show_alert("提醒 C", alert_id="c", sticky=False)
        assert [item["id"] for item in shell._alert_queue] == ["b", "c"]
        assert shell._alert_current["id"] == "a"

        shell.resolve_alert("a")
        assert shell._alert_current is None or shell._alert_current["id"] != "a"
        assert shell._speech_bubble is not None
    finally:
        shell._delete_runtime_marker()


def test_show_bubble_skipped_while_hidden_or_suppressed(tmp_path):
    """隐藏 / 设置页抑制期：普通气泡静默丢弃（window.show_bubble 语义）。"""
    shell, _ = _make_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.set_bubble_suppressed(True)
        shell.show_bubble("不该出现")
        assert shell._speech_bubble._raw_text != "不该出现"
        shell.set_bubble_suppressed(False)
        shell.set_pet_visible(False)
        shell.show_bubble("还是不该出现")
        # 隐藏期连泡都不建（旧窗是在已建好的泡上换文案）：没有任何呈现面带着这段文字
        assert shell._speech_bubble is None, "隐藏期不建气泡"
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 5. 显隐 pause/resume
def test_set_pet_visible_pauses_and_resumes_subsystems(tmp_path):
    watcher, manager = StubMonitor(), StubMonitor()
    shell, _ = _make_shell(tmp_path, watcher=watcher, manager=manager)
    try:
        # 注入接线：构造参数落到壳属性（AppShell.start() 传的就是这两份共享实例）
        assert shell.proactive_watcher is watcher
        assert shell.agent_link_manager is manager

        shell.set_pet_visible(True)
        assert shell.isVisible() is True
        assert (watcher.resumed, manager.resumed) == (1, 1)

        shell.set_pet_visible(False)
        assert shell.isVisible() is False
        assert (watcher.paused, manager.paused) == (1, 1)

        shell.set_pet_visible(True)
        assert (watcher.resumed, manager.resumed) == (2, 2)
    finally:
        shell._delete_runtime_marker()


def test_fullscreen_autohide_pauses_subsystems(tmp_path):
    """全屏自动隐藏与手动隐藏同语义（legacy 走同一个 hide() → _pause_activity）。"""
    watcher, manager = StubMonitor(), StubMonitor()
    shell, _ = _make_shell(tmp_path, watcher=watcher, manager=manager)
    try:
        shell.overlay.show()
        shell._on_fullscreen_changed(True)
        assert shell.isVisible() is False
        assert (watcher.paused, manager.paused) == (1, 1)
        shell._on_fullscreen_changed(False)
        assert shell.isVisible() is True
        assert (watcher.resumed, manager.resumed) == (1, 1)
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 6. 快速对话
class _StubChatService:
    """ChatService 替身：只记 send 调用（不碰网络）。"""

    def __init__(self) -> None:
        self.busy = False
        self.sent: list = []

    def send(self, messages, config):
        self.sent.append((messages, config))
        return "req-1"

    def stop(self) -> None:
        pass


def test_bubble_click_opens_quick_chat_anchored_to_sprite(tmp_path):
    """气泡可点 → 快速对话输入，锚点 = 被点 sprite 的身体框（全局）。"""
    shell, config = _make_shell(tmp_path, real_config=True)
    try:
        shell.overlay.show()
        bubble = shell._speech_bubble
        assert bubble is not None
        assert shell.quick_chat_available() is True
        shell.show_bubble("点我聊天")
        # 普通气泡可点（旧路径 _set_speech_bubble_interactive 语义）
        assert not bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        bubble.clicked.emit()  # 真实信号槽 → _on_bubble_clicked → open_quick_chat
        quick = shell._quick_chat
        assert quick is not None
        anchor = quick.pet_window
        assert anchor.visible_content_rect() == sprite_anchor_rect_global(
            shell.sprite, shell.overlay.geometry().topLeft())
        assert quick.isVisible() is True
    finally:
        shell._close_quick_chat()
        shell._delete_runtime_marker()


def test_quick_chat_enter_sends_through_existing_chat_service(tmp_path):
    """回车发送走既有 ChatService/SessionStore 链路（不接新后端）。"""
    shell, config = _make_shell(tmp_path, real_config=True)
    try:
        shell.overlay.show()
        assert shell.open_quick_chat() is True
        quick = shell._quick_chat
        stub = _StubChatService()
        quick.service = stub
        quick.input.setText("主人今天也要加油")
        quick.input.returnPressed.emit()
        assert len(stub.sent) == 1
        messages, _settings = stub.sent[0]
        assert [m.get("content") for m in messages if m.get("role") == "user"] == [
            "主人今天也要加油"]
    finally:
        shell._close_quick_chat()
        shell._delete_runtime_marker()


def test_alert_bubble_click_is_noop(tmp_path):
    """交互/提醒气泡的主体点击必须是 no-op（按钮自身才是决策入口）。"""
    shell, config = _make_shell(tmp_path, real_config=True)
    try:
        shell.overlay.show()
        shell.show_alert("审批请求", alert_id="x", buttons=[("同意", lambda: None)])
        shell._speech_bubble.clicked.emit()
        assert shell._quick_chat is None
    finally:
        shell._close_quick_chat()
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 7. 无聊天变体降级
def test_no_chat_module_degrades_quietly(tmp_path, monkeypatch):
    """pet.chat 缺失（打包 excludes）→ ImportError 守卫静默降级为不可点。

    复现真实变体：``pet.quick_chat`` 的模块级 ``from .chat.service import
    ChatService`` 在无聊天打包里抛 ImportError（name = ``pet.chat.service``）。
    """
    monkeypatch.setitem(sys.modules, "pet.chat.service", None)
    monkeypatch.delitem(sys.modules, "pet.quick_chat", raising=False)
    shell, _ = _make_shell(tmp_path, real_config=True)
    try:
        shell.overlay.show()
        assert shell.quick_chat_available() is False
        assert shell.open_quick_chat() is False
        shell.show_bubble("无聊天变体也能冒泡")
        assert shell._speech_bubble._raw_text == "无聊天变体也能冒泡"
        assert shell._speech_bubble.testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    finally:
        shell._delete_runtime_marker()


def test_chat_disabled_degrades_quietly(tmp_path):
    """进程级 enable_chat=False（纯桌宠版）→ 快速对话入口与气泡点击一起降级。"""
    shell, _ = _make_shell(tmp_path, real_config=True,
                           instance=_NoChatInstance(cap.CapConfig(tmp_path)))
    try:
        shell.overlay.show()
        assert shell.quick_chat_available() is False
        assert shell.open_quick_chat() is False
        shell.show_bubble("纯桌宠版")
        assert shell._speech_bubble.testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert shell.on_look_synced is None
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 8. 联动动作面
def test_link_anim_requests_reach_behavior_controller(tmp_path):
    """request_link_anim/idle/switch_clip 落在行为控制器（不再是静默 no-op）。

    前置：壳可见——联动动作是**自动事件**，落点按规范取"可见的最小槽"
    （``alert_target_sprite``），隐藏期不给任何呈现场合。
    """
    shell, _lib = fac._make_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.request_link_anim("act1")
        assert shell.sprite._clip_name == "act1"
        assert shell.behavior.state_of(shell.sprite) == STATE_ACTS

        shell.clear_pending_link_anim()
        assert shell._pending_link_anim is None

        # 一次性动作播放中：联动请求存为待播，不打断
        shell.request_link_anim("act1")
        assert shell._pending_link_anim == "act1"
        # 动作播完（状态机收口）→ extras 链观测下降边沿，接续待播
        shell.behavior.forget(shell.sprite)
        shell._link_chain._was_busy = True
        shell._link_chain.tick([shell.sprite], 0.016)
        assert shell._pending_link_anim is None
        assert shell.sprite._clip_name == "act1"

        # 一次性动作在播时回待机请求不打断（让它自然播完）
        shell.request_link_idle()
        assert shell.sprite._clip_name == "act1"
        shell.behavior.forget(shell.sprite)
        shell.request_link_idle()
        assert shell.sprite._clip_name == "idle1"
    finally:
        shell._delete_runtime_marker()


def test_link_chain_asks_provider_after_one_shot_ends(tmp_path):
    """联动动作链接续：一次性动作播完 → 问 provider 要下一个（_on_anim_ended 等价）。"""
    shell, lib = fac._make_shell(tmp_path)
    lib.acts.append("act2")
    lib._clips["act2"] = lib._clips["act1"]
    try:
        shell.overlay.show()
        shell.set_link_next_provider(lambda: "act2")
        shell.request_link_anim("act1")
        assert shell.sprite._clip_name == "act1"

        shell.behavior.forget(shell.sprite)   # 一次性动作播完
        shell._link_chain._was_busy = True
        shell._link_chain.tick([shell.sprite], 0.016)
        assert shell.sprite._clip_name == "act2"
        assert shell._link_anim_current == "act2"
    finally:
        shell._delete_runtime_marker()


def test_shell_installs_shared_link_provider(tmp_path):
    """壳自接共享联动链 provider（AppShell._wire_shared_subsystems 的等价物）。

    legacy 由 app.py 在每窗创建后调 ``_wire_shared_subsystems``；overlay 壳在
    AppShell.start() 才出现，共享 manager 的 __init__ 期注入落进空集合，
    必须由壳自接一次。
    """
    class _SharedStub:
        def __init__(self):
            self.agent_link = type(
                "_Mgr", (), {"_next_busy_anim": staticmethod(lambda: "act1")})()
            self.providers = []

            class _Proxy:
                def set_link_next_provider(_self, provider):
                    self.providers.append(provider)

            self.proxy = _Proxy()

    shared = _SharedStub()
    host = _Host(cap.CapConfig(tmp_path))
    host._shared = shared
    instance = cap.CapInstance(cap.CapConfig(tmp_path))
    instance.shell = host
    shell, _ = _make_shell(tmp_path, instance=instance)
    try:
        # 壳自接：provider 已交给共享 proxy（stub 只记录，扇出面见下）
        assert shared.providers == [shared.agent_link._next_busy_anim]

        # 真 proxy 的扇出面：provider 落到呈现目标（sprite 壳）
        proxy = MultiWindowProxy(_Host(cap.CapConfig(tmp_path), overlay_shell=shell))
        proxy.set_link_next_provider("sentinel")
        assert shell._link_next_provider == "sentinel"
    finally:
        shell._delete_runtime_marker()


def test_on_look_synced_injected_when_chat_available(tmp_path):
    """主动识屏答复同步面：有聊天时指向 PetInstance.sync_look_to_chat。"""

    class _ChatInstance(cap.CapInstance):
        def sync_look_to_chat(self, user_text, reply):
            self.synced = (user_text, reply)

    instance = _ChatInstance(cap.CapConfig(tmp_path))
    shell, _ = _make_shell(tmp_path, instance=instance)
    try:
        callback = shell.on_look_synced
        assert callable(callback)
        callback("看到的画面", "答复")
        assert instance.synced == ("看到的画面", "答复")
    finally:
        shell._delete_runtime_marker()
