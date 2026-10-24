"""G2 R6/R7：主身份交接不重放提醒/不重复出声；合法重复文本不被去重误删。

R6：提醒呈现面 = ``alert_target_sprite()``（可见最低 slot）。slot0 退出/回归
引起呈现面迁移时，不得因交接再弹一次提醒、不得再出声，且同一提醒最多只有
一份可见；交接后按 id 收起必须真的收掉那一份。
R7：两条不同 id 的同文本提醒都要依次呈现、各自可按 id 收起；同 id 合并。
agent_link 同 agent 同文本的两次合法事件（越过文档化冷却）都要冒出来。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet import click_sound  # noqa: E402
from pet.speech_bubble import PetSpeechBubble  # noqa: E402
from tests.test_overlay_collision_toggle import _make_slot_shell  # noqa: E402

app = QApplication.instance() or QApplication([])

ALERT_TEXT = "Agent 需要你批准一条命令"


@pytest.fixture
def probes(monkeypatch):
    """记录全部气泡 show_text 调用与全部音效播放（音效走 ClickSoundPool 单点）。"""
    shown: list[tuple[object, str]] = []
    sounds: list[object] = []
    original = PetSpeechBubble.show_text

    def recording_show_text(self, text, *args, **kwargs):
        shown.append((self, str(text)))
        return original(self, text, *args, **kwargs)

    def fake_play(self, path, volume=1.0):
        sounds.append(path)
        return True

    monkeypatch.setattr(PetSpeechBubble, "show_text", recording_show_text)
    monkeypatch.setattr(click_sound.ClickSoundPool, "play_sound", fake_play)
    return shown, sounds


def _child(shell):
    children = [s for s in shell._all_sprites() if s is not shell.sprite]
    assert len(children) == 1
    return children[0]


def _visible_copies(shell, text):
    bubbles = []
    follower = getattr(shell, "_bubble_follower", None)
    if follower is not None and follower.bubble is not None:
        bubbles.append(follower.bubble)
    for f in shell._bubble_followers.values():
        if f is not None and f.bubble is not None:
            bubbles.append(f.bubble)
    return [b for b in bubbles
            if b.isVisible() and getattr(b, "_raw_text", "") == text]


def _cleanup(shell):
    try:
        shell.stop()
    finally:
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- R6
def test_r6_slot0_return_does_not_replay_alert_or_sound(tmp_path, probes):
    shown, sounds = probes
    shell, _cfg = _make_slot_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.spawn_pet()
        child = _child(shell)
        assert shell.exit_pet(shell.sprite) is True
        assert shell.alert_target_sprite() is child

        shell.show_alert(ALERT_TEXT, alert_id="ap-1")
        assert [t for _b, t in shown].count(ALERT_TEXT) == 1
        assert len(_visible_copies(shell, ALERT_TEXT)) == 1
        shown_before, sounds_before = len(shown), len(sounds)

        shell.spawn_pet()  # slot0 回归（最低空闲身份 = 0）
        app.processEvents()
        assert shell.sprite is not None
        assert shell.alert_target_sprite() is shell.sprite

        replays = [t for _b, t in shown[shown_before:] if t == ALERT_TEXT]
        assert replays == [], "slot0 回归不得重放提醒"
        assert len(sounds) == sounds_before, "slot0 回归不得重复出声"
        assert len(_visible_copies(shell, ALERT_TEXT)) <= 1
        assert shell._alert_current is not None
        assert shell._alert_current.get("id") == "ap-1"
    finally:
        _cleanup(shell)


def test_r6_alert_resolvable_after_slot0_return(tmp_path, probes):
    """交接后按 id 收起：那一份可见提醒必须真的消失（不留孤儿气泡）。"""
    shell, _cfg = _make_slot_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.spawn_pet()
        assert shell.exit_pet(shell.sprite) is True
        shell.show_alert(ALERT_TEXT, alert_id="ap-1")
        assert len(_visible_copies(shell, ALERT_TEXT)) == 1

        shell.spawn_pet()
        app.processEvents()
        shell.resolve_alert("ap-1")
        app.processEvents()
        assert shell._alert_current is None
        assert _visible_copies(shell, ALERT_TEXT) == [], "收起后不得残留可见提醒"
    finally:
        _cleanup(shell)


def test_r6_slot0_exit_moves_alert_without_sound_or_duplicate(tmp_path, probes):
    shown, sounds = probes
    shell, _cfg = _make_slot_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.spawn_pet()
        child = _child(shell)
        assert shell.alert_target_sprite() is shell.sprite

        shell.show_alert(ALERT_TEXT, alert_id="ap-2")
        assert len(_visible_copies(shell, ALERT_TEXT)) == 1
        shown_before, sounds_before = len(shown), len(sounds)

        assert shell.exit_pet(shell.sprite) is True
        app.processEvents()
        assert shell.alert_target_sprite() is child

        moved = [t for _b, t in shown[shown_before:] if t == ALERT_TEXT]
        assert len(moved) <= 1, "迁移最多重挂一次"
        assert len(sounds) == sounds_before, "slot0 退出不得出声"
        assert len(_visible_copies(shell, ALERT_TEXT)) <= 1, "同一提醒最多一份可见"

        shell.resolve_alert("ap-2")
        app.processEvents()
        assert _visible_copies(shell, ALERT_TEXT) == []
    finally:
        _cleanup(shell)


def test_r6_hidden_slot0_return_does_not_replay(tmp_path, probes):
    """slot0 逐只隐藏（非退出）→ 提醒落子宠 → slot0 再显示：不重放、不出声。"""
    shown, sounds = probes
    shell, _cfg = _make_slot_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.spawn_pet()
        child = _child(shell)
        main = shell.sprite
        shell.set_sprite_visible(main, False)
        assert shell.alert_target_sprite() is child

        shell.show_alert(ALERT_TEXT, alert_id="ap-3")
        assert len(_visible_copies(shell, ALERT_TEXT)) == 1
        shown_before, sounds_before = len(shown), len(sounds)

        shell.set_sprite_visible(main, True)
        app.processEvents()
        assert shell.alert_target_sprite() is main
        assert [t for _b, t in shown[shown_before:] if t == ALERT_TEXT] == []
        assert len(sounds) == sounds_before
        assert len(_visible_copies(shell, ALERT_TEXT)) <= 1
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- R7
def test_r7_same_text_distinct_ids_both_presented_in_order(tmp_path, probes):
    shown, _sounds = probes
    shell, _cfg = _make_slot_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.show_alert(ALERT_TEXT, alert_id="a1")
        shell.show_alert(ALERT_TEXT, alert_id="a2")
        assert shell._alert_current["id"] == "a1"
        assert [q["id"] for q in shell._alert_queue] == ["a2"]
        assert [t for _b, t in shown].count(ALERT_TEXT) == 1

        shell.resolve_alert("a1")
        app.processEvents()
        assert shell._alert_current is not None
        assert shell._alert_current["id"] == "a2"
        assert [t for _b, t in shown].count(ALERT_TEXT) >= 2, "第二条同文本提醒必须呈现"
        assert len(_visible_copies(shell, ALERT_TEXT)) == 1

        shell.resolve_alert("a2")
        app.processEvents()
        assert shell._alert_current is None
        assert not shell._alert_queue
        assert _visible_copies(shell, ALERT_TEXT) == []
    finally:
        _cleanup(shell)


def test_r7_resolving_queued_twin_keeps_current(tmp_path, probes):
    shell, _cfg = _make_slot_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.show_alert(ALERT_TEXT, alert_id="a1")
        shell.show_alert(ALERT_TEXT, alert_id="a2")
        shell.resolve_alert("a2")
        assert shell._alert_current["id"] == "a1"
        assert not shell._alert_queue
        assert len(_visible_copies(shell, ALERT_TEXT)) == 1
    finally:
        _cleanup(shell)


def test_r7_same_id_merges(tmp_path, probes):
    shell, _cfg = _make_slot_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.show_alert(ALERT_TEXT, alert_id="a1")
        shell.show_alert(ALERT_TEXT + "（更新）", alert_id="a1")
        assert shell._alert_current["id"] == "a1"
        assert shell._alert_current["text"] == ALERT_TEXT + "（更新）"
        assert not shell._alert_queue

        shell.show_alert("排队旧文案", alert_id="b1")
        shell.show_alert("排队新文案", alert_id="b1")
        assert [q["text"] for q in shell._alert_queue] == ["排队新文案"]
    finally:
        _cleanup(shell)


def test_r7_same_text_without_id_not_deduped(tmp_path, probes):
    shell, _cfg = _make_slot_shell(tmp_path)
    try:
        shell.overlay.show()
        shell.show_alert(ALERT_TEXT)
        shell.show_alert(ALERT_TEXT)
        assert shell._alert_current["text"] == ALERT_TEXT
        assert [q["text"] for q in shell._alert_queue] == [ALERT_TEXT]
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- R7 agent_link
def _make_link_mgr(tmp_path, agent_link_cfg=None):
    from pet.agent_link import AgentLinkManager
    from pet.config import Config

    bubbles: list[str] = []

    class DummyWin:
        cats = {"acts": ["写代码"]}
        idles = ["待机呼吸"]
        _bubble_busy_until = 0.0

        def isVisible(self):  # noqa: N802
            return True

        def _switch(self, name):
            pass

        def show_bubble(self, text, duration_ms=3000):
            bubbles.append(text)

        def _pick(self, lst):
            return lst[0]

        def request_link_anim(self, name):
            pass

        def request_link_idle(self):
            pass

    cfg = Config(base=tmp_path)
    data = cfg.data
    data["dialogue_mode"] = "custom"
    data["dialogue_phrases"] = {"activity.read": ["同一句过程汇报"]}
    if agent_link_cfg is not None:
        data["agent_link"] = {**data.get("agent_link", {}), **agent_link_cfg}
    cfg.save()
    clock = [1000.0]
    mgr = AgentLinkManager(DummyWin(), cfg, min_interval=2.0, clock=lambda: clock[0])
    return mgr, bubbles, clock


def test_r7_agent_link_identical_activity_after_cooldown_both_surface(tmp_path):
    mgr, bubbles, clock = _make_link_mgr(tmp_path)
    mgr._on_agent_activity("dsh", "read")
    assert bubbles == ["同一句过程汇报"]

    clock[0] += 30.0  # 同文案 60s 冷却窗内：按文档应被节流
    mgr._on_agent_activity("dsh", "read")
    assert len(bubbles) == 1

    clock[0] += mgr._ACTIVITY_SAME_LABEL + 1.0  # 越过冷却
    mgr._on_agent_activity("dsh", "read")
    assert bubbles == ["同一句过程汇报", "同一句过程汇报"]


def test_r7_agent_link_repeated_sound_after_cooldown_plays_again(tmp_path, monkeypatch):
    from pet import agent_link

    played: list[object] = []
    monkeypatch.setattr(agent_link, "play_sound",
                        lambda path, volume=1.0: played.append(path) or True)
    mgr, _bubbles, clock = _make_link_mgr(
        tmp_path, {"sound_enabled": True, "sound_cooldown_seconds": 2.0})
    if agent_link.resolve_builtin_sound("builtin:agent-done") is None:
        pytest.skip("内置 agent-done 音效缺失")
    mgr._emit_sound("done", "dsh")
    assert len(played) == 1
    clock[0] += 1.0  # 冷却窗内
    mgr._emit_sound("done", "dsh")
    assert len(played) == 1
    clock[0] += 2.5  # 越过冷却
    mgr._emit_sound("done", "dsh")
    assert len(played) == 2
