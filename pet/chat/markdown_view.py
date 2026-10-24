"""AI 回复的 Markdown → Qt 富文本。

用 Qt 自带的 md4c 解析（QTextDocument.setMarkdown，GitHub 方言：表格、删除线、
任务列表、自动链接），不引入第三方依赖。几处取舍：

- ``MarkdownNoHTML``：模型输出里的原始 HTML 按字面显示，不渲染——联网搜索的网页
  内容可能诱导模型输出标签，渲染进 QLabel 没必要也不安全；
- 单个换行保留为换行：标准 Markdown 把单换行并成同一段，但桌宠的回复常用单换行
  分句（与 GitHub 评论 / 主流聊天界面的显示习惯一致）；代码块内不处理；
- 去掉文档级字体，让气泡沿用聊天窗口 QSS 的字体字号；代码换等宽字体并加浅底色。
- 场景扮演的动作段（括号包裹）在 Markdown 解析前先换 token，解析后还原成斜体弱色；
  modern 聊天窗、classic 聊天窗与快速对话气泡**统一走这里渲染**，落盘原文不变。
- 动作段是括号形状启发式：正文里的函数调用 ``f(x)``、``(共 3 个)`` 也会被当作
  动作段。这是已知限制，不按长度/含中文/含数字去猜（那样只会引入新的误判）。
"""
from __future__ import annotations

import html as html_mod
import re

from PySide6.QtGui import QTextDocument

_FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')
_LINK_TARGET_MAX_CHARS = 2000
_BODY_STYLE = re.compile(r'<body style="[^"]*">')
_LINK_COLOR = re.compile(r'(<a href="[^"]*">)<span style=" color:#[0-9a-fA-F]{6};">')
_TABLE_DELIM = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$')
_FONT_DECL =re.compile(r"font-family:'[^']*';")
_MONO_STYLED = "font-family:'Consolas','Courier New',monospace; background-color:rgba(127,127,127,0.16);"
# 聊天气泡里标题缩一档，避免 h1 比正文大太多
_HEADING_SIZE = re.compile(r'font-size:(xx-large|x-large);')
_HEADING_MAP = {'xx-large': 'x-large', 'x-large': 'large'}
# 场景扮演的动作段：全角（）或半角 () 整段包裹；允许多行（跨行连续动作段）。
# 括号配对与嵌套层级在 _action_candidates 里处理：只允许一层嵌套（外层整体作为
# 动作段），两层以上回退到只取最内层，不递归；有效性过滤在 _action_ranges。
_INLINE_CODE = re.compile(r'`[^`\n]*`')
# 裸 URL：http(s):// 或 www. 开头，到空白或中文标点为止；尖括号是 autolink
# 的边界，必须排除，否则会把 `<url>` 的收尾 `>` 一起吃进链接。`*` 与反引号是
# Markdown 强调/行内代码的边界，也不能吃进 URL——否则
# ``https://x.org/a_(b)**重点**`` 会把「重点」整段焊进 href。半角括号先收下，
# 末尾再按括号配平与句末标点修剪（见 _trim_url）。
_URL = re.compile(
    r'(?:https?://|(?<![A-Za-z0-9.])www\.)'
    r'[^\s<>，。、；：！？（）【】《》〈〉「」『』“”‘’…—～·`*]+',
    re.IGNORECASE,
)
_URL_TRAILING = (
    '.,;:!?"\''
    '`*_]}'
    '>~》」】'
)
# 斜体 + 弱色：半透明灰在浅色/深色气泡里都比正文弱一档（跟随主题的对比关系，
# 不写死某一种主题的底色）。整段（含括号）一起渲染。
_ACTION_STYLE = 'font-style:italic; color:rgba(128,128,128,0.88);'


def _features():
    f = QTextDocument.MarkdownFeature
    return f(f.MarkdownDialectGitHub.value | f.MarkdownNoHTML.value)


def _is_table_header(lines, i) -> bool:
    return 0 <= i < len(lines) - 1 and '|' in lines[i] and bool(_TABLE_DELIM.match(lines[i + 1]))


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """排序后合并重叠/相接区间，供双指针判断使用。"""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def _intersect_flags(
    spans: list[tuple[int, int]], ranges: list[tuple[int, int]]
) -> list[bool]:
    """``spans`` 已按起点升序：双指针判断每个区间是否与任一 ``ranges`` 相交。"""
    merged = _merge_ranges(ranges)
    flags: list[bool] = []
    index = 0
    for start, end in spans:
        while index < len(merged) and merged[index][1] <= start:
            index += 1
        flags.append(index < len(merged) and merged[index][0] < end)
    return flags


def _fence_marker(line: str) -> tuple[str, int, str] | None:
    """返回行首围栏的 (字符, 长度, 行内剩余内容)；不是围栏行返回 None。"""
    match = _FENCE.match(line)
    if match is None:
        return None
    run = match.group(1)
    return run[0], len(run), line[match.end():]


def _fenced_ranges(text: str) -> list[tuple[int, int]]:
    """围栏代码块的字符区间。

    单遍状态机：记住开启围栏的字符与长度，只有行首、同字符、长度不小于开启
    围栏且其后没有内容的行才收尾；``~`` 行不能提前关掉 ``` 块。
    """
    ranges: list[tuple[int, int]] = []
    position = 0
    fence: tuple[str, int] | None = None
    fence_start = 0
    for line in text.splitlines(keepends=True):
        marker = _fence_marker(line)
        if fence is None:
            if marker is not None:
                fence = (marker[0], marker[1])
                fence_start = position
        elif (marker is not None and marker[0] == fence[0]
              and marker[1] >= fence[1] and not marker[2].strip()):
            ranges.append((fence_start, position + len(line)))
            fence = None
        position += len(line)
    if fence is not None:
        ranges.append((fence_start, len(text)))
    return ranges


def _inline_code_ranges(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _INLINE_CODE.finditer(text)]


_INDENT = re.compile(r'^(?: {4}|\t)')


def _indented_code_ranges(text: str, fenced: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """缩进代码块（四空格/Tab）的字符区间；``fenced`` 是已识别的围栏区间。

    成块条件沿 CommonMark 的核心判据：块的上一行是空行或文首（段落正文的缩进
    续行不算代码）；块内允许空行，块尾取最后一个缩进行（尾随空行不进区间）。
    与围栏区间相交的候选整段放弃（保守：围栏口径优先）。
    """
    lines = text.splitlines(keepends=True)
    positions: list[int] = []
    position = 0
    for line in lines:
        positions.append(position)
        position += len(line)

    def _in_fenced(start: int, end: int) -> bool:
        return any(s < end and start < e for s, e in fenced)

    ranges: list[tuple[int, int]] = []
    index = 0
    total = len(lines)
    while index < total:
        if not _INDENT.match(lines[index]):
            index += 1
            continue
        prev_blank = index == 0 or not lines[index - 1].strip()
        start = positions[index]
        if not prev_blank or _in_fenced(start, positions[index] + len(lines[index])):
            index += 1
            continue
        cursor = index
        last_content = index
        while cursor < total:
            current = lines[cursor]
            if _INDENT.match(current):
                last_content = cursor
                cursor += 1
            elif not current.strip():
                cursor += 1  # 可能是块内空行，先看后面还有没有缩进行
            else:
                break
        end = positions[last_content] + len(lines[last_content])
        if not _in_fenced(start, end):
            ranges.append((start, end))
        index = max(cursor, index + 1)
    return ranges


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """围栏代码块、缩进代码块与行内代码的字符区间（动作段/URL 不碰这些地方）。"""
    fenced = _fenced_ranges(text)
    return fenced + _indented_code_ranges(text, fenced) + _inline_code_ranges(text)


def _link_ranges(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """单遍扫描出链接目标与链接标签区间。

    返回 ``(targets, labels)``：

    - ``targets``：``](...)`` 从 ``]`` 到配对 ``)`` 的区间；目标内成对括号
      （``a_(b)``）靠同一遍的括号栈配对，目标不跨行，超长目标按防御上限丢弃。
    - ``labels``：``[`` 到 ``](`` 的区间，供 R1 的裸 URL 包裹跳过标签文本。

    每个 ``]`` 只弹栈一次，每个括号只入栈/出栈一次，总体 O(n)。
    """
    targets: list[tuple[int, int]] = []
    labels: list[tuple[int, int]] = []
    brackets: list[int] = []
    parens: list[int] = []
    for index, char in enumerate(text):
        if char == '[':
            brackets.append(index)
        elif char == ']':
            if not brackets:
                continue
            label_start = brackets.pop()
            if index + 1 < len(text) and text[index + 1] == '(':
                labels.append((label_start, index + 2))
        elif char == '(':
            parens.append(index)
        elif char == ')':
            if not parens:
                continue
            opening = parens.pop()
            # 只有紧跟在 ']' 后面的 '(' 才是链接目标起点；目标内的嵌套括号
            # 也会入栈，但它们的起点前不是 ']'，不会被误认成新目标。
            if opening > 0 and text[opening - 1] == ']':
                start = opening - 1
                end = index + 1
                if end - start <= _LINK_TARGET_MAX_CHARS:
                    targets.append((start, end))
        elif char == '\n':
            # 链接目标不跨行：清空未闭合的括号，避免跨行误配。
            parens.clear()
    targets.sort()
    labels.sort()
    return targets, labels


def _link_target_ranges(text: str) -> list[tuple[int, int]]:
    """Markdown 链接目标 ``](...)`` 的字符区间。"""
    return _link_ranges(text)[0]


def _trim_url(url: str) -> str:
    """剪掉 URL 末尾不属于链接的字符。

    右括号按配平决定：URL 内 ``(`` 多于 ``)`` 时，末尾这个 ``)`` 才属于链接；
    否则剪掉。句末标点、强调符、代码符一律剪掉。只处理末尾，中间的括号保持
    原样；括号计数只做一次，避免长尾括号退化成 O(n²)。
    """
    end = len(url)
    opens = url.count('(')
    closes = url.count(')')
    while end:
        last = url[end - 1]
        if last in _URL_TRAILING:
            end -= 1
        elif last == ')' and closes > opens:
            closes -= 1
            end -= 1
        else:
            break
    return url[:end]


def _url_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _URL.finditer(text):
        url = _trim_url(match.group())
        if url:
            ranges.append((match.start(), match.start() + len(url)))
    return ranges


def _autolink_bare_urls(text: str) -> str:
    """把所有裸 URL 包成 ``<...>``，交给 md4c 当显式 autolink 解析。

    Qt/md4c 的自动链接在紧跟中文、全角标点、query 里的 ``%`` 或路径含中文时
    会静默失败（用户看到纯文本、点不动），所以不再依赖它：``_URL`` 命中的
    裸 URL 一律显式包裹。``www.`` 开头的裸域名补 ``https://`` 作为 href。

    受保护区（代码区间 / 链接目标 / 链接标签 / 已在 ``<...>`` 内）跳过；区间
    判断用排序 + 双指针，不做两两比较。返回用于解析的新文本，原文本不被修改
    （落盘原文由调用方保证）。
    """
    targets, labels = _link_ranges(text)
    protected = _code_ranges(text) + targets + labels
    matches = list(_URL.finditer(text))
    if not matches:
        return text
    spans: list[tuple[int, int]] = []
    candidates: list[tuple[int, int, str]] = []
    for match in matches:
        url = _trim_url(match.group())
        if not url:
            continue
        start = match.start()
        end = start + len(url)
        spans.append((start, end))
        candidates.append((start, end, url))
    if not spans:
        return text
    blocked = _intersect_flags(spans, protected)
    edits: list[tuple[int, int, str]] = []
    for (start, end, url), is_blocked in zip(candidates, blocked):
        if is_blocked:
            continue
        if (start > 0 and text[start - 1] == '<'
                and end < len(text) and text[end] == '>'):
            continue  # 已经是 <url> 形式，绝不二次包裹
        if not url.lower().startswith(('http://', 'https://')):
            url = 'https://' + url
        edits.append((start, end, url))
    if not edits:
        return text
    chunks: list[str] = []
    last = 0
    for start, end, url in edits:
        chunks.append(text[last:start])
        chunks.append('<' + url + '>')
        last = end
    chunks.append(text[last:])
    return ''.join(chunks)


def _action_candidates(text: str) -> list[tuple[int, int]]:
    """单遍扫描全文，配对括号并返回动作段候选（已做一层嵌套取舍）。

    全角 ``（）`` 与半角 ``()`` 各自一个栈，不跨类型配对。每遇到开括号把
    ``[start, has_child, has_grandchild]`` 压栈；遇到闭括号弹出本类型最内层
    未闭合的开括号并产出区间，同时把「有孩子 / 有孙子」记到父帧上。空括号
    （``()``）不算动作段，也不参与嵌套层级。

    取区间的口径与旧实现一致：根区间没有孙层时取根（允许一层嵌套）；有孙层
    时只取没有子括号的叶子（最内层）。每个字符只访问一次，总体 O(n)。
    """
    stacks: dict[str, list[list]] = {'（': [], '(': []}
    closers = {'）': '（', ')': '('}
    candidates: list[tuple[int, int]] = []
    for index, char in enumerate(text):
        if char in stacks:
            stacks[char].append([index, False, False])
            continue
        opening = closers.get(char)
        if opening is None:
            continue
        stack = stacks[opening]
        if not stack:
            continue
        start, has_child, has_grandchild = stack.pop()
        end = index + 1
        if end - start <= 2:  # 空括号不是动作段，也不能算父区间的子层
            continue
        if stack:
            parent = stack[-1]
            parent[1] = True
            if has_child:
                parent[2] = True
        elif not has_grandchild:
            # 根区间没有孙层：整段作为动作段（一层嵌套在外层里）。
            candidates.append((start, end))
        if not has_child and stack:
            # 深层嵌套里的叶子：外层不取时由它出面，交给后面的有效性过滤与
            # 不相交选择去重。
            candidates.append((start, end))
    return candidates


def _partial_inline_flags(
    candidates: list[tuple[int, int]], inline: list[tuple[int, int]]
) -> list[bool]:
    """判断候选区间是否与行内代码**部分**相交。

    行内代码完整落在候选区间内（动作段包住 ``x``）不算部分相交；只要有任一端点
    落在代码区间内部就跳过。端点集合排序后与行内区间做双指针扫描，O(k log k + m)，
    不做候选 × 代码的两两比较。
    """
    if not candidates or not inline:
        return [False] * len(candidates)
    endpoints: list[tuple[int, int]] = []
    for index, (start, end) in enumerate(candidates):
        endpoints.append((start, index))
        endpoints.append((end, index))
    endpoints.sort()
    ordered_inline = sorted(inline)
    flags = [False] * len(candidates)
    pointer = 0
    for position, index in endpoints:
        while pointer < len(ordered_inline) and ordered_inline[pointer][1] <= position:
            pointer += 1
        if pointer < len(ordered_inline):
            start, end = ordered_inline[pointer]
            if start < position < end:
                flags[index] = True
    return flags


def _action_ranges(text: str) -> list[tuple[int, int]]:
    """动作段区间：先按有效性过滤候选，再裁成两两不相交。

    动作段内含受保护区（URL/代码/链接目标）时整段不渲染为动作段，属已知限制
    （4.5 评估）。有效性判断全部走排序 + 双指针：

    - 围栏/链接目标/裸 URL：任何相交都跳过；
    - 行内代码：部分相交跳过，整段动作完整包住代码时保留（R5 要还原等宽样式）；
    - 最后才做不相交选择。外层因 URL 失效时内层候选仍留在流里，于是回退到
      内层（R7），而不是把内层也提前吃掉。
    """
    candidates = _action_candidates(text)
    if not candidates:
        return []
    # F3：交叉嵌套（`(a （b) c）`）会让两种括号的区间相交，而 tokenize 要求
    # 区间有序且两两不相交。按 (start, -end) 排序后单遍扫描，先出现且更长者胜。
    candidates.sort(key=lambda span: (span[0], -span[1]))
    fenced = _fenced_ranges(text)
    protected = (fenced + _indented_code_ranges(text, fenced)
                 + _link_target_ranges(text) + _url_ranges(text))
    blocked = _intersect_flags(candidates, protected)
    partial = _partial_inline_flags(candidates, _inline_code_ranges(text))
    spans: list[tuple[int, int]] = []
    last_end = -1
    for (start, end), is_blocked, is_partial in zip(candidates, blocked, partial):
        if is_blocked or is_partial:
            continue
        if start >= last_end:
            spans.append((start, end))
            last_end = end
    return spans


def _tokenize_actions(text: str) -> tuple[str, list[tuple[str, str]]]:
    """把动作段换成占位 token，交给 Markdown 解析后再还原成带样式的富文本。

    不能直接把 ``<span>`` 塞进 Markdown：``MarkdownNoHTML`` 会把它当字面文本。
    先换 token 还让跨行动作段作为一个整体（不会在段落切分时被拆散）。
    """
    spans = _action_ranges(text)
    # tokenize 的前置条件：区间已排序且两两不相交（_action_ranges 已显式过滤）。
    assert all(left[1] <= right[0] for left, right in zip(spans, spans[1:]))
    if not spans:
        return text, []
    prefix = '\u2063AR'
    while prefix in text:  # 用户正文恰好含 token 前缀时继续加长，绝不误还原
        prefix += '\u2063'
    chunks: list[str] = []
    replacements: list[tuple[str, str]] = []
    last = 0
    for index, (start, end) in enumerate(spans):
        token = f'{prefix}{index}\u2063'
        chunks.append(text[last:start])
        chunks.append(token)
        replacements.append((token, text[start:end]))
        last = end
    chunks.append(text[last:])
    return ''.join(chunks), replacements


_ACTION_INLINE = re.compile(r'(`[^`\n]*`|\*\*[^*\n]+\*\*)')


def _render_action_inline(text: str) -> str:
    """动作段内的最小行内解析：`` `x` `` → 等宽 ``<code>``，``**x**`` → ``<strong>``。

    先整体 escape 再认这两个标记，其余字符（包括 HTML）仍按字面显示。
    """
    escaped = html_mod.escape(text, quote=False)
    chunks: list[str] = []
    last = 0
    for match in _ACTION_INLINE.finditer(escaped):
        chunks.append(escaped[last:match.start()])
        token = match.group()
        if token.startswith('`'):
            chunks.append(f'<code style="{_MONO_STYLED}">{token[1:-1]}</code>')
        else:
            chunks.append(f'<strong>{token[2:-2]}</strong>')
        last = match.end()
    chunks.append(escaped[last:])
    return ''.join(chunks).replace('\n', '<br />')


def _restore_actions(html: str, replacements: list[tuple[str, str]]) -> str:
    """单遍还原所有动作 token，避免逐个 ``str.replace`` 退化成 O(k·n)。"""
    if not replacements:
        return html
    mapping = dict(replacements)
    # token 形如 prefix + 序号 + "\u2063"；prefix 由 _tokenize_actions 保证
    # 不在正文里出现，因此这个模式只会命中原样保留的 token。
    prefix = replacements[0][0][:-1].rstrip('0123456789')
    token_pattern = re.compile(re.escape(prefix) + r'\d+\u2063')

    def render(match: re.Match[str]) -> str:
        token = match.group()
        return f'<span style="{_ACTION_STYLE}">{_render_action_inline(mapping[token])}</span>'

    return token_pattern.sub(render, html)


def preserve_line_breaks(text: str) -> str:
    """段落内的单个换行改成硬换行（行尾两个空格）；围栏代码块内原样保留。

    另外：表格紧跟在文字行后面时补一个空行——md4c 不允许表格打断段落，而模型常写
    「如下：」换行就直接开始画表。
    """
    lines = str(text).split('\n')
    out = []
    fence: tuple[str, int] | None = None
    for i, line in enumerate(lines):
        marker = _fence_marker(line)
        if fence is None:
            if marker is not None:
                fence = (marker[0], marker[1])
                out.append(line)
                continue
        else:
            if (marker is not None and marker[0] == fence[0]
                    and marker[1] >= fence[1] and not marker[2].strip()):
                fence = None
            out.append(line)
            continue
        if _is_table_header(lines, i) and out and out[-1].strip() and '|' not in out[-1]:
            out.append('')
        nxt = lines[i + 1] if i + 1 < len(lines) else ''
        if (line.strip() and nxt.strip() and _fence_marker(nxt) is None
                and not _is_table_header(lines, i + 1)
                and not line.endswith('  ') and not line.endswith('\\')):
            line += '  '
        out.append(line)
    return '\n'.join(out)


def markdown_to_html(text: str, *, link_color: str = '#3994ff') -> str:
    # 裸 URL 先包成 <...> 再解析：Qt 的自动链接在中文/全角标点后不生效，不包会断链。
    source = _autolink_bare_urls(str(text))
    source, actions = _tokenize_actions(source)
    doc = QTextDocument()
    doc.setMarkdown(preserve_line_breaks(source), _features())
    html = doc.toHtml()
    head, sep, body = html.partition('<body')
    body = _BODY_STYLE.sub('<body>', sep + body, count=1)
    # 去掉文档级字体后，正文里剩下的 font-family 只来自代码片段（Qt 按平台写入不同的
    # 等宽族名），统一换成等宽字体 + 浅底色
    html = head + _FONT_DECL.sub(_MONO_STYLED, body)
    html = _HEADING_SIZE.sub(lambda m: f'font-size:{_HEADING_MAP[m.group(1)]};', html)
    html = _LINK_COLOR.sub(lambda m: f'{m.group(1)}<span style=" color:{link_color};">', html)
    return _restore_actions(html, actions)
