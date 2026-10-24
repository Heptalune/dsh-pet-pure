# -*- coding: utf-8 -*-
"""AI 对话全局记忆（档 A：手动钉记忆）测试。

覆盖设计稿 CHAT-GLOBAL-MEMORY.md §7 验收要点：
- 存储：写入/读取往返、文本去重 hits+1、容量淘汰（101 条 → 100，hits 少 + 最老先走）、
  损坏 JSON 降级空、读失败时 add 不写回空快照、跨进程锁（同文件两个实例互斥）；
- 注入：有记忆时 system 含「关于用户的记忆」段且有硬字符上限；
  chat_memory_enabled=False / 无记忆时 build_messages 输出与现状逐字节一致；
- 指令：「记住：我不吃辣」→ 入记忆 + 确认气泡 + 不发网络；空指令 → 不入记忆有提示；
- 钉为记忆：modern 工具按钮 / legacy 右键菜单入口钉入后 memory.json 有条目（source=manual）；
- 设置组：chat_memory_enabled 开启/关闭写回 + reload 白名单采纳、清空走确认。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication

from pet.chat.memory import (
    ARCHIVE_DUP_RATIO,
    INJECT_CHAR_LIMIT,
    MAX_ENTRIES,
    MAX_HIT_WRITE_ATTEMPTS,
    MAX_TOTAL_CHARS,
    ChatMemory,
    _hit_queue_for,
    injection_text,
    memory_path,
    parse_remember_command,
    render_injection,
)
from pet.chat.memory_recall import TOPIC_ARCHIVE_DAYS, is_archived, recall
from pet.chat.prompt import PromptBuilder


def _qapp():
    app = QApplication.instance()
    return app or QApplication([])


def _clock(start=datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)):
    """每次调用前进 1 分钟的可注入时钟（created_at 顺序可控）。"""
    state = {"now": start}

    def tick():
        value = state["now"]
        state["now"] = value + timedelta(minutes=1)
        return value

    return tick


# ---------------------------------------------------------------- 存储
def test_add_and_load_round_trip(tmp_path):
    mem = ChatMemory(tmp_path / "memory.json", clock=_clock())
    entry, created = mem.add("我不吃辣")
    assert created is True
    assert entry["text"] == "我不吃辣"
    assert entry["source"] == "manual"
    assert entry["hits"] == 1
    assert entry["id"]
    entries = mem.load()
    assert [e["text"] for e in entries] == ["我不吃辣"]
    # 落盘结构：{version, entries:[...]}
    raw = json.loads((tmp_path / "memory.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert isinstance(raw["entries"], list)


def test_add_dedup_bumps_hits_and_refreshes_updated_at(tmp_path):
    mem = ChatMemory(tmp_path / "memory.json", clock=_clock())
    first, _ = mem.add("周三有课")
    again, created = mem.add("周三有课")
    assert created is False
    assert again["id"] == first["id"]  # 不新增条目
    assert again["hits"] == 2
    assert again["created_at"] == first["created_at"]
    assert again["updated_at"] > first["updated_at"]
    assert len(mem.load()) == 1


def test_add_rejects_empty_text(tmp_path):
    mem = ChatMemory(tmp_path / "memory.json", clock=_clock())
    assert mem.add("") is None
    assert mem.add("   ") is None
    assert mem.load() == []
    assert not (tmp_path / "memory.json").exists()


def test_entry_count_cap_evicts_low_hits_then_oldest(tmp_path):
    mem = ChatMemory(tmp_path / "memory.json", clock=_clock())
    mem.add("常用偏好")  # 最老，但 hits 高
    mem.add("常用偏好")
    mem.add("常用偏好")
    for index in range(MAX_ENTRIES):
        mem.add(f"事实-{index}")
    entries = mem.load()
    assert len(entries) == MAX_ENTRIES  # 101 → 100
    texts = {e["text"] for e in entries}
    assert "常用偏好" in texts  # hits 高，哪怕最老也留下
    assert "事实-0" not in texts  # hits 同为 1 时最老的先走
    assert f"事实-{MAX_ENTRIES - 1}" in texts


def test_total_char_cap_evicts_until_budget(tmp_path):
    mem = ChatMemory(tmp_path / "memory.json", clock=_clock())
    for index in range(MAX_ENTRIES):
        mem.add(f"{index:03d}-" + "x" * 400)
    entries = mem.load()
    total = sum(len(e["text"]) for e in entries)
    assert total <= MAX_TOTAL_CHARS
    assert len(entries) < MAX_ENTRIES


def test_corrupt_json_degrades_to_empty(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("{not json", encoding="utf-8")
    mem = ChatMemory(path, clock=_clock())
    assert mem.load() == []


def test_add_aborts_on_read_failure_without_writing_empty_snapshot(tmp_path):
    """读失败（I/O 错误）与"没有记忆"是两个事实：add 放弃修改，绝不把
    只含新条目的快照盖到盘上旧记忆头上（沿用 proactive_memory 的三层锁口径）。"""
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=_clock())
    mem.add("旧记忆")
    before = path.read_text(encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")  # 非瞬时冲突：不重试、如实失败

    mem._retry_with_narrow_lock = boom
    assert mem.add("新记忆") is None
    mem2 = ChatMemory(path, clock=_clock())
    assert [e["text"] for e in mem2.load()] == ["旧记忆"]
    assert path.read_text(encoding="utf-8") == before


def test_cross_process_lock_file_blocks_second_writer(tmp_path):
    """跨进程锁存在性：旁路 .lock 被持有时，另一个实例的 add 放弃修改。"""
    path = tmp_path / "memory.json"
    first = ChatMemory(path, clock=_clock(), lock_timeout_ms=50)
    second = ChatMemory(path, clock=_clock(), lock_timeout_ms=50)
    assert first._lock_file == second._lock_file
    assert first._lock_file.name == "memory.json.lock"
    held = QLockFile(str(first._lock_file))
    assert held.tryLock(100)
    try:
        assert second.add("抢锁写入") is None
        assert not path.exists()  # 没拿到锁：无锁写入绝不发生
    finally:
        held.unlock()
    # 锁释放后同一实例可正常写入
    assert second.add("释放后写入") is not None


def test_two_instances_interleaved_adds_keep_both(tmp_path):
    path = tmp_path / "memory.json"
    one = ChatMemory(path, clock=_clock())
    two = ChatMemory(path, clock=_clock())
    one.add("来自实例一")
    two.add("来自实例二")
    texts = {e["text"] for e in one.load()}
    assert texts == {"来自实例一", "来自实例二"}


def test_memory_file_is_never_listed_or_renamed_as_session(tmp_path):
    """memory.json 与会话文件同目录：会话列表必须跳过它——否则 load() 会把它
    当损坏会话改名成 .corrupt-<ts>.json，记忆文件直接消失（真实缺陷路径）。"""
    from pet.chat.session_store import SessionStore

    path = memory_path(tmp_path, "", "shenshen")
    ChatMemory(path).add("我不吃辣")
    store = SessionStore(tmp_path, "")
    assert store.list("shenshen") == []
    assert path.exists()  # 没被当损坏会话改名
    assert list(path.parent.glob("memory.corrupt-*.json")) == []


def test_remove_entry(tmp_path):
    mem = ChatMemory(tmp_path / "memory.json", clock=_clock())
    entry, _ = mem.add("要删的")
    assert mem.remove(entry["id"]) is True
    assert mem.load() == []
    assert mem.remove("不存在的-id") is True  # 目标状态已达成


def test_clear_removes_file(tmp_path):
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=_clock())
    mem.add("a")
    mem.add("b")
    assert mem.clear() is True
    assert not path.exists()
    assert mem.clear() is True  # 本来就不存在 = 已清空


# ---------------------------------------------------------------- v2f L4/L5/L6
def test_apply_ops_noop_does_not_rewrite_file(tmp_path):
    """v2f L4：FORGET 未命中 = 空操作，不写盘且返回 False（mtime 不变）。"""
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=_clock())
    mem.add("周三有课")
    before_mtime = path.stat().st_mtime_ns
    before_text = path.read_text(encoding="utf-8")

    assert mem.apply_ops(
        [{"op": "FORGET", "text": "不存在的记忆"}], source="auto") is False
    assert path.stat().st_mtime_ns == before_mtime
    assert path.read_text(encoding="utf-8") == before_text


def test_add_dedup_refresh_enforces_budget(tmp_path):
    """v2f L5：盘上 12 条共 6000 字符时，去重刷新也要收敛到 4000 预算内。"""
    path = tmp_path / "memory.json"
    base = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    texts = [f"{index:02d}-" + "长" * 497 for index in range(12)]
    assert sum(len(text) for text in texts) == 6000
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "id": f"old-{index}",
                        "text": text,
                        "source": "manual",
                        "layer": "fact",
                        "created_at": (base - timedelta(minutes=index)).isoformat(),
                        "updated_at": (base - timedelta(minutes=index)).isoformat(),
                        "hits": 1,
                    }
                    for index, text in enumerate(texts)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mem = ChatMemory(path, clock=_clock())
    refreshed, created = mem.add(texts[0])
    assert created is False
    entries = mem.load()
    assert texts[0] in {entry["text"] for entry in entries}
    assert sum(len(entry["text"]) for entry in entries) <= MAX_TOTAL_CHARS
    assert len(entries) == 8  # 12 × 500 → 8 × 500


def test_note_recalled_hit_increments_persisted_hits(tmp_path):
    """v2f L6：命中落盘同时 hits += 1（淘汰/注入排序都看 hits）。"""
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=lambda: datetime.now().astimezone())
    topic, _ = mem.add("那个 Bug", layer="topic")
    assert topic["hits"] == 1
    mem.note_recalled([topic["id"]])
    assert mem.flush_hits(5.0) is True
    persisted = next(
        e for e in json.loads(path.read_text(encoding="utf-8"))["entries"]
        if e["id"] == topic["id"])
    assert persisted["hits"] == 2


# ---------------------------------------------------------------- v2c M1/M3
def test_auto_apply_ops_cannot_unarchive_topic_by_changing_layer(tmp_path):
    """M1：自动提炼把归档 topic 改层成 fact 不能绕过归档（仍不注入）。"""
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=_clock())
    topic, _ = mem.add("那个鉴权 bug 用 token", layer="topic")
    assert mem.set_archived(topic["id"], True) is True

    assert mem.apply_ops(
        [{"op": "ADD", "text": "那个鉴权 bug 用 token", "layer": "fact"}],
        source="auto",
    ) is True

    loaded = mem.load()[0]
    assert loaded["layer"] == "topic"  # explicit_layer 被忽略
    assert loaded["archived"] is True  # 归档态未丢
    assert is_archived(loaded) is True
    assert recall([loaded], "token", turn_index=2).entries == ()


def test_auto_apply_ops_does_not_refresh_implicit_stale_topic(tmp_path):
    """M3：盘上还没有 archived 键的隐式超期 topic，自动重申也不得复活。"""
    path = tmp_path / "memory.json"
    stale_at = datetime.now().astimezone() - timedelta(days=TOPIC_ARCHIVE_DAYS + 1)
    old = ChatMemory(path, clock=lambda: stale_at)
    topic, _ = old.add("那个鉴权 bug 用 token", layer="topic")
    before = next(
        e for e in json.loads(path.read_text(encoding="utf-8"))["entries"]
        if e["id"] == topic["id"])
    assert "archived" not in before  # 隐式超期：盘上没有 archived 键

    later = ChatMemory(path, clock=lambda: datetime.now().astimezone())
    assert later.apply_ops(
        [{"op": "ADD", "text": "那个鉴权 bug 用 token", "layer": "fact"}],
        source="auto",
    ) is True

    after = next(
        e for e in json.loads(path.read_text(encoding="utf-8"))["entries"]
        if e["id"] == topic["id"])
    assert after["layer"] == "topic"
    assert after["last_hit_at"] == before["last_hit_at"]
    assert after["updated_at"] == before["updated_at"]
    assert "archived" not in after
    assert is_archived(after) is True
    assert recall([later.load()[0]], "token", turn_index=2).entries == ()


def test_auto_apply_ops_can_still_forget_archived_topic(tmp_path):
    """M1：FORGET（删除）是允许的——用户/模型要求忘掉归档条目。"""
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=_clock())
    topic, _ = mem.add("那个鉴权 bug 用 token", layer="topic")
    assert mem.set_archived(topic["id"], True) is True
    assert mem.apply_ops(
        [{"op": "FORGET", "text": "那个鉴权 bug 用 token"}], source="auto") is True
    assert mem.load() == []


def test_auto_reaffirm_never_demotes_manual_user_layer(tmp_path):
    """发版终审（高）：自动重申带 layer=fact 也不得把手动 user 偏好降级。

    降级后该偏好不再恒定注入——render_user_snapshot 立即变空。"""
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=_clock())
    entry, created = mem.add("我不吃辣", layer="user")
    assert created is True

    assert mem.apply_ops(
        [{"op": "ADD", "text": "我不吃辣", "layer": "fact"}], source="auto") is True

    loaded = next(e for e in mem.load() if e["id"] == entry["id"])
    assert loaded["layer"] == "user"
    assert loaded["hits"] == 2
    from pet.chat.memory_recall import render_user_snapshot
    assert "我不吃辣" in render_user_snapshot(mem.load())
    # 手动通道仍然可以改层（语义不变）。
    assert mem.apply_ops(
        [{"op": "ADD", "text": "我不吃辣", "layer": "fact"}], source="manual") is True
    assert next(e for e in mem.load() if e["id"] == entry["id"])["layer"] == "fact"


# ---------------------------------------------------------------- v2f N1/N3
def test_auto_reaffirm_never_renews_topic_activity(tmp_path):
    """v2f N1：第 13/26/39 天自动重申只 +hits，第 15 天仍归档、recall 不命中。"""
    path = tmp_path / "memory.json"
    state = {"now": datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)}
    mem = ChatMemory(path, clock=lambda: state["now"])
    topic, _ = mem.add("那个鉴权 bug 用 token", layer="topic")
    created = topic["created_at"]

    for day in (13, 26, 39):
        state["now"] = topic_at = datetime.fromisoformat(created) + timedelta(days=day)
        assert mem.apply_ops(
            [{"op": "ADD", "text": "那个鉴权 bug 用 token"}], source="auto"
        ) is True
        persisted = next(
            e for e in json.loads(path.read_text(encoding="utf-8"))["entries"]
            if e["id"] == topic["id"])
        assert persisted["last_hit_at"] == topic["last_hit_at"]
        assert persisted["updated_at"] == topic["updated_at"]

    day15 = datetime.fromisoformat(created) + timedelta(days=15)
    loaded = mem.load()
    assert is_archived(loaded[0], day15) is True
    assert recall(loaded, "token", now=day15, turn_index=2).entries == ()

    day52 = datetime.fromisoformat(created) + timedelta(days=52)
    assert is_archived(loaded[0], day52) is True
    assert recall(loaded, "token", now=day52, turn_index=2).entries == ()
    assert topic_at > day15  # 重申确实发生在第 15 天之后


def test_auto_add_near_duplicate_of_archived_memory_is_discarded(tmp_path):
    """v2f N3：换说法新建会被归档 Jaccard 挡住；手动来源不受限。"""
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=lambda: datetime.now().astimezone())
    archived, _ = mem.add("我在准备十月底的考试", layer="topic")
    assert mem.set_archived(archived["id"], True) is True
    before = path.read_text(encoding="utf-8")

    assert ARCHIVE_DUP_RATIO == 0.6
    assert mem.apply_ops(
        [{"op": "ADD", "text": "在准备十月底的考试（考完要复盘）"}],
        source="auto",
    ) is False
    assert path.read_text(encoding="utf-8") == before
    loaded = mem.load()
    assert len(loaded) == 1
    assert recall(loaded, "十月底的考试", turn_index=2).entries == ()
    assert "考完要复盘" not in render_injection(loaded)

    # 手动来源不受归档去重限制（手动钉仍是恢复/新建的合法入口）。
    assert mem.apply_ops(
        [{"op": "ADD", "text": "在准备十月底的考试（考完要复盘）"}],
        source="manual",
    ) is True
    assert any(
        "考完要复盘" in entry["text"] for entry in mem.load())


# ---------------------------------------------------------------- 指令解析
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("记住：我不吃辣", "我不吃辣"),
        ("记住:我不吃辣", "我不吃辣"),  # 英文冒号也认
        ("  记住：我不吃辣  ", "我不吃辣"),
        ("记住：", ""),  # 空指令：返回空串（与"不是指令"区分）
        ("记住：  ", ""),
        ("请记住：我不吃辣", None),
        ("记住我", None),
        ("你好", None),
        ("", None),
    ],
)
def test_parse_remember_command(raw, expected):
    assert parse_remember_command(raw) == expected


# ---------------------------------------------------------------- 注入渲染
def _entries(texts):
    base = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    return [
        {
            "id": f"id-{index}",
            "text": text,
            "source": "manual",
            "created_at": (base + timedelta(minutes=index)).isoformat(),
            "updated_at": (base + timedelta(minutes=index)).isoformat(),
            "hits": 1,
        }
        for index, text in enumerate(texts)
    ]


def test_render_injection_lists_entries_under_header():
    section = render_injection(_entries(["我不吃辣", "周三有课"]))
    assert "关于用户的记忆" in section
    assert "- 我不吃辣" in section
    assert "- 周三有课" in section


def test_render_injection_empty_entries_is_empty():
    assert render_injection([]) == ""


def test_render_injection_excludes_archived_by_default():
    """D：归档 topic 默认不喂回提炼/注入，显式 include_archived 才包含。"""
    entries = _entries(["旧话题", "新话题"])
    entries[0]["layer"] = "topic"
    entries[0]["archived"] = True
    assert "旧话题" not in render_injection(entries)
    assert "新话题" in render_injection(entries)
    assert "旧话题" in render_injection(entries, include_archived=True)


def test_render_injection_hard_char_limit():
    entries = _entries([f"第{i}条" + "很长" * 100 for i in range(50)])
    section = render_injection(entries)
    assert len(section) <= INJECT_CHAR_LIMIT
    assert section  # 预算内至少保留头部 + 高优先级条目


def test_render_injection_prefers_high_hits():
    entries = _entries(["冷门" + "长" * 130, "热门" + "长" * 130])
    entries[0]["hits"] = 1
    entries[1]["hits"] = 9
    section = render_injection(entries, char_limit=120)  # 下限 200：只装得下一条
    assert "热门" in section
    assert "冷门" not in section  # 预算只够一条时 hits 高的优先


def test_injection_text_disabled_is_empty(tmp_path):
    path = memory_path(tmp_path, "", "shenshen")
    mem = ChatMemory(path, clock=_clock())
    mem.add("我不吃辣", layer="user")
    mem.add("周三有课")  # fact：不再恒注，留给第二轮起的按需命中
    assert injection_text(False, tmp_path, "", "shenshen") == ""
    section = injection_text(True, tmp_path, "", "shenshen")
    assert "我不吃辣" in section
    assert "周三有课" not in section
    assert injection_text(True, tmp_path, "", "别的角色") == ""  # 按角色隔离


# ---------------------------------------------------------------- PromptBuilder 注入
def _settings():
    from pet.chat.models import ChatSettings

    return ChatSettings()


def test_build_messages_appends_memory_section():
    builder = PromptBuilder()
    now = datetime(2026, 10, 21, 12, 0).astimezone()
    messages = builder.build_messages(
        _settings(), "cat", [], "你好", now=now,
        memory_text="【关于用户的记忆】\n- 我不吃辣",
    )
    system = messages[0]["content"]
    assert system.endswith("【关于用户的记忆】\n- 我不吃辣")


@pytest.mark.parametrize("memory_text", [None, "", "   "])
def test_build_messages_byte_identical_without_memory(memory_text):
    """无记忆 / 空注入：与现状逐字节一致（回归钉死）。"""
    from pet.chat.models import ChatMessage

    builder = PromptBuilder()
    history = [ChatMessage("user", "上一条"), ChatMessage("assistant", "回复")]
    now = datetime(2026, 10, 21, 12, 0).astimezone()
    baseline = builder.build_messages(_settings(), "cat", history, "你好", now=now)
    with_memory = builder.build_messages(
        _settings(), "cat", history, "你好", now=now, memory_text=memory_text
    )
    assert with_memory == baseline


# ---------------------------------------------------------------- 聊天窗：指令 + 钉为记忆
def _modern_window(tmp_path, monkeypatch):
    from pet.config import Config
    from pet.chat.widgets import ChatWindow

    _qapp()
    window = ChatWindow(Config(tmp_path), "shenshen")
    sent = []
    monkeypatch.setattr(window.service, "send", lambda *a, **k: sent.append(a))
    return window, sent


def test_remember_command_stores_memory_without_network(tmp_path, monkeypatch):
    window, sent = _modern_window(tmp_path, monkeypatch)
    try:
        window.input.setPlainText("记住：我不吃辣")
        window.send_message()
        assert sent == []  # 不发网络
        entries = _win_memory(window).load()
        assert [e["text"] for e in entries] == ["我不吃辣"]
        assert entries[0]["source"] == "manual"
        # 指令与确认都入会话记录（user + assistant，不落模型）
        roles = [m.role for m in window.session.messages]
        assert roles == ["user", "assistant"]
        assert window.session.messages[0].content == "记住：我不吃辣"
        assert "我不吃辣" in window.session.messages[1].content
        # 对话里有确认气泡
        assert window._bubbles[-1].role == "assistant"
        assert "记住" in window._bubbles[-1].content
        assert window.service.busy is False
    finally:
        window.close()
        window.deleteLater()


def test_remember_command_empty_text_gives_hint(tmp_path, monkeypatch):
    window, sent = _modern_window(tmp_path, monkeypatch)
    try:
        window.input.setPlainText("记住：")
        window.send_message()
        assert sent == []
        assert not (window.store.root / "shenshen" / "memory.json").exists()
        assert window._bubbles[-1].role == "assistant"
        assert window._bubbles[-1].content  # 有提示气泡
    finally:
        window.close()
        window.deleteLater()


def test_normal_message_still_goes_to_network(tmp_path, monkeypatch):
    window, sent = _modern_window(tmp_path, monkeypatch)
    try:
        window.input.setPlainText("今天天气怎么样")
        window.send_message()
        assert len(sent) == 1  # 普通消息不受影响
    finally:
        window.close()
        window.deleteLater()


def test_pin_bubble_to_memory_modern(tmp_path, monkeypatch):
    window, _sent = _modern_window(tmp_path, monkeypatch)
    try:
        bubble = window._add("assistant", "周三下午我有课")
        bubble.pin_button.click()
        entries = _win_memory(window).load()
        assert [e["text"] for e in entries] == ["周三下午我有课"]
        assert entries[0]["source"] == "manual"
        # 重复钉同一条：不新增，hits+1
        bubble.pin_button.click()
        entries = _win_memory(window).load()
        assert len(entries) == 1
        assert entries[0]["hits"] == 2
    finally:
        window.close()
        window.deleteLater()


def test_legacy_window_remember_command_and_pin(tmp_path, monkeypatch):
    from pet.config import Config
    from pet.chat.legacy_widgets import ChatWindow

    _qapp()
    window = ChatWindow(Config(tmp_path), "shenshen")
    sent = []
    monkeypatch.setattr(window.service, "send", lambda *a, **k: sent.append(a))
    try:
        window.input.setPlainText("记住:我不吃辣")
        window.send_message()
        assert sent == []
        entries = _win_memory(window).load()
        assert [e["text"] for e in entries] == ["我不吃辣"]
        # legacy 镜像钉为记忆：气泡 pin 入口 → memory.json 有条目
        bubble = window._add("assistant", " pinned 内容 ")
        window._pin_bubble(bubble)
        entries = _win_memory(window).load()
        assert "pinned 内容" in {e["text"] for e in entries}
    finally:
        window.close()
        window.deleteLater()


def test_begin_generation_injects_memory_into_system(tmp_path, monkeypatch):
    """真实生成路径：首轮只注 user 快照；第二轮起 fact 按需命中；关开关回到基线。"""
    window, sent = _modern_window(tmp_path, monkeypatch)
    try:
        _win_memory(window).add("我不吃辣", layer="user")
        _win_memory(window).add("周三有课", layer="fact")
        window.input.setPlainText("晚上吃什么")
        window.send_message()
        assert len(sent) == 1
        system = sent[0][0][0]["content"]
        assert "关于用户的记忆" in system
        assert "我不吃辣" in system
        assert "周三有课" not in system  # 首轮不命中 fact
        # 第二轮：带历史，fact 命中注入
        window.input.setPlainText("周三有课吗")
        window.send_message()
        assert len(sent) == 2
        second = sent[1][0][0]["content"]
        assert "周三有课" in second
        # 关开关：注入段为空，与无记忆逐字节一致
        window.config.set("chat_memory_enabled", False)
        baseline = window.prompt_builder.build_messages(
            window.settings, window.character_id, [], "晚上吃什么"
        )
        window.input.setPlainText("晚上吃什么")
        window.send_message()
        assert len(sent) == 3
        assert sent[2][0][0]["content"] == baseline[0]["content"]
        assert "关于用户的记忆" not in sent[2][0][0]["content"]
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------- 设置组
def _settings_page(tmp_path, monkeypatch):
    import pet.modern_settings_dialog as settings_mod
    from pet.config import Config
    from pet.chat.ai_settings_page import _AiSettingsPage

    _qapp()
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    config = Config(tmp_path)
    page = _AiSettingsPage(config)
    return config, page


def test_settings_toggle_writes_back(tmp_path, monkeypatch):
    config, page = _settings_page(tmp_path, monkeypatch)
    try:
        assert page.chat_memory_enabled.isChecked() is True  # 默认开
        assert page.chat_memory_auto_extract.isChecked() is True  # 档 B 默认开（被动形成）
        page.chat_memory_enabled.setChecked(False)
        page.chat_memory_auto_extract.setChecked(False)
        page.save()
        assert config.get("chat_memory_enabled") is False
        assert config.get("chat_memory_auto_extract") is False
        # 重新打开读回关闭态
        from pet.chat.ai_settings_page import _AiSettingsPage

        page2 = _AiSettingsPage(config)
        assert page2.chat_memory_enabled.isChecked() is False
        assert page2.chat_memory_auto_extract.isChecked() is False
        page2.deleteLater()
    finally:
        page.deleteLater()


def test_settings_key_adopted_on_reload(tmp_path, monkeypatch):
    """reload 白名单：盘上值被采纳（非 NULL 接受键：显式 null 不采纳、保留内存现值）。"""
    config, _page = _settings_page(tmp_path, monkeypatch)
    config.set("chat_memory_enabled", False)
    assert config.save() is True
    config.set("chat_memory_enabled", True)
    config.reload()
    assert config.get("chat_memory_enabled") is False
    config.set("chat_memory_enabled", True)  # 内存为开
    raw = json.loads(config.path.read_text(encoding="utf-8"))
    raw["chat_memory_enabled"] = None
    config.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    config.reload()
    assert config.get("chat_memory_enabled") is True  # 显式 null 不被采纳（不被覆盖成 None/关）


def test_settings_memory_rows_claimed_in_ai_domain(tmp_path, monkeypatch):
    """「AI 与对话」域「记忆」组：两行进 claim 清单、挂在域页上，不掉「待分类」。"""
    import pet.modern_settings_dialog as settings_mod
    from pet.config import Config
    from pet.modern_settings_dialog import ModernSettingsDialog, SettingRow

    _qapp()
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    dlg = ModernSettingsDialog(Config(tmp_path), include_ai=True)
    try:
        pages = [dlg.pages.widget(i) for i in range(dlg.pages.count())]
        for key in ("chat_memory_enabled", "chat_memory_auto_extract", "chat_memory_manage"):
            row = dlg.findChild(SettingRow, f"settingRow_{key}")
            assert row is not None, key
            assert any(page.isAncestorOf(row) for page in pages), key
        from PySide6.QtWidgets import QLabel

        labels = [lb.text() for lb in dlg.findChildren(QLabel)]
        assert "记忆" in labels
        assert "待分类（开发期）" not in labels
    finally:
        dlg.close()
        dlg.deleteLater()


def test_settings_memory_manager_list_delete_and_clear(tmp_path, monkeypatch):
    config, page = _settings_page(tmp_path, monkeypatch)
    try:
        path = memory_path(config.dir, "", str(config.get("character")))
        mem = ChatMemory(path)
        first, _ = mem.add("我不吃辣")
        mem.add("周三有课")
        dialog = page._open_memory_manager()
        try:
            assert dialog._row_count() == 2
            # 单条删除
            dialog._delete_entry(first["id"])
            assert [e["text"] for e in mem.load()] == ["周三有课"]
            assert dialog._row_count() == 1
            # 全部清空：取消 → 不动
            monkeypatch.setattr(
                "pet.chat.ai_settings_page.QMessageBox.question",
                lambda *a, **k: _no_button(a[3] if len(a) > 3 else None),
            )
            dialog._clear_all()
            assert len(mem.load()) == 1
            # 全部清空：确认 → 文件删除
            monkeypatch.setattr(
                "pet.chat.ai_settings_page.QMessageBox.question",
                lambda *a, **k: _yes_button(a[3] if len(a) > 3 else None),
            )
            dialog._clear_all()
            assert mem.load() == []
            assert not path.exists()
            assert dialog._row_count() == 1  # 空态提示行
            assert dialog.clear_btn.isEnabled() is False
        finally:
            dialog.close()
            dialog.deleteLater()
    finally:
        page.deleteLater()


def test_memory_manager_groups_layers_changes_layer_and_restores_topic(tmp_path, monkeypatch):
    """UI：四层分组；单条下拉改层；归档 topic 灰显 + 恢复。"""
    from datetime import timedelta

    from PySide6.QtWidgets import QComboBox, QLabel, QPushButton

    from pet.chat.memory import LAYERS
    from pet.chat.memory_recall import is_archived

    config, page = _settings_page(tmp_path, monkeypatch)
    try:
        path = memory_path(config.dir, "", str(config.get("character")))
        stale_at = datetime.now().astimezone() - timedelta(days=16)
        stale_mem = ChatMemory(path, clock=lambda: stale_at)
        stale, _ = stale_mem.add("旧话题", layer="topic")
        mem = ChatMemory(path)
        user, _ = mem.add("我不吃辣", layer="user")
        fact, _ = mem.add("周三有课", layer="fact")
        mem.add("别再把配置删了", layer="lesson")
        dialog = page._open_memory_manager()
        try:
            assert dialog._row_count() == 4
            labels = [label.text() for label in dialog.findChildren(QLabel)]
            for text in ("用户偏好", "事实", "教训", "话题"):
                assert any(text in label for label in labels), text
            # 单条下拉改层：fact → lesson
            combo = dialog.findChild(QComboBox, f"chat-memory-layer-{fact['id']}")
            assert combo is not None
            combo.setCurrentIndex(LAYERS.index("lesson"))
            assert next(e for e in mem.load() if e["id"] == fact["id"])["layer"] == "lesson"
            # 归档 topic：灰显 + 恢复按钮，点恢复后不再归档
            archived_combo = dialog.findChild(QComboBox, f"chat-memory-layer-{stale['id']}")
            assert archived_combo is None or not archived_combo.isEnabled()  # E：归档行不可改层
            restore = dialog.findChild(QPushButton, f"chat-memory-restore-{stale['id']}")
            assert restore is not None
            restore.click()
            restored = next(e for e in mem.load() if e["id"] == stale["id"])
            assert restored["archived"] is False
            assert is_archived(restored, datetime.now().astimezone()) is False
        finally:
            dialog.close()
            dialog.deleteLater()
    finally:
        page.deleteLater()


# ---------------------------------------------------------------- v2c L1/L2
def test_hit_write_failure_keeps_pending_and_retries(tmp_path, monkeypatch):
    """L1：写盘失败不置 _last_write（不进 60s 去抖），批次留在 _pending 重试。"""
    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=_clock())
    topic, _ = mem.add("那个 Bug", layer="topic")
    hit_at = datetime.now().astimezone()

    real_write = ChatMemory._write

    def broken(self, entries):
        return False

    monkeypatch.setattr(ChatMemory, "_write", broken)
    mem.note_recalled([topic["id"]], now=hit_at)
    assert mem.flush_hits(0.5) is False

    queue = _hit_queue_for(path)
    with queue._cond:
        assert topic["id"] in queue._pending
        assert topic["id"] not in queue._last_write

    monkeypatch.setattr(ChatMemory, "_write", real_write)
    assert mem.flush_hits(5.0) is True
    persisted = next(
        e for e in json.loads(path.read_text(encoding="utf-8"))["entries"]
        if e["id"] == topic["id"])
    assert persisted["last_hit_at"] == hit_at.isoformat()


def test_hit_write_gives_up_after_five_failures_with_one_warning(
    tmp_path, monkeypatch, caplog
):
    """v2f L1：连续失败 5 次后放弃本批（flush True），只记一条无 traceback 的 warning。"""
    import pet.chat.memory as memory_mod

    path = tmp_path / "memory.json"
    mem = ChatMemory(path, clock=_clock())
    topic, _ = mem.add("那个 Bug", layer="topic")
    monkeypatch.setattr(ChatMemory, "_write", lambda self, entries: False)
    # 把退避压到毫秒级：仍走同一套指数退避/放弃逻辑，避免测试真等 15s。
    monkeypatch.setattr(memory_mod, "_FAIL_RETRY_SECONDS", 0.01)
    assert MAX_HIT_WRITE_ATTEMPTS == 5

    mem.note_recalled([topic["id"]], now=datetime.now().astimezone())
    with caplog.at_level(logging.WARNING, logger="pet.chat.memory"):
        assert mem.flush_hits(5.0) is True

    queue = _hit_queue_for(path)
    with queue._cond:
        assert queue._pending == {}
        assert queue._failures == 0
    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING and "放弃本批" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None


def test_about_to_quit_flushes_chat_memory_hits(tmp_path, monkeypatch):
    """L2：aboutToQuit 收口会调用 flush_pending_hits（带超时），不再丢去抖窗口。"""
    from pet import app as app_mod
    from pet.chat import memory as memory_mod
    from pet.chat import session_store as session_store_mod
    from pet.config import Config

    calls: list[float] = []
    monkeypatch.setattr(
        memory_mod, "flush_pending_hits",
        lambda timeout=2.0: calls.append(timeout) or 0,
    )
    # 退出收口的会话写盘 worker 与记忆 flush 无关，避免永久关闭影响同进程后续用例。
    monkeypatch.setattr(session_store_mod, "close_all_writers", lambda *a, **k: True)

    shell = app_mod.AppShell(_qapp(), Config(tmp_path), enable_chat=False)
    shell._teardown_fps_capture = lambda **kwargs: None
    shell._teardown_config_watcher = lambda: None
    try:
        shell._on_about_to_quit()
    finally:
        app_mod.AppShell._shutdown_live_for_tests()

    assert calls == [2.0], f"aboutToQuit 未调用记忆 flush：{calls}"


def _yes_button(_buttons):
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Yes


def _win_memory(window) -> ChatMemory:
    """窗口实际使用的那份记忆文件（config.dir = tmp_path / APP_DIR_NAME）。"""
    return ChatMemory(window.store.root / window.character_id / "memory.json")


def _no_button(_buttons):
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.No
