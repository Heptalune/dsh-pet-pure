# -*- coding: utf-8 -*-
"""AI 对话酒馆化：场景卡 + 扮演格式引导 + 括号动作段渲染。

钉住这些事：
- 三个配置键是**每宠键**（真值在 slot 档案，默认关）；
- 引导段逐字照 spec，只在 roleplay_enabled + 有效 system prompt 非空时追加；
- 场景卡空 = 只有引导段，非空才追加场景卡（cast 多行原样透传）；
- 所有 chat surface（modern / classic / 快速对话）统一渲染括号动作段，
  落盘原文不变；
- 动作段预处理不碰链接目标、裸 URL 与代码区间，动作段内的行内标记仍生效。
"""
from __future__ import annotations

import json
import re
import time

import pytest

from pet.chat.models import ChatMessage, ChatSettings
from pet.chat.prompt import PromptBuilder

# spec v2「扮演引导」定稿文案（逐字，不得改写语义）。v2 的重点是场景发散：
# 环境细节 + 在场其他人主动参与 + 每轮都要有推进，而不是两个人轮流对台词。
ROLEPLAY_GUIDE = (
    "你在和用户进行场景扮演。目标不是「你一句我一句地对台词」，而是让这个场景活着往前走。\n"
    "① 一轮回复里通常同时有三样东西（写成自然段落，不必分条）：你的台词（带动作或神态）、"
    "一个能被感知的环境细节（声音、光线、气味、气氛、时间在流逝），以及在场其他人的动静。"
    "环境不必每轮写满，但别让场景塌成白底上的两个人说话。\n"
    "② 在场的人有自己的目的和小动作：会插话、会看眼色、会起哄、会打断，也可能带来新信息"
    "或小麻烦。多人场景可以一人一句短促交替，不必每句话都由你来说。\n"
    "③ 每轮至少让场景往前走一小步——有人换了位置、有人说了句意外的话、有新东西被注意到、"
    "气氛变了。不要用「嗯」「是吗」这类空转把球直接踢回给用户。\n"
    "④ 场景转换、新人物入场、或隔了较长时间：用一段旁白交代新场景和在场的每个人"
    "（一个外貌特征加一个说话/习惯特征），照着场景卡写，场景卡没有就自己编但前后保持一致。"
    "名字一旦定下就不要改，没名字的路人给个临时称呼（「穿灰西装的男人」）。\n"
    "⑤ 可以主动发散：引入合理的环境元素或新角色（路过的旁人、突然的声响、电话、天气变化、"
    "外面的动静），只要与场景卡不冲突、并给用户留出接话的空间。\n"
    "⑥ 节奏：一次回复两三句到一小段为宜，别写成整章小说，也别把所有线索一次抖完。\n"
    "⑦ 绝不替用户做决定，不写用户的动作、台词和心理——用户的（动作）是剧情的一部分，"
    "接着演，不重复不点评。\n"
    "⑧ 场景卡是硬设定：与场景卡冲突时以场景卡为准。"
)


def _system(**kwargs) -> str:
    """走真实 PromptBuilder.build_messages，取 system 段。"""
    kwargs.setdefault("role_prompt", "你是测试角色。")
    messages = PromptBuilder().build_messages(
        ChatSettings(),
        "test-character",
        [],
        "你好",
        now=None,
        **kwargs,
    )
    return messages[0]["content"]


def _write_config(tmp_path, payload: dict) -> None:
    from pet import config as config_mod

    cfg_dir = tmp_path / config_mod.APP_DIR_NAME
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"version": 4, **payload}, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- prompt 注入
def test_guide_is_injected_only_when_enabled():
    off = _system(roleplay_enabled=False)
    assert ROLEPLAY_GUIDE not in off

    on = _system(roleplay_enabled=True)
    assert ROLEPLAY_GUIDE in on


def test_guide_uses_the_exact_spec_wording():
    system = _system(roleplay_enabled=True)
    assert ROLEPLAY_GUIDE in system
    # 五条编号一个不少、顺序不变（逐字文案已在上面的常量里固定）。
    positions = [system.index(marker) for marker in ("①", "②", "③", "④", "⑤")]
    assert positions == sorted(positions)


def test_guide_is_skipped_when_effective_system_prompt_is_empty():
    """roleplay_enabled 但有效 system prompt 为空：没有身份的扮演没有意义，不追加。"""
    messages = PromptBuilder().build_messages(
        ChatSettings(default_system_prompt=""),
        "test-character",
        [],
        "你好",
        roleplay_enabled=True,
        roleplay_scene="雨夜便利店",
        roleplay_cast="店员阿澈：总在擦杯子",
    )
    system = messages[0]["content"]
    assert ROLEPLAY_GUIDE not in system
    assert "雨夜便利店" not in system


def test_guide_contract_is_effective_system_prompt_not_persona_label(tmp_path):
    """R8 契约：门槛是有效 system prompt，不是「persona 段」这个字面说法。

    清空 System Prompt 且角色 manifest 无 persona → 不注入引导（场景卡也不注入）；
    有全局默认人设时即便角色 manifest 没有 persona，引导照常注入。
    """
    builder = PromptBuilder(characters_root=tmp_path)
    empty = builder.build_messages(
        ChatSettings(default_system_prompt=""),
        "no-persona",
        [],
        "你好",
        roleplay_enabled=True,
        roleplay_scene="雨夜便利店",
        roleplay_cast="阿澈：擦杯子",
    )[0]["content"]
    assert ROLEPLAY_GUIDE not in empty
    assert "雨夜便利店" not in empty and "阿澈" not in empty

    with_default = builder.build_messages(
        ChatSettings(default_system_prompt="你是默认人设。"),
        "no-persona",
        [],
        "你好",
        roleplay_enabled=True,
    )[0]["content"]
    assert ROLEPLAY_GUIDE in with_default


def test_guide_is_appended_after_persona():
    persona = "你是测试角色。"
    system = _system(role_prompt=persona, roleplay_enabled=True)
    assert system.index(persona) < system.index(ROLEPLAY_GUIDE)


def test_empty_scene_card_keeps_only_the_guide():
    system = _system(roleplay_enabled=True, roleplay_scene="", roleplay_cast="")
    assert ROLEPLAY_GUIDE in system
    assert "【场景卡】" not in system


def test_scene_card_is_injected_when_non_empty():
    scene = "雨夜的便利店，灯管忽明忽暗。"
    system = _system(
        roleplay_enabled=True,
        roleplay_scene=scene,
        roleplay_cast="店员阿澈：总在擦同一个杯子，说话前先叹口气。",
    )
    assert ROLEPLAY_GUIDE in system
    assert "【场景卡】" in system
    assert scene in system
    assert "店员阿澈：总在擦同一个杯子，说话前先叹口气。" in system


def test_only_scene_or_only_cast_also_forms_a_scene_card():
    only_scene = _system(roleplay_enabled=True, roleplay_scene="天台，风很大。", roleplay_cast="")
    assert "【场景卡】" in only_scene and "天台，风很大。" in only_scene

    only_cast = _system(
        roleplay_enabled=True,
        roleplay_scene="",
        roleplay_cast="阿澈：叹气\n小满：转笔",
    )
    assert "【场景卡】" in only_cast
    assert "阿澈：叹气\n小满：转笔" in only_cast


def test_multiline_cast_is_passed_through_verbatim():
    cast = "阿澈：总在擦同一个杯子，说话前先叹口气。\n小满：爱转笔，一紧张就把笔帽咬在嘴里。\n老周：看谁都先眯眼。"
    system = _system(roleplay_enabled=True, roleplay_cast=cast)
    assert cast in system


def test_scene_and_cast_are_only_sent_when_enabled():
    scene = "雨夜的便利店。"
    cast = "阿澈：擦杯子"
    for kwargs in (
        {"roleplay_scene": scene, "roleplay_cast": cast},
        {"roleplay_enabled": False, "roleplay_scene": scene, "roleplay_cast": cast},
    ):
        system = _system(**kwargs)
        assert scene not in system
        assert cast not in system


# ---------------------------------------------------------------- 配置键纪律
def test_roleplay_defaults_are_off_and_empty(tmp_path):
    from pet.config import Config

    cfg = Config(base=tmp_path)
    assert cfg.data["roleplay_enabled"] is False
    assert cfg.data["roleplay_scene"] == ""
    assert cfg.data["roleplay_cast"] == ""


def test_roleplay_keys_are_per_pet(tmp_path):
    """三个键必须在每宠键集里（读错层 = 前科回归）。"""
    from pet import pet_profiles
    from pet.config import Config

    assert {"roleplay_enabled", "roleplay_scene", "roleplay_cast"} <= set(
        pet_profiles.PER_PET_KEYS
    )
    defaults = Config(base=tmp_path).data
    assert {"roleplay_enabled", "roleplay_scene", "roleplay_cast"} <= set(defaults)


def test_roleplay_values_are_normalized_on_load(tmp_path):
    from pet.config import Config

    _write_config(
        tmp_path,
        {
            "roleplay_enabled": "true",
            "roleplay_scene": "  " + "场" * 600 + "  ",
            "roleplay_cast": "人" * 700,
        },
    )
    cfg = Config(base=tmp_path)
    assert cfg.data["roleplay_enabled"] is True
    assert cfg.data["roleplay_scene"] == "场" * 500
    assert cfg.data["roleplay_cast"] == "人" * 600

    _write_config(
        tmp_path / "dirty",
        {
            "roleplay_enabled": "false",
            "roleplay_scene": 42,
            "roleplay_cast": ["not", "text"],
        },
    )
    dirty = Config(base=tmp_path / "dirty")
    assert dirty.data["roleplay_enabled"] is False
    assert dirty.data["roleplay_scene"] == ""
    assert dirty.data["roleplay_cast"] == ""


def test_roleplay_keys_live_in_each_slot_profile(tmp_path):
    """不同角色/槽位各存各的场景卡，互不覆盖。"""
    from pet import pet_profiles
    from pet.config import Config

    cfg_dir = Config(base=tmp_path).dir
    first = pet_profiles.write_profile_settings(
        cfg_dir,
        1,
        {
            "roleplay_enabled": True,
            "roleplay_scene": "一号角色的雨夜便利店。",
            "roleplay_cast": "阿澈：擦杯子",
        },
    )
    second = pet_profiles.write_profile_settings(
        cfg_dir,
        2,
        {
            "roleplay_enabled": True,
            "roleplay_scene": "二号角色的天台。",
            "roleplay_cast": "小满：转笔",
        },
    )
    assert first.ok and second.ok

    one = Config(base=tmp_path, instance_id="slot-1")
    two = Config(base=tmp_path, instance_id="slot-2")
    assert one.get("roleplay_enabled") is True
    assert one.get("roleplay_scene") == "一号角色的雨夜便利店。"
    assert one.get("roleplay_cast") == "阿澈：擦杯子"
    assert two.get("roleplay_scene") == "二号角色的天台。"
    assert two.get("roleplay_cast") == "小满：转笔"


def _slot_routed_config(tmp_path):
    """构造一只宠的 SlotRoutedConfig 视图（每宠键从 slot 档案读）。"""
    from pet import pet_profiles
    from pet.config import Config
    from pet.settings_slot_config import SlotRoutedConfig

    main = Config(base=tmp_path)
    main.dir.mkdir(parents=True, exist_ok=True)
    written = pet_profiles.write_profile_settings(
        main.dir,
        1,
        {
            "roleplay_enabled": True,
            "roleplay_scene": "一号角色的雨夜便利店。",
            "roleplay_cast": "阿澈：擦杯子",
        },
    )
    assert written.ok
    instance = Config(base=tmp_path, instance_id="slot-1")
    return SlotRoutedConfig(instance, main, 1)


@pytest.mark.parametrize("surface", ["legacy", "quick"])
def test_legacy_and_quick_read_roleplay_from_slot_profile(surface, tmp_path, qapp):
    """R3：classic 聊天窗与快速对话都按每宠键传 roleplay_*，且走 config.get。"""
    from pet.chat.legacy_widgets import ChatWindow
    from pet.quick_chat import QuickChatBubble

    route = _slot_routed_config(tmp_path)
    sent: list[list[dict]] = []

    def fake_send(messages, config, request_id=None):
        sent.append(messages)
        return request_id or "test-request"

    if surface == "legacy":
        window = ChatWindow(route, "shenshen")
        window.service.send = fake_send
        try:
            window._begin_generation("你好")
            route.set("roleplay_enabled", False)
            window._begin_generation("再来")
        finally:
            window.close()
            qapp.processEvents()
    else:
        bubble = QuickChatBubble(route)
        bubble.service.send = fake_send
        try:
            bubble.input.setText("你好")
            bubble._send()
            route.set("roleplay_enabled", False)
            bubble.input.setText("再来")
            bubble._send()
        finally:
            bubble.close()
            qapp.processEvents()

    assert len(sent) == 2
    enabled_system = sent[0][0]["content"]
    disabled_system = sent[1][0]["content"]
    assert ROLEPLAY_GUIDE in enabled_system
    assert "一号角色的雨夜便利店。" in enabled_system
    assert "阿澈：擦杯子" in enabled_system
    assert ROLEPLAY_GUIDE not in disabled_system
    assert "一号角色的雨夜便利店。" not in disabled_system


# ---------------------------------------------------------------- 显示层
def _fragments(html: str):
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    doc = QTextDocument()
    doc.setHtml(html)
    found = []
    block = doc.begin()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid():
                fmt = fragment.charFormat()
                color = fmt.foreground().color()
                found.append((fragment.text(), fmt.fontItalic(), color.alpha()))
            iterator += 1
        block = block.next()
    return found


def _action_fragments(html: str):
    return [item for item in _fragments(html) if item[1]]


def _hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', html)


# ---------------------------------------------------------------- 动作段预处理边界
def test_prewrapped_autolink_is_not_double_wrapped():
    """F1：已经写在 <...> 里的 URL 不再被二次包裹成 ``<<...>>``。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("<https://x.org/a_(b)>")
    assert _hrefs(html) == ["https://x.org/a_(b)"]
    assert "&lt;https" not in html
    assert "&gt;" not in html


@pytest.mark.parametrize(
    "text,expected",
    [
        ("详见 https://example.com/x) 这一页", "https://example.com/x"),
        ("(https://x.org/a)", "https://x.org/a"),
        ("(https://x.org/paper_(2024))", "https://x.org/paper_(2024)"),
    ],
)
def test_bare_url_trims_unbalanced_closing_paren(text, expected):
    """F2：右括号按 URL 内的括号配平决定去留，不能焊进 href。"""
    from pet.chat.markdown_view import markdown_to_html

    assert _hrefs(markdown_to_html(text)) == [expected]


@pytest.mark.parametrize(
    "punct",
    [".", ",", ";", ":", "!", "?", '"', "'", "》", "」", "】"],
)
def test_bare_url_trims_trailing_sentence_punctuation(punct):
    """F2：尾随句末标点不属于链接。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html(f"见 https://example.com/x_(y){punct} 后面")
    assert _hrefs(html) == ["https://example.com/x_(y)"]


@pytest.mark.parametrize(
    "text",
    ["(a （b) c）", "(他停了一下（叹气) 然后说）"],
)
def test_crossing_parentheses_never_duplicate_text(text):
    """F3：交叉嵌套的动作区间必须两两不相交，渲染可见文字长度与原文一致。"""
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QApplication

    from pet.chat.markdown_view import markdown_to_html

    QApplication.instance() or QApplication([])
    doc = QTextDocument()
    doc.setHtml(markdown_to_html(text))
    assert doc.toPlainText() == text
    assert len(doc.toPlainText()) == len(text)


def test_action_pairing_latency_budget(qapp):
    """F4：括号配对与 token 还原来回都是线性量级，整段渲染不超过预算。"""
    from pet.chat.markdown_view import markdown_to_html

    cases = [
        ("paren-dense 10KB", "(a)" * 2500, 0.150),
        ("deep 2500", "(" * 2500 + "x" + ")" * 2500, 0.150),
        ("normal 6.5KB", "（他停下脚步，看着窗外的雨。）\n" * 300, 0.020),
    ]
    for name, text, budget in cases:
        started = time.perf_counter()
        markdown_to_html(text)
        elapsed = time.perf_counter() - started
        assert elapsed < budget, f"{name}: {elapsed * 1000:.1f} ms"


def test_link_target_parentheses_stay_in_href():
    """R1：markdown 链接目标里的半角括号不能被动作段 token 吞掉。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("看 [维基](https://x.org/a_(b)) 这篇")
    href = html.split('href="', 1)[1].split('"', 1)[0]
    assert href == "https://x.org/a_(b)"
    assert "<span" not in href
    assert "维基" in html


def test_bare_url_with_parentheses_is_autolinked_whole():
    """R1：裸 URL 里的括号整段留在链接里，不渲染成动作段。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("见 https://example.com/x_(y) 这篇")
    assert 'href="https://example.com/x_(y)"' in html
    assert "font-style:italic" not in html
    assert not _action_fragments(html)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("见 https://x.org/a 完", "https://x.org/a"),
        ("见 https://x.org/a，然后", "https://x.org/a"),
        ("见 https://x.org/a。", "https://x.org/a"),
        ("见 https://x.org/a?q=%E4%B8%AD", "https://x.org/a?q=%E4%B8%AD"),
        ("见 https://x.org/中文", "https://x.org/中文"),
        ("http://x.org:8080/a", "http://x.org:8080/a"),
        ("见 www.example.com，然后", "https://www.example.com"),
        ("见www.example.com，然后", "https://www.example.com"),
    ],
)
def test_bare_urls_are_always_autolinked(text, expected):
    """v2e R1：紧跟中文/全角标点、带 query、带端口、裸域名都必须点得动。"""
    from pet.chat.markdown_view import markdown_to_html

    assert _hrefs(markdown_to_html(text)) == [expected]


def test_link_target_and_bold_wrapped_url_keep_their_href():
    """v2e R1：链接目标与 ``**...**`` 里的 URL 不重复包裹，href 保持原样。"""
    from pet.chat.markdown_view import markdown_to_html

    wiki = markdown_to_html("[维基](https://x.org/a_(b))")
    assert _hrefs(wiki) == ["https://x.org/a_(b)"]
    assert wiki.count("<a ") == 1

    bold = markdown_to_html("**参考：https://x.org/a_(b)**")
    assert _hrefs(bold) == ["https://x.org/a_(b)"]
    assert "font-weight:700" in bold


@pytest.mark.parametrize(
    "text,emphasis",
    [
        ("见 *https://x.org/a_(b)*", "font-style:italic"),
        ("**参考：https://x.org/a_(b)**", "font-weight:700"),
        ("https://x.org/a_(b)**重点**", "font-weight:700"),
    ],
)
def test_url_stops_before_emphasis_markers(text, emphasis):
    """v2e R4：``*``/``**`` 不能被吃进 href；强调必须恢复，不按字面显示。"""
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QApplication

    from pet.chat.markdown_view import markdown_to_html

    QApplication.instance() or QApplication([])
    html = markdown_to_html(text)
    assert _hrefs(html) == ["https://x.org/a_(b)"]
    assert emphasis in html
    doc = QTextDocument()
    doc.setHtml(html)
    assert "*" not in doc.toPlainText()


def test_bare_url_inside_link_label_does_not_split_the_anchor():
    """v2e R5：标签里的 URL 不插尖括号；一个链接仍是一个 ``<a>``。"""
    from pet.chat.markdown_view import markdown_to_html

    text = "看 [说明 https://x.org/a_(b) 页](https://y.org/)"
    html = markdown_to_html(text)
    assert _hrefs(html) == ["https://y.org/"]
    assert html.count("<a ") == 1
    visible = "".join(item[0] for item in _fragments(html))
    assert "说明 https://x.org/a_(b) 页" in visible


def test_unclosed_parenthesis_is_not_an_action():
    """既有正例：未闭合括号不上色。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("（她抬手说了一半")
    assert not _action_fragments(html)
    assert "font-style:italic" not in html


@pytest.mark.parametrize(
    "text,inner",
    [
        ("（她笑（小声）说 https://x.org/a）", "（小声）"),
        ("（看 https://x.org/a 然后（笑））", "（笑）"),
    ],
)
def test_outer_action_with_url_falls_back_to_inner(text, inner):
    """v2e R7：外层动作段含 URL 被丢弃时，内层干净子段仍要渲染成斜体。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html(text)
    assert _hrefs(html) == ["https://x.org/a"]
    actions = _action_fragments(html)
    assert any(inner in fragment_text for fragment_text, _italic, _alpha in actions), html
    assert "".join(item[0] for item in _fragments(html)) == text


def test_action_ranges_latency_with_half_inline_code(qapp):
    """v2e R2：一半括号一半行内代码的 20KB 输入不再走 O(k·m) 相交判断。"""
    from pet.chat.markdown_view import markdown_to_html

    started = time.perf_counter()
    markdown_to_html("（动作）`x`" * 2000)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.150, f"half paren+code 20KB: {elapsed * 1000:.1f} ms"


def test_link_target_scan_latency_budget(qapp):
    """v2e R3：``](`` 密集输入用单遍扫描，两条预算都要过。"""
    from pet.chat.markdown_view import markdown_to_html

    cases = [
        ("many ]( 10KB", "](" * 5000, 0.150),
        ("many ]( 3000", "](" * 1000, 0.050),
    ]
    for name, text, budget in cases:
        started = time.perf_counter()
        markdown_to_html(text)
        elapsed = time.perf_counter() - started
        assert elapsed < budget, f"{name}: {elapsed * 1000:.1f} ms"


def test_parentheses_inside_inline_code_are_not_actions():
    """R1：行内代码里的括号原样留在代码里。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("`(x)`")
    assert not _action_fragments(html)
    assert "font-style:italic" not in html
    assert "(x)" in html


def test_action_cannot_cross_code_fence_boundary():
    """R2：动作段起于围栏外、止于围栏内时必须跳过，围栏与后续正文都完好。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("（看这段：\n\n```\ncode）\n```\n完了")
    assert "font-style:italic" not in html
    assert "<pre" in html and "code）" in html
    pre = html[html.index("<pre"):html.index("</pre>")]
    assert "完了" not in pre
    assert "完了" in html[html.index("</pre>"):]


@pytest.mark.parametrize(
    "text,outer",
    [
        ("(he said (quietly) then left)", "(he said (quietly) then left)"),
        ("（他说（低声）然后走了）", "（他说（低声）然后走了）"),
    ],
)
def test_one_level_nested_parentheses_render_outer_as_action(text, outer):
    """R4：一层嵌套时外层整体作为动作段。"""
    from pet.chat.markdown_view import markdown_to_html

    actions = _action_fragments(markdown_to_html(text))
    assert any(outer in fragment_text for fragment_text, _italic, _alpha in actions)


def test_two_levels_of_nesting_still_only_take_the_innermost():
    """R4：两层以上只取最内层，不无限递归。"""
    from pet.chat.markdown_view import markdown_to_html

    actions = _action_fragments(markdown_to_html("（最外（中间（最内）中间）最外）"))
    assert any("（最内）" in fragment_text for fragment_text, _italic, _alpha in actions)
    assert not any("（中间" in fragment_text for fragment_text, _italic, _alpha in actions)


def test_action_inline_code_and_bold_are_parsed():
    """R5：动作段内的 `x` 与 **x** 渲染成等宽/加粗，不残留字面标记。"""
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("（指着 `设置` 图标）\n（**重点**动作）")
    assert "font-style:italic" in html
    assert "<code" in html and "<strong>" in html
    assert "`" not in html and "**" not in html
    assert "设置" in html and "重点" in html


@pytest.mark.parametrize(
    "text,action_text",
    [
        ("（她抬眼看你）台词", "（她抬眼看你）"),
        ("(she looks up) line", "(she looks up)"),
    ],
)
def test_parenthesized_action_is_italic_and_weak(text, action_text):
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html(text)
    actions = _action_fragments(html)
    assert actions, html
    assert any(action_text in fragment_text for fragment_text, _italic, _alpha in actions)
    assert all(alpha < 255 for _text, _italic, alpha in actions)
    dialogue = [item for item in _fragments(html) if not item[1]]
    assert any("台词" in item[0] or "line" in item[0] for item in dialogue)


def test_multiline_action_is_rendered_as_one_italic_weak_segment():
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("第一句\n（动作一\n动作二）\n第二句")
    actions = _action_fragments(html)
    assert actions
    action_text = "".join(text for text, _italic, _alpha in actions)
    assert "动作一" in action_text and "动作二" in action_text
    assert all(alpha < 255 for _text, _italic, alpha in actions)
    # 台词部分不被动作样式连坐。
    dialogue = "".join(text for text, italic, _alpha in _fragments(html) if not italic)
    assert "第一句" in dialogue and "第二句" in dialogue


def test_code_fence_parentheses_are_not_treated_as_actions():
    from pet.chat.markdown_view import markdown_to_html

    html = markdown_to_html("```\nfunc(x)\n```")
    assert not _action_fragments(html)


def test_rendering_never_mutates_the_original_text(tmp_path):
    """纯展示层：渲染不写回原文，会话落盘也逐字保持。"""
    from pet.chat.markdown_view import markdown_to_html
    from pet.chat.session_store import SessionStore

    raw = "（动作）台词\n（第二段动作）"
    before = str(raw)
    html = markdown_to_html(raw)
    assert raw == before
    assert "font-style:italic" in html
    assert raw not in html  # HTML 里是带样式标签的版本，不是原文

    store = SessionStore(tmp_path)
    session = store.create("test-character", "p", "system")
    session.messages.append(ChatMessage("assistant", raw))
    assert store.save(session)
    assert store.flush()
    loaded = store.load(session.session_id, "test-character")
    assert loaded is not None
    assert loaded.messages[0].content == raw


def test_all_chat_surfaces_share_one_action_renderer(tmp_path, qapp):
    """R6：modern / classic / 快速对话三个入口统一渲染，输出逐字一致。"""
    from pet.chat.legacy_widgets import MessageBubble as LegacyBubble
    from pet.chat.widgets import MessageBubble as ModernBubble
    from pet.config import Config
    from pet.quick_chat import QuickChatBubble

    src = "（她抬眼看你）台词 `设置` **重点**"
    modern = ModernBubble("assistant", src)
    legacy = LegacyBubble("assistant", src)
    bubble = QuickChatBubble(Config(base=tmp_path))
    try:
        bubble._set_reply_text(src)
        bubble._render_reply()
        outputs = [modern.body.text(), legacy.body.text(), bubble.output.text()]
        assert len(set(outputs)) == 1, outputs
        assert "font-style:italic" in outputs[0]
        assert "`" not in outputs[0] and "**" not in outputs[0]
    finally:
        modern.deleteLater()
        legacy.deleteLater()
        bubble.close()
        qapp.processEvents()


# ---------------------------------------------------------------- 设置 UI
@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _settings_dialog(tmp_path, monkeypatch, slot: int):
    import pet.modern_settings_dialog as settings_mod
    from pet.config import Config

    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    return settings_mod.ModernSettingsDialog(
        Config(base=tmp_path, instance_id=f"slot-{slot}"),
        include_ai=True,
        standalone=True,
    )


def test_settings_group_is_mounted_with_its_guide(tmp_path, monkeypatch, qapp):
    from pet.modern_settings_dialog import SettingRow
    from pet.settings_page_guide import SECTION_GUIDE

    dialog = _settings_dialog(tmp_path, monkeypatch, 1)
    try:
        pages = [dialog.pages.widget(i) for i in range(dialog.pages.count())]
        for key in ("roleplay_enabled", "roleplay_scene"):
            row = dialog.findChild(SettingRow, f"settingRow_{key}")
            assert row is not None, key
            assert any(page.isAncestorOf(row) for page in pages), key
        assert dialog.ai_page.roleplay_scene_btn.text() == "场景卡…"
        assert "场景扮演" in SECTION_GUIDE
        assert "人设" in SECTION_GUIDE["场景扮演"]
        assert "场景卡" in SECTION_GUIDE["场景扮演"]
        assert "留空则只开启扮演格式，不约束设定" in dialog.findChild(
            SettingRow, "settingRow_roleplay_scene"
        ).hint_label.text()
    finally:
        dialog.close()
        qapp.processEvents()


def test_scene_dialog_has_example_placeholders(qapp):
    from pet.chat.ai_settings_page import RoleplaySceneDialog

    dialog = RoleplaySceneDialog("", "")
    try:
        assert dialog.scene_edit.placeholderText()
        assert dialog.cast_edit.placeholderText()
    finally:
        dialog.deleteLater()


def test_scene_dialog_counters_follow_input_and_turn_red(qapp):
    """R7：两个编辑框实时字数计数，超限变红并给出可见提示。"""
    from pet.chat.ai_settings_page import RoleplaySceneDialog
    from pet.config import ROLEPLAY_SCENE_MAX_CHARS

    dialog = RoleplaySceneDialog("", "")
    dialog.show()
    try:
        dialog.scene_edit.setPlainText("场" * 3)
        assert dialog.scene_counter.text() == f"3/{ROLEPLAY_SCENE_MAX_CHARS}"
        assert "#c9362b" not in dialog.scene_counter.styleSheet()
        over = "场" * (ROLEPLAY_SCENE_MAX_CHARS + 1)
        dialog.scene_edit.setPlainText(over)
        assert dialog.scene_counter.text() == (
            f"{ROLEPLAY_SCENE_MAX_CHARS + 1}/{ROLEPLAY_SCENE_MAX_CHARS}")
        assert "#c9362b" in dialog.scene_counter.styleSheet()
        assert dialog.truncation_hint.isVisible()
        assert "超出上限" in dialog.truncation_hint.text()
        assert not dialog.save_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_scene_dialog_counts_stripped_text_and_allows_save(qapp):
    """F7：计数与禁用都用 strip 后的长度，与 values() 的落盘口径一致。"""
    from pet.chat.ai_settings_page import RoleplaySceneDialog
    from pet.config import ROLEPLAY_SCENE_MAX_CHARS

    dialog = RoleplaySceneDialog("", "")
    dialog.show()
    try:
        dialog.scene_edit.setPlainText("场" * ROLEPLAY_SCENE_MAX_CHARS + "\n\n  ")
        assert dialog.scene_counter.text() == f"{ROLEPLAY_SCENE_MAX_CHARS}/{ROLEPLAY_SCENE_MAX_CHARS}"
        assert "#c9362b" not in dialog.scene_counter.styleSheet()
        assert dialog.save_button.isEnabled()
        assert not dialog.truncation_hint.isVisible()
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_scene_dialog_truncates_scene_with_ellipsis_and_cast_on_line_boundary(qapp):
    """R7：场景补「…」；人物按整行截断，绝不出现半行；截断提示可见。"""
    from pet.chat.ai_settings_page import RoleplaySceneDialog
    from pet.config import ROLEPLAY_CAST_MAX_CHARS, ROLEPLAY_SCENE_MAX_CHARS

    dialog = RoleplaySceneDialog("", "")
    dialog.show()
    try:
        dialog.scene_edit.setPlainText("场" * (ROLEPLAY_SCENE_MAX_CHARS + 60))
        one_line = "阿澈：" + "擦" * 40
        cast_lines = [one_line] * 20
        dialog.cast_edit.setPlainText("\n".join(cast_lines))
        scene, cast = dialog.values()
        assert len(scene) == ROLEPLAY_SCENE_MAX_CHARS
        assert scene.endswith("…")
        kept = cast.split("\n")
        assert kept == cast_lines[: len(kept)]  # 只保留整行，没有半行
        assert len(kept) < len(cast_lines)
        assert len(cast) <= ROLEPLAY_CAST_MAX_CHARS
        assert dialog.truncation_hint.isVisible()
        assert "截断" in dialog.truncation_hint.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_scene_dialog_blocks_save_while_over_limit(qapp):
    """R7：超限时保存按钮禁用（不许静默截断），删到范围内才能保存。"""
    from pet.chat.ai_settings_page import RoleplaySceneDialog
    from pet.config import ROLEPLAY_SCENE_MAX_CHARS

    dialog = RoleplaySceneDialog("", "")
    dialog.show()
    try:
        dialog.scene_edit.setPlainText("场" * 600)
        assert not dialog.save_button.isEnabled()
        dialog.save_button.click()          # 禁用状态下点不动
        assert dialog.isVisible()
        assert "超出上限" in dialog.truncation_hint.text()
        dialog.scene_edit.setPlainText("场" * (ROLEPLAY_SCENE_MAX_CHARS - 1))
        assert dialog.save_button.isEnabled()
        dialog.save_button.click()
        assert not dialog.isVisible()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_settings_save_routes_roleplay_keys_to_each_slot(tmp_path, monkeypatch, qapp):
    """走公开设置窗 seam：两只宠各存各的场景卡，主配置里不留每宠真值。"""
    from pet.config import Config

    for slot, scene in ((1, "一号角色的场景。"), (2, "二号角色的场景。")):
        dialog = _settings_dialog(tmp_path, monkeypatch, slot)
        try:
            dialog.ai_page.roleplay_enabled.setChecked(True)
            dialog.ai_page._roleplay_scene = scene
            dialog.ai_page._roleplay_cast = f"角色{slot}：一句描述"
            assert dialog._write_config() is True
        finally:
            dialog.close()
            qapp.processEvents()

    one = Config(base=tmp_path, instance_id="slot-1")
    two = Config(base=tmp_path, instance_id="slot-2")
    assert one.get("roleplay_scene") == "一号角色的场景。"
    assert two.get("roleplay_scene") == "二号角色的场景。"
    assert one.get("roleplay_cast") == "角色1：一句描述"
    assert two.get("roleplay_cast") == "角色2：一句描述"

    main = json.loads(Config(base=tmp_path).path.read_text(encoding="utf-8"))
    assert "roleplay_scene" not in main
    assert "roleplay_cast" not in main
    assert "roleplay_enabled" not in main
