# -*- coding: utf-8 -*-
"""TTS 提供者接口与注册表——语音报时与具体合成服务之间的稳定缝。

**接入一个新 TTS 的完整步骤**：写一个 ``pet/tts/<name>.py``（声明 ``id`` / ``label`` /
``fields``，实现 ``plan()`` 与 ``synth()``），在 ``pet/tts/__init__.py`` 里 import 它。
设置页、配置清洗、合成尝试链、缓存键与裁剪、降级文案会自动跟上——**不需要改
voice_chime*、config.py、UI 或服务层**（配置键仍需在 config.py 登记，见各 provider
的 ``fields[].key``）。

分层边界是「纯」与「有 IO」：

- 纯层（无 Qt、无网络即可单测）：``TtsField`` 声明配置项、``values()`` 读清洗配置、
  ``plan()`` 产出合成描述、``flavor()`` 产出进缓存键的稳定片段、``availability()``
  判断当前配置下能不能用（只看配置与 ``find_spec``，不联网）；
- IO 层：``synth(text, plan, out_path)`` 真正合成（在后台线程跑），失败抛异常，
  由服务层决定回退与给用户的提示。

写 provider 的红线：

1. 模块顶层**不得** import Qt，也不得 import 重量级/可选依赖（``edge_tts`` 这类要
   惰性导入到 ``synth`` 里）——``tests/test_architecture.py::
   test_tts_providers_keep_top_level_imports_light`` 对 ``pet/tts/*.py`` 做机器化
   守卫（只扫顶层，函数体内的延迟 import 不受影响），违反即红；
2. 密码类字段用 ``kind="secret"`` + ``secret_ref``：值存系统钥匙串
   （``pet/tts_secrets``），服务层读出后注入 ``values``，**绝不进 config.json**；
3. ``flavor()`` 只放「影响音频内容」的参数，密钥与纯展示参数不要放——它进缓存键；
4. 拿不到依赖/凭据时不要静默降级成空音频：抛 ``TtsUnavailable(code)``（code 与
   ``availability()`` 的原因码一致），服务层据此回退到下一个后端并给出可操作提示。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

#: 字段类型 → 设置页控件（见 voice_chime_settings._build_field_widget）
#: - select 下拉 / text 单行 / multiline 多行 / number 数字 / flag 开关 / secret 密钥
FIELD_KINDS = ("select", "text", "multiline", "number", "flag", "secret")


@dataclass(frozen=True)
class TtsField:
    """一个 provider 的单个可配置项（声明式：设置页照它生成控件）。

    ``name`` 是 provider 内部短名（``values()`` / ``plan()`` / ``flavor()`` 用它），
    ``key`` 是顶层配置键（必须与 config.py 的默认值 dict 和 reload 白名单一致）。
    """

    name: str
    key: str
    label: str
    hint: str = ""
    kind: str = "text"
    default: object = ""
    options: tuple[tuple[str, str], ...] = ()
    placeholder: str = ""
    minimum: int = 0
    maximum: int = 100
    suffix: str = ""
    max_length: int = 0
    #: 多行框（kind="multiline"）的最小高度：设置页会整行铺开它，写整句描述用
    min_height: int = 80
    secret_ref: str = ""
    advanced: bool = False
    #: 当另一个字段取到指定值时本行隐藏：``(字段 name, 值)``。
    #: 例：MiMo 选「音色设计」模型时内置音色行没有意义。
    hidden_when: tuple[str, object] | None = None
    #: 下拉里没有当前配置值时，是否补一个「自定义：<值>」项（兼容手改配置文件）。
    allow_custom: bool = True


@dataclass
class TtsAttempt:
    """一次合成尝试：服务层据此查缓存、开工、判可用性。"""

    provider: str
    values: dict = dataclass_field(default_factory=dict)
    plan: dict = dataclass_field(default_factory=dict)

    @property
    def ext(self) -> str:
        return str(self.plan.get("ext") or "mp3")


class TtsUnavailable(RuntimeError):
    """合成时才发现自己不可用（缺库 / 缺 Key）；``code`` 与 ``availability()`` 一致。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class TtsProvider:
    """provider 基类：子类至少要给出 ``id`` / ``label`` / ``plan()`` / ``synth()``。"""

    #: 配置里的后端 id（小写、稳定，用户配置里会存这个值）
    id: str = ""
    #: 设置页下拉里的展示名
    label: str = ""
    #: 本后端的可配置项
    fields: tuple[TtsField, ...] = ()
    #: 是否可作为其他后端的后备（失败回退链）
    is_fallback: bool = False
    #: 不可用时的用户可见提示（给「怎么办」，不是「失败了」）
    unavailable_message: str = ""
    #: 不可用原因码（availability() 返回它；服务层据此选提示）
    availability_code: str = ""

    # ------------------------------------------------------------ 纯层
    def clean(self, spec: TtsField, raw) -> object:
        """按字段类型清洗配置值（子类可覆写做更严的校验）。"""
        if spec.kind == "flag":
            return _clean_flag(raw, bool(spec.default))
        if spec.kind == "number":
            return _clean_int(raw, int(spec.default or 0), int(spec.minimum), int(spec.maximum))
        text = str(raw if raw is not None else "").strip()
        if spec.max_length and len(text) > spec.max_length:
            text = text[: spec.max_length]
        if text:
            return text
        return str(spec.default or "")

    def values(self, config) -> dict:
        """从 config 读出本后端的全部字段值（密钥类留空，由服务层注入）。

        取值先按 ``spec.key``（正式配置键）。取不到时回退 ``spec.name``——那是
        「短名 → 值」的紧凑映射（语音报时的 normalize 输出、测试里的扁平 cfg），
        两条路都支持，省得调用方为了一个键去拼 config 形状。
        """
        getter = getattr(config, "get", None)
        values: dict[str, object] = {}
        for spec in self.fields:
            raw = spec.default
            if callable(getter):
                raw = getter(spec.key, None)
                if raw is None:
                    raw = getter(spec.name, spec.default)
            values[spec.name] = self.clean(spec, raw)
        return values

    def plan(self, values: dict) -> dict:
        """产出合成描述（含 ``ext``）；纯函数，可离线单测。"""
        raise NotImplementedError

    def flavor(self, values: dict) -> str:
        """进缓存键的稳定片段（只放影响音频内容的参数）。"""
        return ""

    def availability(self, values: dict) -> str:
        """空串 = 可用；否则返回原因码。只看配置与 find_spec，不联网。"""
        return ""

    def preflight(self, values: dict) -> tuple[dict, str]:
        """合成前的自检/校正：返回 ``(可能被修正的 values, 面向用户的提示)``。

        默认原样返回。provider 可以在这里把「配置里已经不成立的东西」换成能用的
        （典型：在线音色被下架 → 换默认音色），并把原因作为提示交给服务层转达给用户。
        **跑在 GUI 线程，不许联网**——要联网的校验放到 ``synth()`` 里。
        """
        return values, ""

    # ------------------------------------------------------------ IO 层
    def synth(self, text: str, plan: dict, out_path) -> None:
        """真正合成并把音频写到 ``out_path``；失败抛异常（缺依赖/凭据用 TtsUnavailable）。"""
        raise NotImplementedError


# ---------------------------------------------------------------- 注册表
_REGISTRY: dict[str, TtsProvider] = {}


def register(provider: TtsProvider) -> TtsProvider:
    """注册一个 provider（重复 id 直接报错，避免静默覆盖）。"""
    if not provider.id:
        raise ValueError("provider 必须声明 id")
    if provider.id in _REGISTRY:
        raise ValueError(f"provider id 重复：{provider.id}")
    _REGISTRY[provider.id] = provider
    return provider


def unregister(provider_id: str) -> None:
    """注销（测试用：验证第三方 provider 可即插即拔）。"""
    _REGISTRY.pop(provider_id, None)


def get(provider_id: str) -> TtsProvider | None:
    return _REGISTRY.get(str(provider_id or "").strip().lower())


def providers() -> tuple[TtsProvider, ...]:
    """全部 provider，按注册顺序（设置页下拉顺序即此顺序）。"""
    return tuple(_REGISTRY.values())


def provider_ids() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def labels() -> dict[str, str]:
    return {pid: provider.label for pid, provider in _REGISTRY.items()}


def fallback_ids(exclude: str = "") -> tuple[str, ...]:
    """可作后备的 provider（自身声明 is_fallback，且不是当前主后端）。"""
    return tuple(
        pid for pid, provider in _REGISTRY.items() if provider.is_fallback and pid != exclude
    )


def secret_fields(provider: TtsProvider) -> tuple[TtsField, ...]:
    return tuple(spec for spec in provider.fields if spec.kind == "secret")


def _clean_flag(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "开", "开启"):
        return True
    if text in ("0", "false", "no", "off", "关", "关闭"):
        return False
    return default


def _clean_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))
