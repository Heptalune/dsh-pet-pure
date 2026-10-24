# -*- coding: utf-8 -*-
"""截图翻译引擎（``pet/screen_capture/translate.py``）：OCR 原文 → 聊天模型文本互译。

这条链路是本片第一个**会主动发网络请求**的引擎（提取文字与复制图像纯本地、
问答由用户在浮窗里亲手点「发送」），所以这里把四件事钉死：

1. **方向**：中日韩占多数译英文，其余译中文（本产品的用户语言是中文）；
2. **组包**：纯**文本**调用——system 是"只输出译文"的指令、user 就是原文、
   ``stream=False``、model 用聊天 provider 自己的 model（不碰视觉端点/视觉模型）；
3. **端点与 Key**：走聊天配置自己的 base_url 与 Key（Authorization 头），
   与 ``pet/vision.py`` 同一条纪律；Key 经 ``config.resolve_api_key`` 解析
   （钥匙串那条路也在内），解析不出就不带 Authorization、绝不硬塞；
4. **失败人话**：HTTP 错误取 provider 的 error.message、网络错误重试一次、
   空回复/无 choices 各有明确说法——宁可报错，绝不返回空译文冒充成功。

打桩惯例与 ``tests/test_vision.py`` 一致：只打 ``urllib.request.urlopen`` 这个
系统边界，不打自家函数；网络类用例把 ``time.sleep`` 也打掉（重试之间的 1s 退避
不该让单测悬在它上面）。
"""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from pet.chat.models import ProviderConfig  # noqa: E402
from pet.screen_capture import translate as translate_mod  # noqa: E402
from pet.screen_capture.translate import TranslateError, translate_text  # noqa: E402


class _FakeResponse:
    """``urlopen`` 的最小可用返回（上下文管理器 + read）。"""

    def __init__(self, payload) -> None:
        self._payload = payload

    def __enter__(self):  # noqa: D105
        return self

    def __exit__(self, *args):  # noqa: D105
        return False

    def read(self, *args):
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")


class _FakeConfig:
    """``resolve_chat_context`` 要的两个口：聊天设置 + Key 解析（钥匙串那一层）。"""

    def __init__(self, provider, *, resolved_key=None) -> None:
        self._provider = provider
        self._resolved_key = resolved_key
        self.resolve_calls = 0

    def chat_settings(self):
        return SimpleNamespace(active_config=self._provider,
                               default_system_prompt="你是一只桌面宠物")

    def resolve_api_key(self, provider) -> str:
        self.resolve_calls += 1
        return provider.api_key if self._resolved_key is None else self._resolved_key


def _provider(**raw) -> ProviderConfig:
    data = {"model": "deepseek-v4-flash", "api_key": "sk-chat-secret"}
    data.update(raw)
    return ProviderConfig.from_dict("test", data)


def _reply(text: str) -> dict:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


def _capture(monkeypatch, *results):
    """打桩 urlopen；``results`` 依次是每次调用要返回的东西（异常则抛出）。

    返回记录列表，每项是 ``{"url", "headers", "body"}``。
    """
    calls: list[dict] = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append({
            "url": req.full_url,
            "headers": dict(req.header_items()),
            "body": json.loads(req.data.decode("utf-8")),
            "timeout": timeout,
            "context": context,
        })
        outcome = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


@pytest.fixture(autouse=True)
def _cheap_ssl_context(monkeypatch):
    """SSL 上下文构造是系统边界：本机 ``certifi`` 加载实测 **0.5s/次**，单测不验它。

    换成一个哨兵还顺手把"确实传给了 urlopen"变成可断言的
    （见 ``test_the_ssl_flag_and_the_context_reach_urlopen``）。
    """
    from pet.chat import providers as providers_mod

    sentinel = object()
    monkeypatch.setattr(providers_mod, "_make_ssl_context", lambda _verify: sentinel)
    return sentinel


def _system_message(body) -> str:
    return body["messages"][0]["content"]


def _user_message(body) -> str:
    return body["messages"][1]["content"]


# ------------------------------------------------------------------ 方向判定


@pytest.mark.parametrize("text,target", [
    ("你好，世界", "英文"),                       # 纯中文 → 英文
    ("今天的会议改到下午三点了", "英文"),
    ("Hello world", "中文"),                     # 纯英文 → 中文
    ("The deadline is Friday", "中文"),
    ("Hello 世界", "中文"),                      # 混合：中文字符不过半 → 中文
    ("你好世界ab", "英文"),                     # 中文过半 → 英文
])
def test_target_language_is_decided_by_the_cjk_ratio(text, target):
    """方向是中英互译：中日韩字符占一半以上译英文，否则译中文。"""
    assert translate_mod._target_language(text) == target


def test_the_target_language_reaches_the_prompt_and_the_source_stays_verbatim(monkeypatch):
    """方向决定 system 指令里的目标语言；user 一个字符都不许动（原文照译）。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("Hello world")))

    assert translate_text("你好，世界", _FakeConfig(_provider())) == "Hello world"

    body = calls[0]["body"]
    assert "英文" in _system_message(body)
    assert "只输出译文" in _system_message(body), "指令必须点名「只输出译文」"
    assert _user_message(body) == "你好，世界"


def test_english_source_asks_for_chinese(monkeypatch):
    calls = _capture(monkeypatch, _FakeResponse(_reply("你好，世界")))

    assert translate_text("Hello world", _FakeConfig(_provider())) == "你好，世界"

    assert "中文" in _system_message(calls[0]["body"])


@pytest.mark.parametrize("target", ["中文", "English", "日本語"])
def test_an_explicit_target_reaches_the_system_instruction(monkeypatch, target):
    """语言行给的目标原样进指令：用户说译什么就译什么。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("译文")))

    assert translate_text("你好，世界", _FakeConfig(_provider()), target=target) == "译文"

    assert f"翻译成{target}" in _system_message(calls[0]["body"])


def test_an_explicit_target_beats_auto_detection(monkeypatch):
    """中文源在自动判定下会译英文；用户选了中文就照用户说的（不再自动翻向）。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("译文")))

    translate_text("你好，世界", _FakeConfig(_provider()), target="中文")

    system = _system_message(calls[0]["body"])
    assert "翻译成中文" in system
    assert "翻译成英文" not in system


@pytest.mark.parametrize("target", [None, "", "auto"])
def test_auto_targets_keep_the_automatic_decision(monkeypatch, target):
    """``None`` / 空串 / ``"auto"`` 三档都是"跟着内容判"（面板缺省就是 auto）。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("译文")))

    translate_text("你好，世界", _FakeConfig(_provider()), target=target)

    assert "翻译成英文" in _system_message(calls[0]["body"])


def test_empty_text_is_refused_before_any_request(monkeypatch):
    """空文本没有可翻译的东西：当场报错，一个字节都不发。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("不该发生")))
    config = _FakeConfig(_provider())

    for blank in ("", "   ", "\n\t "):
        with pytest.raises(TranslateError) as exc_info:
            translate_text(blank, config)
        assert "没有可翻译的文字" in str(exc_info.value)

    assert calls == []
    assert config.resolve_calls == 0, "连 Key 都不该去解析"


# ------------------------------------------------------------------ 正路径组包


def test_translate_text_posts_a_plain_text_request_to_the_chat_endpoint(monkeypatch):
    """端点/模型/组包口径：聊天端点 + 聊天模型 + 纯文本 + 非流式。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("Hello world")))
    provider = _provider(model="deepseek-v4-flash",
                         base_url="https://api.deepseek.com",
                         chat_path="/v1/chat/completions")

    assert translate_text("你好", _FakeConfig(provider)) == "Hello world"

    call = calls[0]
    body = call["body"]
    assert call["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert body["model"] == provider.model
    assert body["stream"] is False
    assert body["temperature"] == 0.2
    assert call["timeout"] >= 60.0            # 短文本翻译的超时下限
    assert body["max_tokens"] == 2048          # max(1024, min(provider.max_tokens, 4096))
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    # 纯文本：user 是字符串，不是 vision 那样的 [{type: text}/{type: image_url}] 列表
    assert isinstance(_user_message(body), str)


def test_the_visual_endpoint_and_visual_key_are_never_used(monkeypatch):
    """独立视觉端点/视觉 Key 是给"看图"的：翻译是文本调用，绝不改道去那里。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("译文")))
    provider = _provider(base_url="https://api.deepseek.com",
                         vision_same_as_chat=False,
                         vision_base_url="https://open.bigmodel.cn/api/paas/v4",
                         vision_api_key="sk-vision-secret")

    translate_text("你好", _FakeConfig(provider))

    assert calls[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert calls[0]["headers"].get("Authorization") == "Bearer sk-chat-secret"


def test_the_max_tokens_are_clamped_into_a_sane_window(monkeypatch):
    calls = _capture(monkeypatch, _FakeResponse(_reply("x")), _FakeResponse(_reply("y")))

    translate_text("你好", _FakeConfig(_provider(max_tokens=8)))       # 太小 → 抬到 1024
    translate_text("你好", _FakeConfig(_provider(max_tokens=99999)))   # 太大 → 压到 4096

    assert calls[0]["body"]["max_tokens"] == 1024
    assert calls[1]["body"]["max_tokens"] == 4096


def test_the_ssl_flag_and_the_context_reach_urlopen(monkeypatch, _cheap_ssl_context):
    """``verify_ssl`` 必须真的传到 SSL 上下文构造上（自签名网关靠它跑通）。"""
    from pet.chat import providers as providers_mod

    seen: list[bool] = []
    monkeypatch.setattr(providers_mod, "_make_ssl_context",
                        lambda verify: seen.append(verify) or _cheap_ssl_context)
    calls = _capture(monkeypatch, _FakeResponse(_reply("译文")), _FakeResponse(_reply("译文")))

    translate_text("你好", _FakeConfig(_provider(verify_ssl=False)))
    translate_text("你好", _FakeConfig(_provider()))          # 缺省 True

    assert seen == [False, True]
    assert calls[0]["context"] is _cheap_ssl_context


# ------------------------------------------------------------------ Key 纪律


def test_the_authorization_header_carries_the_resolved_chat_key(monkeypatch):
    """Key 随聊天配置：解析出来的那个（钥匙串路径）进 Authorization 头。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("译文")))
    config = _FakeConfig(_provider(api_key=""), resolved_key="sk-from-keychain")

    translate_text("你好", config)

    assert config.resolve_calls == 1
    assert calls[0]["headers"].get("Authorization") == "Bearer sk-from-keychain"
    assert calls[0]["headers"].get("Content-type") == "application/json"


def test_without_a_key_the_request_simply_omits_authorization(monkeypatch):
    """没有 Key 就不带 Authorization（不是塞一个空 Bearer）——本地网关就是这么用的。"""
    calls = _capture(monkeypatch, _FakeResponse(_reply("译文")))

    translate_text("你好", _FakeConfig(_provider(api_key=""), resolved_key=""))

    assert "Authorization" not in calls[0]["headers"]


# ------------------------------------------------------------------ 失败人话


def test_http_error_surfaces_the_provider_message(monkeypatch):
    """HTTP 错误取 provider 自己的 error.message——不重试、不吞成"网络失败"。"""
    body = json.dumps({"error": {"message": "余额不足，请充值"}}).encode("utf-8")
    error = urllib.error.HTTPError("https://api.deepseek.com/v1/chat/completions", 402,
                                   "Payment Required", {}, io.BytesIO(body))
    calls = _capture(monkeypatch, error)

    with pytest.raises(TranslateError) as exc_info:
        translate_text("你好", _FakeConfig(_provider()))

    assert "余额不足，请充值" in str(exc_info.value)
    assert len(calls) == 1, "HTTP 错误是确定性拒绝，不该重试"


def test_http_error_without_a_json_body_still_says_something_readable(monkeypatch):
    error = urllib.error.HTTPError("https://x/v1/chat/completions", 500, "Server Error",
                                   {}, io.BytesIO(b"<html>502 Bad Gateway</html>"))
    _capture(monkeypatch, error)

    with pytest.raises(TranslateError) as exc_info:
        translate_text("你好", _FakeConfig(_provider()))

    assert "502 Bad Gateway" in str(exc_info.value)


def test_a_network_error_is_retried_once_and_the_backoff_is_short(monkeypatch):
    """网络错误（DNS/连接被掐）重试一次，再失败就把原因报到用户面前。"""
    slept: list[float] = []
    monkeypatch.setattr(translate_mod.time, "sleep", slept.append)
    calls = _capture(monkeypatch,
                     urllib.error.URLError("connection reset"),
                     urllib.error.URLError("connection reset"))

    with pytest.raises(TranslateError) as exc_info:
        translate_text("你好", _FakeConfig(_provider()))

    assert len(calls) == 2, "网络错误只重试一次（翻译是短文本快调用，不做长退避）"
    assert slept == [1.0]
    assert "网络连接失败" in str(exc_info.value)
    assert "connection reset" in str(exc_info.value)


def test_a_transient_network_error_recovers_on_the_retry(monkeypatch):
    """重试是真重试：第一次抖一下、第二次成功就必须把译文交出来。"""
    monkeypatch.setattr(translate_mod.time, "sleep", lambda _s: None)
    calls = _capture(monkeypatch,
                     urllib.error.URLError("temporary failure"),
                     _FakeResponse(_reply("Hello world")))

    assert translate_text("你好，世界", _FakeConfig(_provider())) == "Hello world"

    assert len(calls) == 2


def test_a_socket_level_error_is_treated_as_a_network_error(monkeypatch):
    """OSError 那一支（超时/连接中断）走同一套重试与话术。"""
    monkeypatch.setattr(translate_mod.time, "sleep", lambda _s: None)
    calls = _capture(monkeypatch, OSError("timed out"), OSError("timed out"))

    with pytest.raises(TranslateError) as exc_info:
        translate_text("你好", _FakeConfig(_provider()))

    assert len(calls) == 2
    assert "网络连接失败" in str(exc_info.value)


def test_a_response_without_choices_is_reported(monkeypatch):
    """有 200 但没有 choices（网关吞了请求/返回了别的形状）＝没说话，报错。"""
    _capture(monkeypatch, _FakeResponse({"id": "x"}))

    with pytest.raises(TranslateError) as exc_info:
        translate_text("你好", _FakeConfig(_provider()))

    assert "无 choices" in str(exc_info.value)


def test_an_empty_reply_is_reported(monkeypatch):
    """空回复不许当成"译文就是空的"交给用户——那是无声失败。"""
    _capture(monkeypatch, _FakeResponse({"choices": [{"message": {"content": "   "}}]}))

    with pytest.raises(TranslateError) as exc_info:
        translate_text("你好", _FakeConfig(_provider()))

    assert "空回复" in str(exc_info.value)


def test_a_missing_message_content_is_reported(monkeypatch):
    _capture(monkeypatch, _FakeResponse({"choices": [{"finish_reason": "stop"}]}))

    with pytest.raises(TranslateError):
        translate_text("你好", _FakeConfig(_provider()))


def test_content_parts_are_joined(monkeypatch):
    """部分实现把 content 拆成 parts：只取 text part 拼起来，别把结构交给用户。"""
    payload = {"choices": [{"message": {"content": [
        {"type": "text", "text": "Hello "},
        {"type": "reasoning", "text": "不该拼进来"},
        {"type": "text", "text": "world"},
    ]}}]}
    _capture(monkeypatch, _FakeResponse(payload))

    assert translate_text("你好", _FakeConfig(_provider())) == "Hello world"


def test_the_prompt_treats_ocr_line_breaks_as_layout_not_sentence_ends(monkeypatch):
    """OCR 文本按栏宽硬折行：指令必须让模型先接回完整句子，而不是照行对译。

    旧指令写着「保留原有的换行结构」——模型老实照做，于是 "Working / on / two of /
    these" 被逐行对译成「正在 / 处理 / 这些中的 / 这些」，语序与语义一起丢。
    """
    calls = _capture(monkeypatch, _FakeResponse(_reply("正在做其中两件")))

    translate_text("Working\non\ntwo of\nthese", _FakeConfig(_provider()))

    system = _system_message(calls[0]["body"])
    assert "保留原有的换行结构" not in system, "逐行对齐原文正是逐词译文的成因"
    assert "不是句子边界" in system
    assert "只输出译文" in system, "「只输出译文」这条不许被新指令挤掉"
    # 原文仍然逐字照发（不改写用户看到的 OCR 结果）
    assert _user_message(calls[0]["body"]) == "Working\non\ntwo of\nthese"
