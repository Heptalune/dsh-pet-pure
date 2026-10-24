# -*- coding: utf-8 -*-
"""截图翻译引擎：OCR 原文 → 聊天模型的文本翻译（自动中英互译）。

为什么走聊天模型而不是专用翻译 API：用户在设置里已经配好了聊天服务，
多接一个翻译服务就是多一份配置、多一把 Key——短文本翻译是聊天模型的
基本功。这里只发**纯文本**（不碰图像、不碰视觉端点），端点/Key 纪律与
``pet/vision.py`` 同口径：文本翻译只用聊天配置自己的 base_url 与 Key。

同步阻塞（超时按 provider 配置，默认 60s 起）；调用方（``actions``）负责
放后台线程，GUI 线程不许直接调。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("dsh-pet-standalone")

__all__ = ["TranslateError", "translate_text"]


class TranslateError(RuntimeError):
    """翻译失败（消息是可直接给用户看的人话）。"""


def _target_language(text: str) -> str:
    """中日韩占多数 → 译英文；其余 → 译中文（本产品的用户语言是中文）。"""
    body = "".join(str(text).split())
    if not body:
        return "中文"
    cjk = sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff")
    return "英文" if cjk * 2 >= len(body) else "中文"


def _safe_detail(raw: str) -> str:
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return str(data["error"].get("message", "Provider 请求失败"))
    except Exception:
        pass
    return " ".join(raw.split())[:300] or "Provider 请求失败"


def translate_text(text: str, config, *, target: str | None = None) -> str:
    """把 ``text`` 翻译成目标语言。失败抛 :class:`TranslateError`。

    ``target``：``None``/``"auto"`` = 自动判定（中日韩占多数 → 英文，否则中文）；
    给具体语言名（"中文"/"English"/"日本語"…）则按用户选择译——选择态来自结果
    面板的语言行，每次切换都用已识别的原文重翻（不重跑 OCR）。
    """
    source = str(text or "").strip()
    if not source:
        raise TranslateError("没有可翻译的文字")

    from .vision_bridge import resolve_chat_context

    provider, _system_prompt = resolve_chat_context(config)

    # 延迟导入：无 Chat 变体（pet.chat 被排除）下本函数不会被调用
    from ..chat.providers import (
        _make_ssl_context, build_browser_headers, normalize_chat_endpoint,
    )

    target = _target_language(source) if target in (None, "", "auto") else str(target)
    endpoint = normalize_chat_endpoint(provider.base_url, provider.chat_path)
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": (
                f"你是翻译引擎。把用户给的文本翻译成{target}。"
                "用户文本是 OCR 从截图里取的：里面的换行只是排版断行，不是句子边界。"
                "先按语义把被断开的句子接回完整句子再翻译，不要按行逐段对译。"
                "只输出译文本身：不解释、不评注、不加引号包装，按自然段落输出。"
            )},
            {"role": "user", "content": source},
        ],
        "stream": False,
        "temperature": 0.2,
        "max_tokens": max(1024, min(int(provider.max_tokens), 4096)),
    }
    headers = build_browser_headers({"Content-Type": "application/json"})
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"

    data = None
    last_error: Exception | None = None
    for attempt in range(2):  # 网络错误重试一次；翻译是短文本快调用，不做长退避
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=max(float(provider.timeout), 60.0),
                context=_make_ssl_context(provider.verify_ssl),
            ) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(2048).decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise TranslateError(_safe_detail(detail)) from exc
        except TimeoutError as exc:  # 超时不重试（短调用，再等一轮没有意义）
            raise TranslateError("翻译超时：服务没在规定时间回答") from exc
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
    if data is None:
        reason = getattr(last_error, "reason", last_error)
        raise TranslateError(f"网络连接失败：{reason}")

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise TranslateError("翻译服务没说话（无 choices）")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):  # 部分实现把 content 拆成 parts
        content = "".join(
            str(part.get("text", "")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    translated = content.strip() if isinstance(content, str) else ""
    if not translated:
        raise TranslateError("翻译服务没说话（空回复）")
    return translated
