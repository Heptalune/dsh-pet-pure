# -*- coding: utf-8 -*-
"""小米 MiMo TTS provider（``mimo-v2.5-tts`` 系列，OpenAI 兼容接口）。

接口要点（官方文档）：
- ``POST https://api.xiaomimimo.com/v1/chat/completions``，音频以 base64 内联返回；
- 鉴权：官方 curl 用 ``api-key`` 头，OpenAI SDK 用 ``Authorization: Bearer``，两个都带；
- **待合成文本必须放在 assistant 消息**；user 消息是可选风格指令（音色设计模型下必填）；
- 内置音色 8 款（中文 冰糖/茉莉/苏打/白桦，英文 Mia/Chloe/Milo/Dean），也可用
  ``mimo-v2.5-tts-voicedesign`` 由文字描述现造音色。

API Key 存系统钥匙串（``pet/tts_secrets``，ref ``tts/mimo``）：本模块只声明
``kind="secret"`` 字段，不碰 keyring——服务层读出后注入 ``values``。
Key 在小米开放平台（``platform.xiaomimimo.com``）生成：这句只留在这里，
设置页不讲课（机主 2026-10-13：「各种信息太复杂了……写得更人性化和简洁一点」）。
"""

from __future__ import annotations

import base64
import json
import re
import urllib.request

from .base import TtsField, TtsProvider, TtsUnavailable, register

MIMO_BASE_URL = "https://api.xiaomimimo.com"
MIMO_CHAT_PATH = "/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5-tts"  # 内置音色
MIMO_MODEL_VOICE_DESIGN = "mimo-v2.5-tts-voicedesign"  # 用文字描述生成音色
MIMO_MODELS: tuple[tuple[str, str], ...] = (
    (MIMO_MODEL, "内置音色（8 款）"),
    (MIMO_MODEL_VOICE_DESIGN, "音色设计（用文字描述音色）"),
)
DEFAULT_MIMO_MODEL = MIMO_MODEL
# 内置音色 id 就是中文/英文名本身（见官方文档的音色表）
MIMO_VOICE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("冰糖", "冰糖（女 · 中文默认）"),
    ("茉莉", "茉莉（女 · 中文）"),
    ("苏打", "苏打（男 · 中文）"),
    ("白桦", "白桦（男 · 中文）"),
    ("Mia", "Mia（女 · 英文）"),
    ("Chloe", "Chloe（女 · 英文）"),
    ("Milo", "Milo（男 · 英文）"),
    ("Dean", "Dean（男 · 英文）"),
)
DEFAULT_MIMO_VOICE = "冰糖"
DEFAULT_MIMO_STYLE = ""  # 风格指令：自然语言描述语气/情绪/节奏；空则不发 user 消息
MIMO_STYLE_MAX_LEN = 300
MIMO_AUDIO_FORMAT = "wav"  # 非流式请求的输出格式（缓存扩展名随之）
MIMO_TIMEOUT_S = 30.0
AVAILABILITY_CODE = "mimo-key-missing"
MISSING_MESSAGE = "小米 MiMo 未配置 API Key：桌宠设置 → 语音 → 语音报时 里填一次即可"
SECRET_REF = "tts/mimo"


def clean_mimo_model(value) -> str:
    """清洗模型 id；非法回落内置音色模型。"""
    text = str(value or "").strip()
    return text if text in {model for model, _ in MIMO_MODELS} else DEFAULT_MIMO_MODEL


def clean_mimo_voice(value) -> str:
    """清洗音色 id（内置音色名就是 id 本身）；空值回落默认音色。"""
    text = str(value or "").strip()
    return text[:64] if text else DEFAULT_MIMO_VOICE


def clean_mimo_style(value) -> str:
    """清洗风格指令：去控制字符、压掉换行、按上限截断。"""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))
    text = re.sub(r"\s*\r?\n\s*", " ", text).strip()
    return text[:MIMO_STYLE_MAX_LEN]


def mimo_messages(text: str, values: dict) -> list[dict]:
    """构造 messages：文本在 assistant、风格指令在可选 user。"""
    messages: list[dict] = []
    style = clean_mimo_style(values.get("style", ""))
    if style:
        messages.append({"role": "user", "content": style})
    messages.append({"role": "assistant", "content": str(text or "")})
    return messages


def mimo_payload(text: str, values: dict) -> dict:
    """MiMo TTS 非流式请求体（内置音色模型才发 voice；音色设计模型不发）。"""
    audio: dict = {"format": MIMO_AUDIO_FORMAT}
    if clean_mimo_model(values.get("model")) != MIMO_MODEL_VOICE_DESIGN:
        audio["voice"] = clean_mimo_voice(values.get("voice"))
    return {
        "model": clean_mimo_model(values.get("model")),
        "messages": mimo_messages(text, values),
        "audio": audio,
        "stream": False,
    }


def parse_mimo_audio(payload) -> bytes:
    """从 MiMo 响应里解出音频字节（base64）。

    只接受官方文档记录的结构 ``choices[0].message.audio.data``；解不出就抛
    ``ValueError`` 并带上可读原因（服务层把异常文本回给气泡，用户至少知道
    发生了什么，而不是「合成失败」四个字）。
    """
    if not isinstance(payload, dict):
        raise ValueError("响应不是 JSON 对象")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        message = payload.get("error") or payload.get("message") or "响应里没有 choices"
        raise ValueError(str(message)[:120])
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    audio = message.get("audio") if isinstance(message, dict) else None
    data = audio.get("data") if isinstance(audio, dict) else None
    if not data:
        raise ValueError("响应里没有音频数据")
    try:
        return base64.b64decode(str(data))
    except Exception as exc:  # noqa: BLE001 —— 统一转成可读错误
        raise ValueError(f"音频数据解码失败：{exc}") from exc


class MimoTtsProvider(TtsProvider):
    id = "mimo"
    label = "小米 MiMo（需 API Key）"
    is_fallback = False
    availability_code = AVAILABILITY_CODE
    unavailable_message = MISSING_MESSAGE
    fields = (
        TtsField(
            "model", "voice_chime_mimo_model", "音色来源",
            "内置音色，或按文字描述现造。",
            kind="select", default=DEFAULT_MIMO_MODEL, options=MIMO_MODELS,
        ),
        TtsField(
            "voice", "voice_chime_mimo_voice", "音色",
            "小米自带音色，中英文都有。",
            kind="select", default=DEFAULT_MIMO_VOICE, options=MIMO_VOICE_OPTIONS,
            hidden_when=("model", MIMO_MODEL_VOICE_DESIGN),
        ),
        TtsField(
            "style", "voice_chime_mimo_style", "风格指令",
            "一句话描述语气，音色设计必填。",
            kind="multiline", default=DEFAULT_MIMO_STYLE, max_length=MIMO_STYLE_MAX_LEN,
            min_height=88,
            placeholder="如：轻快、带点笑意（留空按默认）",
        ),
        TtsField(
            "api_key", "voice_chime_mimo_api_key", "API Key",
            "去小米开放平台生成。",
            kind="secret", default="", secret_ref=SECRET_REF,
        ),
    )

    def plan(self, values: dict) -> dict:
        return {
            "provider": self.id,
            "model": clean_mimo_model(values.get("model")),
            "voice": clean_mimo_voice(values.get("voice")),
            "style": clean_mimo_style(values.get("style", "")),
            # 密钥只进 plan（合成要用），不进 flavor/缓存键，也不进日志。
            "api_key": str(values.get("api_key") or ""),
            "ext": MIMO_AUDIO_FORMAT,
        }

    def flavor(self, values: dict) -> str:
        # 前缀 mimo 让它与 edge 的 flavor 天然不同命名空间。
        return (
            f"mimo|{clean_mimo_model(values.get('model'))}"
            f"|{clean_mimo_voice(values.get('voice'))}"
            f"|{clean_mimo_style(values.get('style', ''))}"
        )

    def availability(self, values: dict) -> str:
        return "" if str(values.get("api_key") or "").strip() else AVAILABILITY_CODE

    def synth(self, text: str, plan: dict, out_path) -> None:
        """走 pet/http_util（系统代理失败改直连）打同步 HTTP，写出 wav 字节。"""
        from .. import http_util

        key = str(plan.get("api_key") or "").strip()
        if not key:
            raise TtsUnavailable(AVAILABILITY_CODE)
        body = json.dumps(mimo_payload(text, plan), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            MIMO_BASE_URL.rstrip("/") + MIMO_CHAT_PATH,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "api-key": key,
                "Authorization": f"Bearer {key}",
            },
        )
        with http_util.urlopen(request, timeout=MIMO_TIMEOUT_S) as response:
            raw = response.read()
        audio = parse_mimo_audio(json.loads(raw.decode("utf-8", "replace")))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)


PROVIDER = register(MimoTtsProvider())
