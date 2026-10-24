# -*- coding: utf-8 -*-
"""全局记忆 v2a 的检索层：分层常量、关键词、BM25 风格打分、已见去重、注入组装。

纯 Python、零 Qt、零第三方依赖。存储仍由 ``pet/chat/memory.py`` 负责；本模块只
接受 ``entries`` 列表（dict）并做内存计算，方便单测直接钉住打分单调性。

对齐 ``.scratch/memory-v2/SPEC-v2a.md``：
- 四层 ``user/fact/lesson/topic``；缺省/未知层按 ``fact``；
- user 层恒定注入（≤600 字符，按 importance + 新近度截）；
- fact/lesson/topic 按需命中 top-3（≤600 字符），首轮（turn_index<=1）不命中；
- 打分 = BM25 长度归一后的 idf 分 × 艾宾浩斯衰减 × importance；
- 命中门槛（v2f）：至少一个长度 ≥2 的命中词；只保留本轮最高分 25% 以上的
  条目（相对门槛随语料自适应，绝对 idf 门槛会随 df/N 漂移）；score ≤ 0 永不注入；
- 已见去重：top-3 的前 2 条不排除已见，第 3 条起跳过已见；
- topic 连续 14 天未命中自动归档（归档条目不参与命中）；
- 空命中不注入占位。

v2b：条目/查询关键词上限放宽到整条记忆（48 / 64），ASCII 单词与版本号也入
关键词；idf 按**全部未归档条目**统计；注入候选改由显式 ``MemoryPayload`` 传入，
不再使用线程暂存（``augment_for_turn`` 的 entries / session_key 由调用方给）。
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

LAYERS = ("user", "fact", "lesson", "topic")
DEFAULT_LAYER = "fact"
LAYER_LABELS = {
    "user": "用户偏好",
    "fact": "事实",
    "lesson": "教训",
    "topic": "话题",
}

HALF_LIFE_DAYS = 14.0
TOPIC_ARCHIVE_DAYS = 14.0
USER_CHAR_LIMIT = 600
RECALL_CHAR_LIMIT = 600
RECALL_TOP_K = 3
MAX_KEYWORDS = 48

#: BM25 长度归一常数：k1=1.2、b=0.6 是标准取值（Robertson & Zaragoza,
#: "The Probabilistic Relevance Framework: BM25 and Beyond", 2009；
#: Lucene/Elasticsearch 的 BM25Similarity 默认同值）。v2c 用它替换会被长
#: 条目稀释的 coverage 因子。
BM25_K1 = 1.2
BM25_B = 0.6
#: 命中门槛（v2f）：删掉「命中词 idf 和 ≥ MIN_SCORE」这条绝对门槛——idf 随
#: df/N 漂移，全库共享一个主题词时（df/N→1）同主题条目会被整体挡掉。改为
#: 相对门槛：只保留本轮最高分 25% 以上的条目，门槛随语料自适应。
#: v2c 误注入基线（20 条真实风格记忆 × 30 句无关闲聊）实测仍为 1/30。
RELATIVE_MIN_RATIO = 0.25

_QUERY_KEYWORD_LIMIT = 64
_SECONDS_PER_DAY = 86400.0
#: 拉丁/数字词（含版本号这类点分形式）与 CJK 串；版本号匹配后还会拆出相邻 bigram。
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*|[\u3400-\u4dbf\u4e00-\u9fff]+", re.UNICODE)


def _is_cjk_token(token: str) -> bool:
    """``_TOKEN_RE`` 的 CJK 分支：只看首字符即可（整段都在同一字符类里）。

    不用 ``^[...]+$`` 重扫整段：100k 字的纯 CJK 查询上那是一次多余的线性扫描
    （v2c L3 的耗时预算）。
    """
    first = token[0]
    return "\u3400" <= first <= "\u4dbf" or "\u4e00" <= first <= "\u9fff"


#: 高频虚词不单独成为关键词；只影响单字/整词，不影响含实义字的 bigram。
_STOPWORDS = frozenset(
    "的 了 是 在 我 你 他 她 它 们 这 那 就 也 都 和 与 吗 呢 啊 呀 吧 哦 嗯 不 有".split()
)

_USER_HEADER = (
    "【关于用户的记忆】以下是用户的偏好与习惯，回答时自然参考，不要逐字复述。"
)
_RECALL_HEADER = (
    "【相关记忆】以下是与当前话题相关的记忆，只在有帮助时自然参考，不要逐字复述。"
)


def normalize_layer(value: Any) -> str:
    """任意值 → 合法层名；缺省/未知一律 ``fact``（老条目兼容）。"""
    layer = str(value or "").strip().lower()
    return layer if layer in LAYERS else DEFAULT_LAYER


def layer_of(entry: dict[str, Any]) -> str:
    """条目层名（缺省 fact）。"""
    return normalize_layer(entry.get("layer"))


def extract_keywords(text: Any, limit: int = MAX_KEYWORDS) -> list[str]:
    """从文本提取关键词：CJK bigram + 拉丁/数字词，去重、保序、截到 ``limit``。

    单字 CJK 且在虚词表里的跳过；双字 bigram 不拆（例如「不吃」「周三」）。
    ASCII/数字词长度必须 ≥2（``a`` / ``0`` 这类单字符不入关键词，v2c M4：
    否则「第 4 点」会命中版本号里的裸 ``4``）。点分版本号整串保留，并额外
    产出相邻段的 bigram（``4.4.0`` → ``4.4.0`` / ``4.4`` / ``4.0``，**不**产出
    裸 ``4`` / ``0``）。内层循环达到 ``limit`` 立即返回，避免超长 CJK 串先造出
    全部 bigram（v2c L3）。
    同一套函数用于写入（手动钉/提炼 ops）与查询，避免两边口径漂移。
    """
    if limit <= 0:
        return []
    # 不整段 lower()：100k 纯 CJK 查询上那是白扫一遍；拉丁/数字 token 在
    # 分支内各自 lower 即可，CJK 没有大小写。
    raw = str(text or "")
    keywords: list[str] = []
    seen: set[str] = set()
    scanned = 0

    def _add(token: str) -> bool:
        """加入一个关键词；返回是否已处理到 ``limit`` 个候选。"""
        nonlocal scanned
        if scanned >= limit:
            return True
        scanned += 1
        if token and token not in seen and token not in _STOPWORDS:
            seen.add(token)
            keywords.append(token)
        return scanned >= limit

    for match in _TOKEN_RE.finditer(raw):
        token = match.group(0)
        if _is_cjk_token(token):
            if len(token) == 1:
                if _add(token):
                    break
                continue
            for index in range(len(token) - 1):
                if _add(token[index:index + 2]):
                    return keywords[:limit]
        elif "." in token:
            token = token.lower()
            # 版本号：整串 + 相邻段 bigram（4.4.0 → 4.4.0 / 4.4 / 4.0）。
            # 单段（4 / 0）在 v2c M4 起不再入关键词。
            if _add(token):
                return keywords[:limit]
            parts = token.split(".")
            for index in range(len(parts) - 1):
                if _add(f"{parts[index]}.{parts[index + 1]}"):
                    return keywords[:limit]
        else:
            token = token.lower()
            # ASCII/数字词：单字符没有检索价值，还会制造「第 4 点」这类误命中。
            if len(token) >= 2 and _add(token):
                break
    return keywords[:limit]


def _entry_keywords(entry: dict[str, Any]) -> list[str]:
    """条目关键词：优先持久化的 ``keywords``，缺失/空则现场 bigram 兜底。"""
    raw = entry.get("keywords")
    cleaned: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            token = str(item or "").strip().lower()
            if token and token not in cleaned:
                cleaned.append(token)
    if cleaned:
        return cleaned
    return extract_keywords(entry.get("text", ""))


def _entry_time(entry: dict[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            return datetime.fromisoformat(str(entry.get(key, ""))).timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now().astimezone()).astimezone()


def decay(entry: dict[str, Any], now: datetime | None = None) -> float:
    """艾宾浩斯衰减：``0.5 ** (age_days / 14)``（未来时间按 0 天）。"""
    age_days = max(0.0, (_now(now).timestamp() - _entry_time(entry, "updated_at", "created_at"))
                   / _SECONDS_PER_DAY)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def importance(entry: dict[str, Any]) -> float:
    """importance：manual=1.2 / auto=1.0；lesson 层恒 ×1.3。"""
    weight = 1.2 if str(entry.get("source", "")).strip().lower() == "manual" else 1.0
    if layer_of(entry) == "lesson":
        weight *= 1.3
    return weight


def _topic_last_activity(entry: dict[str, Any]) -> float:
    return max(
        _entry_time(entry, "last_hit_at"),
        _entry_time(entry, "updated_at"),
        _entry_time(entry, "created_at"),
    )


def _topic_is_stale(entry: dict[str, Any], now: datetime) -> bool:
    last = _topic_last_activity(entry)
    if last <= 0:
        return False
    return (_now(now).timestamp() - last) / _SECONDS_PER_DAY >= TOPIC_ARCHIVE_DAYS


def is_archived(entry: dict[str, Any], now: datetime | None = None) -> bool:
    """topic 是否归档：显式 ``archived`` 或连续 14 天未命中。非 topic 恒 False。"""
    if layer_of(entry) != "topic":
        return False
    if bool(entry.get("archived")):
        return True
    return _topic_is_stale(entry, _now(now))


def archive_stale_topics(entries: list[dict[str, Any]], now: datetime | None = None) -> bool:
    """把连续 14 天未命中的 topic 标成 ``archived``；返回是否有改动（纯内存）。"""
    moment = _now(now)
    changed = False
    for entry in entries:
        if (layer_of(entry) == "topic"
                and not bool(entry.get("archived"))
                and _topic_is_stale(entry, moment)):
            entry["archived"] = True
            changed = True
    return changed


def _corpus_stats(entries: list[dict[str, Any]]) -> tuple[dict[str, float], float]:
    """一次遍历算 idf 与 avgdl（BM25 长度归一的分母基准）。

    ``idf`` 用全库未归档条目统计（v2b G 口径）；``avgdl`` 是同一批条目的
    关键词个数均值。空语料返回 avgdl=1.0，避免除零。
    """
    total = max(1, len(entries))
    document_frequency: dict[str, int] = {}
    total_length = 0
    for entry in entries:
        keywords = _entry_keywords(entry)
        total_length += len(keywords)
        for keyword in set(keywords):
            document_frequency[keyword] = document_frequency.get(keyword, 0) + 1
    idf = {
        keyword: math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
        for keyword, frequency in document_frequency.items()
    }
    avgdl = total_length / total if total_length > 0 else 1.0
    return idf, max(avgdl, 1e-9)


def score_entry(
    entry: dict[str, Any],
    query_keywords: set[str],
    idf: dict[str, float],
    now: datetime | None = None,
    *,
    avgdl: float | None = None,
) -> float:
    """单条打分：BM25 长度归一的命中 idf 和 × 衰减 × importance；无命中 = 0。

    门槛①：命中词必须至少有一个长度 ≥2（v2c M4：单字符/全库常见词不足以
    支撑一次注入）。绝对 idf 门槛已删除（v2f N2：它随 df/N 漂移，会让共享
    主题词的整组记忆检索不到）；相对门槛由 ``recall`` 用本轮最高分统一应用。
    ``avgdl`` 缺省按本条自身长度兜底，生产路径（``rank_entries`` / ``recall``）
    会传入全库均值。
    """
    keywords = _entry_keywords(entry)
    if not keywords or not query_keywords:
        return 0.0
    matched = [keyword for keyword in keywords if keyword in query_keywords]
    if not matched:
        return 0.0
    # 门槛①：至少一个长度 ≥2 的命中词（排除「第 4 点」命中裸 4）。
    if not any(len(keyword) >= 2 for keyword in matched):
        return 0.0
    intersection = sum(idf.get(keyword, 0.0) for keyword in matched)
    # score ≤ 0（无 idf 权重或衰减下溢）永不返回正分，调用方据此排除注入。
    if intersection <= 0.0:
        return 0.0
    corpus_avgdl = avgdl if avgdl is not None and avgdl > 0 else float(len(keywords))
    denominator = 1.0 + BM25_K1 * (
        1.0 - BM25_B + BM25_B * len(keywords) / corpus_avgdl)
    bm25 = intersection * (BM25_K1 + 1.0) / denominator
    return bm25 * decay(entry, now) * importance(entry)


def rank_entries(
    entries: list[dict[str, Any]],
    query: str,
    *,
    now: datetime | None = None,
    idf: dict[str, float] | None = None,
    avgdl: float | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    """按命中分降序返回 ``(score, entry)``；user 层与归档 topic 不参与。

    ``idf`` / ``avgdl`` 由调用方传入时按它打分（recall 用全部未归档条目统计，
    避免候选集大小改变让排序反转）；不传则沿用旧口径按候选集现算。
    """
    moment = _now(now)
    query_keywords = set(extract_keywords(query, limit=_QUERY_KEYWORD_LIMIT))
    if not query_keywords:
        return []
    candidates = [
        entry for entry in entries
        if layer_of(entry) != "user" and not is_archived(entry, moment)
    ]
    if idf is not None and avgdl is not None:
        weights, corpus_avgdl = idf, avgdl
    else:
        weights, corpus_avgdl = _corpus_stats(candidates)
        if idf is not None:
            weights = idf
    scored = []
    for entry in candidates:
        score = score_entry(
            entry, query_keywords, weights, moment, avgdl=corpus_avgdl)
        if score > 0:
            scored.append((score, entry))
    scored.sort(
        key=lambda item: (
            -item[0],
            -_entry_time(item[1], "updated_at", "created_at"),
            str(item[1].get("id", "")),
        )
    )
    return scored


@dataclass(frozen=True)
class RecallResult:
    """一次检索的结果：注入条目、渲染文本、更新后的已见 id 集合。"""

    entries: tuple[dict[str, Any], ...] = ()
    text: str = ""
    seen: frozenset[str] = field(default_factory=frozenset)


def _render_entries(
    header: str,
    entries: list[dict[str, Any]],
    char_limit: int,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    limit = max(0, int(char_limit))
    section = header
    included: list[dict[str, Any]] = []
    for entry in entries:
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        candidate = f"{section}\n- {text}"
        if len(candidate) > limit:
            break
        section = candidate
        included.append(entry)
    if not included:
        return "", ()
    return section, tuple(included)


def render_user_snapshot(
    entries: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    char_limit: int = USER_CHAR_LIMIT,
) -> str:
    """user 层恒定注入段：按 importance × 新近度排序，硬字符上限。"""
    moment = _now(now)
    user_entries = [
        entry for entry in entries
        if layer_of(entry) == "user" and str(entry.get("text", "")).strip()
    ]
    user_entries.sort(
        key=lambda entry: (
            -(importance(entry) * decay(entry, moment)),
            -_entry_time(entry, "updated_at", "created_at"),
        )
    )
    text, _included = _render_entries(_USER_HEADER, user_entries, char_limit)
    return text


def _entry_key(entry: dict[str, Any]) -> str:
    """已见去重键：优先持久化 ``id``；老条目缺 id 时用正文哈希（不落盘）。

    v2f L3：缺 id 的条目若统一返回空串，所有条目共享同一个已见键，第 3 个
    名额从第二轮起永久关闭。这里用正文的 sha1 十六进制保证不同条目键不同；
    该键只在进程内 ``_SEEN`` 使用，不改变磁盘结构。
    """
    entry_id = str(entry.get("id", "") or "").strip()
    if entry_id:
        return entry_id
    text = str(entry.get("text", ""))
    return "text:" + hashlib.sha1(text.encode("utf-8")).hexdigest()


def recall(
    entries: list[dict[str, Any]],
    query: str,
    *,
    seen: frozenset[str] | set[str] | None = None,
    now: datetime | None = None,
    turn_index: int = 2,
    top_k: int = RECALL_TOP_K,
    char_limit: int = RECALL_CHAR_LIMIT,
) -> RecallResult:
    """按需命中 top-k 并渲染；首轮（``turn_index <= 1``）恒空。

    已见规则：排序后的前 2 条无论是否已见都注入；第 3 条起跳过已见，
    顺延到下一个未见条目（防止「越搜越偏」）。
    v2f N2：相对门槛只在本轮最高分 25% 以上的条目里选（最高分 ≤0 直接不注入），
    绝不让 score ≤ 0 的条目进入注入段。
    """
    seen_ids = frozenset(str(item) for item in (seen or ()))
    if int(turn_index) <= 1:
        return RecallResult(seen=seen_ids)
    moment = _now(now)
    # idf / avgdl 用全部未归档条目统计（含 user 层）：候选集大小/无关条目增减不再
    # 改变既有条目的相对稀有度（v2b 盲审 G：N=3 与 N=42 排序反转）。
    idf, avgdl = _corpus_stats(
        [entry for entry in entries if not is_archived(entry, moment)])
    ranked = rank_entries(entries, query, now=moment, idf=idf, avgdl=avgdl)
    if not ranked:
        return RecallResult(seen=seen_ids)
    best_score = ranked[0][0]
    if best_score <= 0:
        return RecallResult(seen=seen_ids)
    cutoff = best_score * RELATIVE_MIN_RATIO
    # 先按门槛筛出候选，再做已见去重：这里的 index 就是"排序后的前 2 条"。
    candidates = [
        (score, entry) for score, entry in ranked
        if score > 0 and score >= cutoff
    ]
    selected: list[dict[str, Any]] = []
    for index, (_score, entry) in enumerate(candidates):
        if len(selected) >= max(1, int(top_k)):
            break
        entry_id = _entry_key(entry)
        if index < 2 or entry_id not in seen_ids:
            selected.append(entry)
    if not selected:
        return RecallResult(seen=seen_ids)
    text, included = _render_entries(_RECALL_HEADER, selected, char_limit)
    if not included:
        return RecallResult(seen=seen_ids)
    updated = seen_ids | {_entry_key(entry) for entry in included}
    return RecallResult(entries=included, text=text, seen=frozenset(updated))


# ---------------------------------------------------------------- 已见去重
_SEEN_LOCK = threading.Lock()
_SEEN: OrderedDict[tuple[str, str], frozenset[str]] = OrderedDict()
_SEEN_MAX_SESSIONS = 128


def _history_key(history: list[Any] | tuple[Any, ...] | None) -> str:
    if not history:
        return ""
    first = history[0]
    return str(
        getattr(first, "message_id", "")
        or getattr(first, "created_at", "")
        or ""
    )


def augment_for_turn(
    entries: list[dict[str, Any]],
    session_key: str,
    history: list[Any] | tuple[Any, ...] | None,
    user_text: str,
    *,
    now: datetime | None = None,
    note_hits: Callable[[list[str]], Any] | None = None,
) -> str:
    """用显式传入的条目返回本轮的 recall 段；首轮/无命中 → 空串。

    ``session_key`` 是 ``_SEEN`` 的命名空间（记忆文件路径），与条目一起由
    ``MemoryPayload`` 显式传给 ``build_messages``——不再有线程暂存，因此同一
    线程上的另一个会话不可能取走别人的记忆。

    ``note_hits`` 每轮都会收到本轮被注入条目的 id（可能为空）：空列表让
    ``note_recalled`` 有机会做一次低频的归档巡检（v2b 盲审 F）。
    """
    query = str(user_text or "").strip()
    if not query:
        return ""
    has_history = bool(history)
    session = (str(session_key or ""), _history_key(history))
    if not has_history:
        # v2f L2：首轮清空必须按 session_key 前缀清掉该会话的**所有**历史桶。
        # 旧代码只 pop (session_key, "")，而正常键是 (session_key, 首条消息 id)，
        # 永远对不上，是死代码。
        prefix = str(session_key or "")
        with _SEEN_LOCK:
            stale_keys = [key for key in _SEEN if key[0] == prefix]
            for key in stale_keys:
                _SEEN.pop(key, None)
        return ""
    with _SEEN_LOCK:
        seen = _SEEN.get(session, frozenset())
    result = recall(entries, query, seen=seen, now=now, turn_index=2)
    if result.seen:
        with _SEEN_LOCK:
            _SEEN[session] = result.seen
            _SEEN.move_to_end(session)
            while len(_SEEN) > _SEEN_MAX_SESSIONS:
                _SEEN.popitem(last=False)
    if note_hits is not None:
        # 发版交叉审 M1：上报全部被注入条目的 id，不只 topic——fact/lesson 的
        # hits 也要累计（淘汰与注入排序都看 hits）；``last_hit_at`` 的刷新仍
        # 只留给 topic（``_apply_hit_batch`` 内部分层判断）。空列表仍会触发一次
        # 低频归档巡检（v2b 盲审 F）。
        hit_ids = [str(entry.get("id", "")) for entry in result.entries]
        try:
            note_hits(hit_ids)
        except Exception:  # noqa: BLE001 - 命中记录失败绝不打断对话
            pass
    return result.text
