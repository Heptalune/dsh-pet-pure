"""深度思考（reasoning_content 展示 / thinking 参数）与联网搜索（检索阶段 + 资料注入）。

网络边界用本地 HTTP 服务模拟 OpenAI 兼容端点；搜索引擎用 monkeypatch 替身，
不访问真实外网。
"""
from __future__ import annotations

import http.server
import json
import threading
import time

import pytest

from pet.chat.models import ChatSettings, ProviderConfig, WebSearchSettings
from pet.chat.providers import (
    OpenAICompatibleProvider,
    ProviderError,
    SSEParser,
    StreamEvent,
    inject_search_context,
    thinking_payload,
)


# ---------------------------------------------------------------- SSE 解析

def test_sse_parser_surfaces_reasoning_and_finish_reason():
    parser = SSEParser()
    events = parser.feed(
        b'data: {"choices":[{"delta":{"reasoning_content":"\xe5\x85\x88\xe6\x83\xb3"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    )
    # 旧接口只返回正文
    assert events == ["ok"]
    parser = SSEParser()
    events = parser.feed_events(
        b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n\n'
        b'data: {"choices":[{"delta":{"reasoning":"more"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"ans"},"finish_reason":"stop"}]}\n\n'
    )
    assert [e["reasoning"] for e in events] == ["think", "more", ""]
    assert [e["content"] for e in events] == ["", "", "ans"]
    assert parser.finish_reason == "stop"


# ---------------------------------------------------------------- thinking 参数

def test_thinking_payload_modes():
    ds = ProviderConfig("p", base_url="https://api.deepseek.com")
    other = ProviderConfig("p", base_url="https://api.example.com")
    assert thinking_payload(ds) == {}  # auto：沿用服务端默认，不发参数
    assert thinking_payload(other) == {}
    assert thinking_payload(ds, force_disable=True) == {"thinking": {"type": "disabled"}}
    assert thinking_payload(other, force_disable=True) == {}  # 非 DeepSeek 的 auto 不冒险发未知参数
    ds.thinking = "enabled"; ds.reasoning_effort = "max"
    assert thinking_payload(ds) == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    other.thinking = "disabled"
    assert thinking_payload(other) == {"thinking": {"type": "disabled"}}


def test_settings_round_trip_keeps_thinking_and_web_search():
    settings = ChatSettings.defaults()
    p = settings.active_config
    p.thinking = "enabled"; p.reasoning_effort = "low"
    settings.web_search = WebSearchSettings(enabled=True, engine="bocha", api_key="secret", max_results=7, fetch_pages=False)
    raw = settings.to_dict(include_secrets=False)
    assert "web_search" not in raw["providers"][settings.active_provider]
    assert "api_key" not in raw["web_search"]
    again = ChatSettings.from_dict(json.loads(json.dumps(raw)))
    assert again.active_config.thinking == "enabled"
    assert again.active_config.reasoning_effort == "low"
    assert again.web_search.enabled is True and again.web_search.engine == "bocha"
    assert again.web_search.max_results == 7 and again.web_search.fetch_pages is False
    # active_config 自带全局联网设置
    assert again.active_config.web_search is again.web_search


def test_settings_from_dict_sanitizes_unknown_values():
    raw = {"providers": {"a": {"thinking": "bogus", "reasoning_effort": "ultra"}},
           "web_search": {"engine": "nope", "max_results": 99}}
    s = ChatSettings.from_dict(raw)
    assert s.active_config.thinking == "auto"
    assert s.active_config.reasoning_effort == "high"
    assert s.web_search.engine == "baidu"
    assert s.web_search.max_results == 10
    assert s.web_search.enabled is False


def test_config_never_writes_web_search_key_to_disk(tmp_path):
    from pet.config import Config

    cfg = Config(tmp_path)
    settings = cfg.chat_settings()
    settings.web_search.api_key = "plain-secret"
    cfg.set_chat_settings(settings)
    assert cfg.save()
    written = [path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json")]
    assert any('"web_search"' in text for text in written)
    assert not any("plain-secret" in text for text in written)


# ---------------------------------------------------------------- 本地假端点

class _FakeEndpoint:
    """按顺序回放响应；记录每次请求体。responses 元素：('json', dict) | ('sse', [dict...]) | ('http', code, dict)。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                endpoint.requests.append(body)
                kind, *rest = endpoint.responses.pop(0)
                if kind == "http":
                    code, payload = rest
                    out = json.dumps(payload).encode()
                    self.send_response(code)
                elif kind == "json":
                    out = json.dumps({"choices": [{"message": rest[0]}]}).encode()
                    self.send_response(200)
                else:
                    out = b"".join(b"data: " + json.dumps({"choices": [c]}).encode() + b"\n\n" for c in rest[0]) + b"data: [DONE]\n\n"
                    self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def _run(provider, messages, cfg):
    return list(provider.stream(messages, cfg, threading.Event()))


def test_stream_yields_reasoning_events_and_sends_thinking_param():
    with _FakeEndpoint([("sse", [
        {"delta": {"reasoning_content": "让我想想"}},
        {"delta": {"reasoning_content": "……"}},
        {"delta": {"content": "答案"}, "finish_reason": "stop"},
    ])]) as ep:
        cfg = ProviderConfig("t", base_url=ep.url, model="m", thinking="enabled", reasoning_effort="high")
        out = _run(OpenAICompatibleProvider(), [{"role": "user", "content": "hi"}], cfg)
    assert [o for o in out if isinstance(o, StreamEvent)] == [StreamEvent("reasoning", "让我想想"), StreamEvent("reasoning", "……")]
    assert "".join(o for o in out if isinstance(o, str)) == "答案"
    assert ep.requests[0]["thinking"] == {"type": "enabled"}
    assert ep.requests[0]["reasoning_effort"] == "high"
    assert "tools" not in ep.requests[0]


def test_reasoning_that_exhausts_max_tokens_reports_actionable_error():
    with _FakeEndpoint([("sse", [
        {"delta": {"reasoning_content": "想了很久"}},
        {"delta": {}, "finish_reason": "length"},
    ])]) as ep:
        cfg = ProviderConfig("t", base_url=ep.url, model="m", max_tokens=64)
        with pytest.raises(ProviderError) as excinfo:
            _run(OpenAICompatibleProvider(), [{"role": "user", "content": "hi"}], cfg)
    assert "最大输出 Token" in str(excinfo.value)


def _web_cfg(url, **kw):
    cfg = ProviderConfig("t", base_url=url, model="m", **kw)
    cfg.web_search = WebSearchSettings(enabled=True, engine="duckduckgo")
    return cfg


def test_web_search_runs_tool_then_injects_results_into_answer(monkeypatch):
    from pet.chat import web_search as ws

    seen = []

    def fake_search(query, settings, *, timeout, verify_ssl):
        seen.append(query)
        return [ws.SearchResult("国庆放假通知", "https://example.gov/notice", "10月1日至8日放假")]

    monkeypatch.setattr(ws, "search", fake_search)
    tool_call = {"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"query": "国庆放假安排"})}}
    with _FakeEndpoint([
        ("json", {"role": "assistant", "content": "", "tool_calls": [tool_call]}),
        ("json", {"role": "assistant", "content": "资料够了"}),
        ("sse", [{"delta": {"content": "放 8 天"}, "finish_reason": "stop"}]),
    ]) as ep:
        cfg = _web_cfg(ep.url, thinking="enabled")
        messages = [{"role": "system", "content": "你是桌宠"}, {"role": "user", "content": "国庆放几天？"}]
        out = _run(OpenAICompatibleProvider(), messages, cfg)

    assert seen == ["国庆放假安排"]
    statuses = [o.text for o in out if isinstance(o, StreamEvent) and o.kind == "status"]
    assert any("正在搜索：国庆放假安排" in s for s in statuses)
    assert "".join(o for o in out if isinstance(o, str)) == "放 8 天"
    planner, planner2, final = ep.requests
    # 检索阶段：工具调用 + 显式关闭思考（避免 DeepSeek 思考模式的 reasoning_content 回传要求）
    assert planner["tools"][0]["function"]["name"] == "web_search"
    assert planner["thinking"] == {"type": "disabled"}
    assert planner["stream"] is False
    assert planner2["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": ws.format_results("国庆放假安排", fake_search("x", None, timeout=1, verify_ssl=True))}
    # 正式回答：不带 tools、按设置开启思考，资料并入 system
    assert "tools" not in final
    assert final["thinking"] == {"type": "enabled"}
    assert final["messages"][0]["role"] == "system"
    assert final["messages"][0]["content"].startswith("你是桌宠")
    assert "【联网搜索结果】" in final["messages"][0]["content"]
    assert "https://example.gov/notice" in final["messages"][0]["content"]
    assert final["messages"][-1] == {"role": "user", "content": "国庆放几天？"}
    # 调用方传入的列表不被改写
    assert messages[0]["content"] == "你是桌宠"


def test_web_search_skipped_when_model_says_no(monkeypatch):
    from pet.chat import web_search as ws

    monkeypatch.setattr(ws, "search", lambda *a, **k: pytest.fail("不应搜索"))
    with _FakeEndpoint([
        ("json", {"role": "assistant", "content": "无需搜索"}),
        ("sse", [{"delta": {"content": "你好呀"}, "finish_reason": "stop"}]),
    ]) as ep:
        out = _run(OpenAICompatibleProvider(), [{"role": "user", "content": "你好"}], _web_cfg(ep.url))
    assert "".join(o for o in out if isinstance(o, str)) == "你好呀"
    assert "【联网搜索结果】" not in json.dumps(ep.requests[-1]["messages"], ensure_ascii=False)
    assert any(isinstance(o, StreamEvent) and "无需联网" in o.text for o in out)


def test_web_search_degrades_gracefully_when_tools_unsupported():
    with _FakeEndpoint([
        ("http", 400, {"error": {"message": "tools not supported"}}),
        ("sse", [{"delta": {"content": "直接回答"}, "finish_reason": "stop"}]),
    ]) as ep:
        out = _run(OpenAICompatibleProvider(), [{"role": "user", "content": "今天新闻"}], _web_cfg(ep.url))
    assert "".join(o for o in out if isinstance(o, str)) == "直接回答"
    assert any(isinstance(o, StreamEvent) and "联网检索不可用" in o.text for o in out)


def test_search_failure_is_reported_to_model_not_fatal(monkeypatch):
    from pet.chat import web_search as ws

    def boom(*a, **k):
        raise ws.WebSearchError("网络连接失败：timed out")

    monkeypatch.setattr(ws, "search", boom)
    call = {"id": "c", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"x"}'}}
    with _FakeEndpoint([
        ("json", {"role": "assistant", "content": "", "tool_calls": [call]}),
        ("json", {"role": "assistant", "content": "算了"}),
        ("sse", [{"delta": {"content": "抱歉没查到"}, "finish_reason": "stop"}]),
    ]) as ep:
        out = _run(OpenAICompatibleProvider(), [{"role": "user", "content": "x"}], _web_cfg(ep.url))
    assert "".join(o for o in out if isinstance(o, str)) == "抱歉没查到"
    assert "失败" in ep.requests[1]["messages"][-1]["content"]
    assert "【联网搜索结果】" not in json.dumps(ep.requests[-1]["messages"], ensure_ascii=False)


def test_inject_search_context_without_system_message():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "看图"}]}]
    out = inject_search_context(msgs, "资料A")
    assert out[0]["role"] == "system" and "资料A" in out[0]["content"]
    assert out[1] == msgs[0]


# ---------------------------------------------------------------- 搜索结果解析

def test_parse_duckduckgo_html_skips_ads_and_decodes_links():
    from pet.chat.web_search import parse_duckduckgo_html

    page = (
        '<div class="result results_links result--ad "><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fad.example">Ad</a></div>'
        '<div class="result results_links results_links_deep web-result ">'
        '<h2><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">Example <b>A</b></a></h2>'
        '<a class="result__snippet" href="x">Snippet &amp; more</a></div>'
    )
    results = parse_duckduckgo_html(page, 5)
    assert len(results) == 1
    assert results[0].title == "Example A"
    assert results[0].url == "https://example.com/a"
    assert results[0].snippet == "Snippet & more"


def test_parse_baidu_html_uses_real_url_and_skips_internal_cards():
    from pet.chat.web_search import parse_baidu_html

    page = (
        '<div class="result-op c-container" mu="http://nourl.ubs.baidu.com/279"><h3>AI 卡片</h3></div>'
        '<div class="result c-container xpath-log" mu="https://news.example.cn/a.html" tpl="www_index">'
        '<h3 class="t"><a href="http://www.baidu.com/link?url=abc">新闻 <em>标题</em></a></h3>'
        '<div class="c-abstract">这是摘要内容</div></div>'
    )
    results = parse_baidu_html(page, 5)
    assert [(r.title, r.url) for r in results] == [("新闻 标题", "https://news.example.cn/a.html")]
    assert "这是摘要内容" in results[0].snippet


def test_html_to_text_drops_scripts_and_keeps_title():
    from pet.chat.web_search import html_to_text

    text = html_to_text("<html><head><title>标题</title><script>var x=1</script></head><body><p>第一段</p><nav>菜单</nav><div>第二段</div></body></html>")
    assert text.startswith("标题：标题")
    assert "第一段" in text and "第二段" in text
    assert "var x" not in text and "菜单" not in text


def test_free_engine_captcha_is_reported_and_falls_back(monkeypatch):
    from pet.chat import web_search as ws

    ddg_ok = (
        '<div class="result results_links results_links_deep web-result ">'
        '<a class="result__a" href="https://example.com/x">X</a><a class="result__snippet" href="#">s</a></div>'
    )
    pages = {
        "baidu": '<html><head><title>百度安全验证</title></head></html>',
        "duckduckgo": ddg_ok,
    }
    calls = []

    def fake_get(url, **kwargs):
        engine = "baidu" if "baidu.com" in url else "duckduckgo"
        calls.append(engine)
        return pages[engine], "text/html"

    monkeypatch.setattr(ws, "_http_get", fake_get)
    results = ws.search("x", WebSearchSettings(enabled=True, engine="baidu"))
    assert calls == ["baidu", "duckduckgo"]
    assert [r.url for r in results] == ["https://example.com/x"]

    pages["duckduckgo"] = '<div id="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div>'
    with pytest.raises(ws.WebSearchError) as excinfo:
        ws.search("x", WebSearchSettings(enabled=True, engine="duckduckgo"))
    message = str(excinfo.value)
    assert "DuckDuckGo 要求人机验证" in message and "百度要求人机验证" in message
    assert "博查" in message


def test_keyed_engine_without_key_raises_clear_error():
    from pet.chat import web_search as ws

    settings = WebSearchSettings(enabled=True, engine="tavily", api_key_ref="")
    with pytest.raises(ws.WebSearchError) as excinfo:
        ws.search("x", settings)
    assert "API Key" in str(excinfo.value)


# ---------------------------------------------------------------- Qt：服务信号与气泡

def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_chat_service_routes_activity_separately_from_content():
    from pet.chat.service import ChatService

    class Provider:
        def stream(self, messages, config, cancel):
            yield StreamEvent("status", "🔍 正在搜索：x")
            yield StreamEvent("reasoning", "思考")
            yield "正文"

    app = _qapp()
    service = ChatService(provider=Provider())
    activity, deltas, finished = [], [], []
    service.activity.connect(lambda rid, kind, text: activity.append((kind, text)))
    service.delta.connect(lambda rid, text: deltas.append(text))
    service.finished.connect(lambda rid, text: finished.append(text))
    service.send([], ProviderConfig("t"))
    deadline = time.time() + 5.0
    while not finished and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert finished == ["正文"]
    assert deltas == ["正文"]
    assert activity == [("status", "🔍 正在搜索：x"), ("reasoning", "思考")]
    service.shutdown()


def test_message_bubble_thinking_panel_collapses_when_answer_starts():
    from pet.chat.widgets import MessageBubble

    app = _qapp()
    bubble = MessageBubble("assistant", "")
    bubble.show()
    assert bubble.thinking_toggle.isHidden()
    bubble.add_activity("🔍 正在搜索：天气")
    bubble.append_reasoning("先看看搜索结果")
    bubble._refresh_thinking()
    app.processEvents()
    assert not bubble.thinking_toggle.isHidden()
    assert bubble.thinking_body.isVisible()
    assert "先看看搜索结果" in bubble.thinking_body.text()
    assert "思考中" in bubble.thinking_toggle.text()
    bubble.finish_thinking()
    app.processEvents()
    assert not bubble.thinking_body.isVisible()
    assert "已联网搜索并深度思考" in bubble.thinking_toggle.text()
    bubble.thinking_toggle.click()
    app.processEvents()
    assert bubble.thinking_body.isVisible()
    # 复制按钮只复制正文
    bubble.set_content("回答")
    assert bubble.content == "回答"
    bubble.close()


def test_ai_settings_page_persists_thinking_and_web_search(tmp_path, monkeypatch):
    from pet.chat import models
    from pet.chat.ai_settings_page import _AiSettingsPage
    from pet.config import Config

    stored = {}
    monkeypatch.setattr(models.SecretStore, "set", lambda self, ref, value: stored.__setitem__(ref, value) or True)
    monkeypatch.setattr(models.SecretStore, "get", lambda self, ref: stored.get(ref, ""))
    app = _qapp()
    cfg = Config(tmp_path)
    page = _AiSettingsPage(cfg)
    assert page._reasoning_effort_row.isHidden()
    page.thinking_mode.setCurrentData("enabled")
    page.reasoning_effort.setCurrentData("max")
    page.web_search_enabled.setChecked(True)
    page.web_search_engine.setCurrentData("bocha")
    page.web_search_key.setText("bocha-key")
    page.web_search_results.setValue(3)
    assert not page._reasoning_effort_row.isHidden()
    assert not page._web_key_row.isHidden()
    page.save()
    assert cfg.save()

    reloaded = Config(tmp_path).chat_settings()
    assert reloaded.active_config.thinking == "enabled"
    assert reloaded.active_config.reasoning_effort == "max"
    assert reloaded.web_search.enabled is True
    assert reloaded.web_search.engine == "bocha"
    assert reloaded.web_search.max_results == 3
    assert reloaded.web_search.api_key == ""  # Key 进钥匙串，不落盘
    assert reloaded.web_search.resolve_api_key() == "bocha-key"
    page.close()
    app.processEvents()


# ---------------------------------------------------------------- 思考过程随会话落盘

def test_chat_message_persists_reasoning_and_stays_compatible():
    from pet.chat.models import ChatMessage

    msg = ChatMessage("assistant", "答", reasoning="想了想", activity=["🔍 正在搜索：x"])
    raw = msg.to_dict()
    assert raw["reasoning"] == "想了想" and raw["activity"] == ["🔍 正在搜索：x"]
    again = ChatMessage.from_dict(json.loads(json.dumps(raw)))
    assert (again.reasoning, again.activity) == ("想了想", ["🔍 正在搜索：x"])
    # 无思考的消息不写这两个键（旧格式不变）；旧文件缺键照常读
    plain = ChatMessage("user", "hi").to_dict()
    assert "reasoning" not in plain and "activity" not in plain
    old = ChatMessage.from_dict({"role": "assistant", "content": "x", "created_at": "2026-09-12T08:00:00+00:00", "message_id": "m"})
    assert old.reasoning == "" and old.activity == []


def test_thinking_panel_survives_reopening_the_conversation(tmp_path):
    from pet.chat.prompt import PromptBuilder
    from pet.chat.widgets import ChatWindow, MessageBubble
    from pet.config import Config

    class Provider:
        def stream(self, messages, config, cancel):
            yield StreamEvent("status", "🔍 正在搜索：天气")
            yield StreamEvent("reasoning", "先看搜索结果再回答")
            yield "晴天"

    app = _qapp()
    cfg = Config(tmp_path)
    win = ChatWindow(cfg, "shenshen")
    win.service.provider = Provider()
    win.composer.input.setPlainText("天气怎么样")
    win.send_message()
    deadline = time.time() + 5
    while time.time() < deadline and not (win.session.messages and win.session.messages[-1].role == "assistant"):
        app.processEvents()
        time.sleep(0.01)
    assert win.session.messages[-1].reasoning == "先看搜索结果再回答"
    win.store.flush()
    win.close()

    reopened = ChatWindow(Config(tmp_path), "shenshen")
    app.processEvents()
    bubble = [b for b in reopened.findChildren(MessageBubble) if b.role == "assistant"][-1]
    assert bubble.content == "晴天"
    assert not bubble.thinking_toggle.isHidden()
    assert not bubble.thinking_toggle.isChecked()  # 回显时收起
    assert "已联网搜索并深度思考" in bubble.thinking_toggle.text()
    bubble.thinking_toggle.click()
    assert "先看搜索结果再回答" in bubble.thinking_body.text()
    # 思维链只用于回显，不进模型上下文
    sent = PromptBuilder().build_messages(reopened.settings, "shenshen", reopened.session.messages, "下一个问题")
    assert "先看搜索结果再回答" not in json.dumps(sent, ensure_ascii=False)
    reopened.close()
