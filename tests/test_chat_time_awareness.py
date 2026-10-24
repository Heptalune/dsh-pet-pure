"""对话时间感知：历史用户消息带发送时间，system 附当前时间与距上条消息的间隔。

时间来自已落盘的 ChatMessage.created_at（旧会话文件本来就有该字段），
只在发给模型时附加，不改写会话文件。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pet.chat.models import ChatMessage, ChatSettings
from pet.chat.prompt import PromptBuilder, format_elapsed
from pet.chat.session_store import SessionStore

TZ8 = timezone(timedelta(hours=8))


def _local(dt):
    """测试按本机时区格式化期望值（被测代码统一转本地时区）。"""
    local = dt.astimezone()
    return f"{local:%Y-%m-%d} 周{'一二三四五六日'[local.weekday()]} {local:%H:%M}"


def _settings():
    return ChatSettings(default_system_prompt="你是桌宠")


def test_history_user_messages_are_stamped_and_system_knows_now_and_gap():
    sent = datetime(2026, 9, 12, 11, 13, tzinfo=timezone.utc)
    replied = sent + timedelta(seconds=20)
    now = datetime(2026, 10, 12, 2, 30, tzinfo=TZ8)
    history = [
        ChatMessage("user", "早上好", sent.isoformat()),
        ChatMessage("assistant", "早呀", replied.isoformat()),
    ]
    messages = PromptBuilder().build_messages(_settings(), "cat", history, "我回来啦", now=now)

    system = messages[0]["content"]
    assert system.startswith("你是桌宠\n\n【时间信息】")
    assert f"当前时间：{_local(now)}" in system
    gap = format_elapsed((now - replied).total_seconds())
    assert f"距离上一条消息（{_local(replied)}）已经过去约 {gap}" in system
    assert gap.startswith("29 天")
    # 用户历史消息带时间标记；助手消息原样（防止模型模仿标记）；本轮输入原样
    assert messages[1] == {"role": "user", "content": f"[{_local(sent)}] 早上好"}
    assert messages[2] == {"role": "assistant", "content": "早呀"}
    assert messages[3] == {"role": "user", "content": "我回来啦"}


def test_continuous_conversation_has_no_gap_note():
    now = datetime(2026, 10, 12, 2, 30, tzinfo=TZ8)
    history = [ChatMessage("user", "hi", (now - timedelta(minutes=3)).isoformat())]
    system = PromptBuilder().build_messages(_settings(), "cat", history, "again", now=now)[0]["content"]
    assert "当前时间" in system
    assert "已经过去" not in system


def test_new_session_and_unparseable_timestamps_degrade_gracefully():
    now = datetime(2026, 10, 12, 2, 30, tzinfo=TZ8)
    messages = PromptBuilder().build_messages(_settings(), "cat", [], "hi", now=now)
    assert "当前时间" in messages[0]["content"] and "已经过去" not in messages[0]["content"]

    broken = [ChatMessage("user", "旧消息", "not-a-date")]
    messages = PromptBuilder().build_messages(_settings(), "cat", broken, "hi", now=now)
    assert messages[1] == {"role": "user", "content": "旧消息"}


def test_format_elapsed():
    assert format_elapsed(5 * 60) == "5 分钟"
    assert format_elapsed(3 * 3600) == "3 小时"
    assert format_elapsed(3 * 3600 + 15 * 60) == "3 小时 15 分钟"
    assert format_elapsed(2 * 86400 + 3600) == "2 天 1 小时"


def test_existing_session_file_gets_timestamps_without_being_rewritten(tmp_path):
    """升级前写下的会话文件（与用户磁盘上格式一致）直接可用，且构建消息不改写文件。"""
    path = tmp_path / "sessions" / "cat" / "abc.json"
    path.parent.mkdir(parents=True)
    raw = {
        "session_id": "abc", "character_id": "cat", "provider_id": "p", "system_prompt": "s",
        "created_at": "2026-09-12T08:31:40.200080+00:00", "updated_at": "2026-09-12T08:32:00+00:00",
        "title": "", "pinned": False,
        "messages": [
            {"role": "user", "content": "旧问题", "created_at": "2026-09-12T08:31:40.200080+00:00", "message_id": "m1"},
            {"role": "assistant", "content": "旧回答", "created_at": "2026-09-12T08:31:50.000000+00:00", "message_id": "m2"},
        ],
    }
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    session = SessionStore(tmp_path).load("abc")
    assert session is not None
    now = datetime(2026, 10, 12, 2, 30, tzinfo=TZ8)
    messages = PromptBuilder().build_messages(_settings(), "cat", session.messages, "新问题", now=now)
    stamp = _local(datetime(2026, 9, 12, 8, 31, 40, tzinfo=timezone.utc))
    assert messages[1]["content"] == f"[{stamp}] 旧问题"
    assert "已经过去约 29 天" in messages[0]["content"]
    assert path.read_bytes() == before
    assert session.messages[0].content == "旧问题"


def test_new_messages_record_timestamps():
    msg = ChatMessage("user", "hi")
    parsed = datetime.fromisoformat(msg.created_at)
    assert parsed.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60
