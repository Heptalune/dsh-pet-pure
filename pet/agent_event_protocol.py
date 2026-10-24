# -*- coding: utf-8 -*-
"""Unified, bounded Agent event protocol."""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any, Mapping

log = logging.getLogger(__name__)

SCHEMA = "agent-event/v1"
MAX_DATA_BYTES = 16 * 1024
MAX_STRING_CHARS = 2000
#: **内容侧**（content / 正文 / 展示文本）剪裁下限。它只约束内容字段，语义字段
#: 不受它限制（见 SEMANTIC_KEYS）。64 本身是按"最短枚举字面量"定的：实测最长
#: 关键字面量 18 字符（RESOURCE_EXHAUSTED / thread_rolled_back /
#: exec_command_begin），UUID sessionId 36 字符。这个数是"内容还能留多少"，
#: 一旦当成语义值的下限用，剪掉的就不是字节而是控制流：ok:'error' 剪成 'e'
#: 后 ModelAccessTracker._resets 把失败当成功；failure.code:'429' 剪成 '4'
#: 后 is_model_access 认不出限流；长 errorMessage 尾部的"timed out"被剪掉后
#: 超时链也认不出（用例见 tests/test_agent_event_protocol.py 的超限组）。
MIN_CONTENT_STRING_CHARS = 64
#: 条目数下限同理：小结构（failure:{code,message} 这类）不参与收缩，收缩只作用于
#: 大容器（content 这类批量内容）。它是**内容条目**配额：语义项不参与计数、也
#: 不被它裁掉（见 `_content_items`）——按插入次序整片切会把排在后半段的控制流键
#: 一起带走。
MIN_CONTENT_ITEMS = 8
DEFAULT_ITEM_LIMIT = 128
#: 内容侧深度上限：超出即整棵子树换成 [truncated] 占位（同时兜住自引用结构）。
MAX_CONTENT_DEPTH = 5
#: 语义侧深度上限：语义键路径不做占位替换，超过上限即视为畸形负载 → 拒收。
MAX_SEMANTIC_DEPTH = 12


class EventPayloadTooLarge(ValueError):
    """语义字段本身装不下 MAX_DATA_BYTES：明确拒收，不得用截断值/占位符冒充。

    调用方（``agent_link._emit_unified_event``）已把解析包在 except 里：拒收只丢
    统一语义层，原始记录转发路径不受影响（与未知 schema 同一处理口径）。这比
    "剪短了但还在"安全——被剪坏的 ok/errorMessage 会被 normalizer 与
    ModelAccessTracker 当成另一件事（``data.get("ok", True)`` 默认成功）。
    """


#: 语义键（按小写比对）：这些字段的值承载**控制流、错误语义与身份**，不是内容。
#: 取名依据是各消费端真实读取的字段（persona_template.UPSTREAM_FIELDS 的"上游记录
#: 字段"清单、agent_event_normalizer、ModelAccessTracker、agent_link 的字段读取；
#: 卡住检测 / 行为模式检测 / 探索看门狗三个检测器已在 2026-10 减法中删除，不再是
#: 本集合的取名依据）：
#: - 判决/状态：ok/timeout/retryExhausted/failureType…——剪坏 ok 会让
#:   tracker._resets 与 normalizer 的 ok 判定把失败当成功；
#: - 错误码与消息：errorCode/errorMessage/errorText/code/failure…——
#:   ModelAccessTracker.is_model_access 在这上面跑限流/超时正则；
#:   `message` 同在错误消息的位置上：两个消费端（model_access_tracker 的
#:   is_model_access、agent_event_normalizer 的 llm/retry 分支）在 `failure`
#:   不是 dict 时把整个 data 当 failure 用，读的就是**根级 message**。展示内容
#:   走 text/content，不在这里；
#: - 身份：sessionId/callId/requestId/rpcId/approvalId…——agent_link 用**字符串
#:   相等**配对审批收尾（_on_normalized_event / _on_approval_resolved），而登记
#:   端存的是未修剪的原始 payload，剪短即永不相等（审批关不掉/错配）；
#: - 工具与来源枚举：tool/toolName/name/provider/model——归一化按 tool 字面量
#:   判 ACTION/EXPLORATION。
#: 注意 `failure` 这类容器在集合里：其内部键靠 `semantic` 逐层传递继续受保护
#: （`failure.message` 不在字面量集合里也要完整）。
SEMANTIC_KEYS: frozenset[str] = frozenset({
    # 判决 / 状态
    "ok", "timeout", "retryexhausted", "retries", "retry", "consecutiveretrycount",
    "failuretype", "errorkind", "evidencestatus", "status", "kind", "outcome",
    "decision", "phase", "operation", "action", "state",
    # 错误码 / 消息（正则消费者）
    "code", "errorcode", "errormessage", "errortext", "message", "failure",
    # 身份（字符串相等配对）
    "sessionid", "session_id", "projectid", "project_id", "callid", "call_id",
    "requestid", "request_id", "rpcid", "rpc_id", "approvalid", "approval_id",
    "questionrpcid", "agentid", "agent_id", "agent_key", "sessionname", "projectname",
    # 工具 / 来源枚举
    "tool", "toolname", "name", "provider", "model", "type", "event", "agent", "count",
})

#: 已知 schema 版本白名单。未知版本不再"静默当当前版处理"：版本号是
#: 上游桥接与 pet 之间的兼容契约，猜版本会让字段语义错位且无从发现。
#: 缺省（无 schema 字段）仍按当前版处理——老记录兼容。
KNOWN_SCHEMAS: frozenset[str] = frozenset({SCHEMA})


def _is_semantic_key(key: Any) -> bool:
    """该键是否承载语义（判决/错误码/身份/枚举）：是则永不被剪裁、也永不被删。"""
    return str(key).strip().lower() in SEMANTIC_KEYS


def _semantic_leaf(value: Any) -> Any:
    """语义键下的标量：原样返回——不截断、不替换占位符、不丢键。"""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _content_items(value: Mapping, item_limit: int) -> list[tuple[Any, Any]]:
    """按**内容条目**配额挑选要保留的 (k, v)：语义项一个不丢。

    为什么不能直接 `list(value.items())[:keep]`：字典按插入次序切，语义键只要
    排在配额之后就整体消失——上游记录里"一堆噪声键 + 末尾几个控制流键"完全正常，
    剪掉的就不是字节而是语义（ok:'error' 消失后 normalizer 走
    `data.get("ok", True)` 默认成功；failure.code 消失后限流漏计）。
    语义项的体量由 SEMANTIC_KEYS 的所有者（协议层预算）负责，不由这里负责。
    """
    quota = max(MIN_CONTENT_ITEMS, item_limit)
    content_seen = 0
    kept: list[tuple[Any, Any]] = []
    for key, item in value.items():
        if _is_semantic_key(key):
            kept.append((key, item))
        elif content_seen < quota:
            content_seen += 1
            kept.append((key, item))
    return kept


def _bounded(value: Any, depth: int = 0, *,
             str_cap: int = MAX_STRING_CHARS, item_limit: int = DEFAULT_ITEM_LIMIT,
             semantic: bool = False) -> Any:
    """把负载递归收口到预算：**内容侧剪裁，语义侧原样**。

    - 内容侧（semantic=False）：长字符串压到 str_cap、容器条目压到 item_limit
      （配额只数内容项，语义键不计入也不被裁）、**容器的键**按内容口径截到 120
      字符、超过 MAX_CONTENT_DEPTH 的子树整体换成 [truncated] 占位；
    - 语义侧（semantic=True，来自命中 SEMANTIC_KEYS 的键）：标量原样保留，容器
      整棵原样保留（子键即便不叫 ok/errorMessage 也受保护），**容器内部的键同样
      不截断**（键是标识符：截断会让共享前缀的长键碰撞合并，两条语义并成一条），
      超过 MAX_SEMANTIC_DEPTH 视为畸形负载 → 抛 EventPayloadTooLarge。
      语义容器**不参与内容条目配额**：配额是给"内容还能留多少"定的，作用到
      failure 这类容器上就变成"错误码能不能被看见"；装不下时由 bounded_data
      按预算明确拒收，绝不用截断值冒充。
    """
    if semantic:
        if not isinstance(value, (Mapping, list, tuple)):
            return _semantic_leaf(value)
        if depth > MAX_SEMANTIC_DEPTH:
            raise EventPayloadTooLarge(
                f"语义字段嵌套超过 {MAX_SEMANTIC_DEPTH} 层，拒绝用占位符冒充语义值")
    elif depth > MAX_CONTENT_DEPTH:
        return "[truncated]"
    if isinstance(value, str):
        return value if len(value) <= str_cap else value[:str_cap]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        items = list(value.items()) if semantic else _content_items(value, item_limit)
        # 键是标识符，不是内容：**语义容器内部**的键不截断——截断会让只在前 120
        # 字符之后才不同的键静默碰撞合并（两条语义并成一条，后写覆盖先写，无日志）。
        # 这样做的代价是"语义键本身就超预算"时会拒收（bounded_data 的出口），
        # 这正是语义侧既有口径：宁可明确拒收，也不给一份被合并过的语义。
        # 内容侧的键维持既有 120 字符截断（那里切的是内容标识，不是控制流身份）。
        return {(str(k) if semantic else str(k)[:120]): _bounded(v, depth + 1, str_cap=str_cap,
                                       item_limit=item_limit,
                                       semantic=semantic or _is_semantic_key(k))
                for k, v in items}
    if isinstance(value, (list, tuple)):
        items = list(value) if semantic else list(value)[:max(MIN_CONTENT_ITEMS, item_limit)]
        return [_bounded(v, depth + 1, str_cap=str_cap, item_limit=item_limit,
                         semantic=semantic)
                for v in items]
    return _semantic_leaf(value)


def _encoded_size(raw: dict[str, Any]) -> int:
    return len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def bounded_data(value: Any) -> dict[str, Any]:
    """把任意事件负载压进 MAX_DATA_BYTES（按 UTF-8 字节），**只裁内容，不动语义**。

    设计取舍：事件负载里"大"的是 content/正文，而控制流依赖的是语义字段
    （ok/failure.code/errorMessage/身份 ID）——它们的体量天然最小，却最有决定权。
    所以三级收口一律只动内容那一侧（语义键连碰都不碰，见 SEMANTIC_KEYS）：
    ① 压缩内容长字符串（> MIN_CONTENT_STRING_CHARS），按超限比例几何收敛；
    ② 仍超限则收紧**内容**容器的条目数（配额只数内容项，语义键不计入也不被裁，
       <= MIN_CONTENT_ITEMS 的小结构不动）；
    ③ 极端情况才丢键，且只丢**非语义**键里体量最大的那些
       （旧实现按插入顺序从尾部删，语义字段排在末尾就先被删掉，
       且单键承载大结构时会一路删到空）。

    出口只有两个：<= MAX_DATA_BYTES 的负载，或者"语义字段本身装不下"时抛
    EventPayloadTooLarge——绝不返回"语义被剪短但看起来还在"的负载，那会让
    normalizer / ModelAccessTracker 把失败读成成功。
    """
    if not isinstance(value, Mapping):
        return {}
    raw = _bounded(value)
    size = _encoded_size(raw)
    cap = MAX_STRING_CHARS
    while size > MAX_DATA_BYTES and cap > MIN_CONTENT_STRING_CHARS:
        target = cap * MAX_DATA_BYTES // max(1, size)
        if target >= cap:
            target = cap // 2
        cap = max(MIN_CONTENT_STRING_CHARS, target)
        raw = _bounded(value, str_cap=cap)
        size = _encoded_size(raw)
    limit = DEFAULT_ITEM_LIMIT
    while size > MAX_DATA_BYTES and limit > MIN_CONTENT_ITEMS:
        limit = max(MIN_CONTENT_ITEMS, limit // 2)
        raw = _bounded(value, str_cap=cap, item_limit=limit)
        size = _encoded_size(raw)
    if size > MAX_DATA_BYTES:
        biggest_first = sorted(
            raw,
            key=lambda k: len(json.dumps(raw[k], ensure_ascii=False, separators=(",", ":"))),
            reverse=True,
        )
        for key in biggest_first:
            if size <= MAX_DATA_BYTES:
                break
            if _is_semantic_key(key):
                continue  # 丢语义键等于把失败变成功/身份错配，宁可拒收
            raw.pop(key, None)
            size = _encoded_size(raw)
    if size > MAX_DATA_BYTES:
        raise EventPayloadTooLarge(
            f"语义字段本身超过 {MAX_DATA_BYTES} 字节（实得 {size}），拒绝截断")
    return raw

def _first(record: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in record and record[name] is not None: return record[name]
    return default

@dataclass(frozen=True)
class AgentEvent:
    schema: str
    timestamp: float
    source: str
    agent_name: str
    project_id: str
    project_name: str
    session_id: str
    session_name: str
    turn: int | str | None
    step: int | str | None
    event: str
    data: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    request_id: str = ""

    @classmethod
    def from_record(cls, record: Mapping[str, Any], *, source_hint: str = "", agent_name_hint: str = "") -> "AgentEvent":
        if not isinstance(record, Mapping): record = {}
        # 版本白名单先于字段解析：未知 schema 的字段语义不可信，拒收而不是
        # 按当前版猜测解析（调用方 agent_link 已把解析包在 try/except 里，
        # 拒收只影响统一语义层，原始记录转发路径不受影响）。
        schema = str(_first(record, "schema", default=SCHEMA) or SCHEMA)
        if schema not in KNOWN_SCHEMAS:
            log.warning("未知 Agent 事件 schema 版本，已拒收: %s", schema)
            raise ValueError(f"unsupported agent event schema: {schema}")
        source = str(_first(record, "source", "agent", default=source_hint) or source_hint)
        name = str(_first(record, "agentName", "agent_name", default=agent_name_hint or source) or source)
        session = str(_first(record, "sessionId", "session_id", default="") or "")
        project_id = str(_first(record, "projectId", "project_id", default="") or "")
        project_name = str(_first(record, "projectName", "project_name", default="") or "")
        session_name = str(_first(record, "sessionName", "session_name", default="") or "")
        event = str(_first(record, "event", "type", default="") or "")
        if not event and "state" in record:
            event = "AgentStatus"
        timestamp = _first(record, "ts", "timestamp", default=time.time())
        try: timestamp = float(timestamp)
        except (TypeError, ValueError): timestamp = time.time()
        data = _first(record, "data", default=None)
        if not isinstance(data, Mapping):
            excluded = {"schema", "ts", "timestamp", "source", "agent", "agentName", "agent_name", "projectId", "project_id", "projectName", "project_name", "sessionId", "session_id", "sessionName", "session_name", "turn", "step", "event", "type", "callId", "call_id", "requestId", "request_id"}
            data = {k: v for k, v in record.items() if k not in excluded}
        return cls(schema, timestamp, source, name, project_id, project_name, session, session_name, _first(record, "turn"), _first(record, "step"), event, bounded_data(data), str(_first(record, "callId", "call_id", default="") or ""), str(_first(record, "requestId", "request_id", default="") or ""))

def parse_agent_event(record: Mapping[str, Any], *, source_hint: str = "", agent_name_hint: str = "") -> AgentEvent:
    return AgentEvent.from_record(record, source_hint=source_hint, agent_name_hint=agent_name_hint)
