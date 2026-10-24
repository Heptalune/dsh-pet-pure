"""联网搜索：搜索引擎适配 + 网页正文抓取（纯 urllib，无第三方依赖）。

DeepSeek 等 OpenAI 兼容 API 本身不带联网能力，providers.py 在正式回答前
先让模型（非思考模式、工具调用）决定要不要搜、搜什么，由本模块执行搜索/读网页，
再把结果作为上下文交给正式回答。

引擎：
- baidu：免 Key，抓取百度网页结果（默认；国内直连可用）
- duckduckgo：免 Key，抓取 html.duckduckgo.com（国内网络通常需要代理）
  两个免 Key 引擎互为兜底；频繁请求会被要求人机验证，此时如实报错，不尝试绕过。
- bocha：博查 Web Search API（需 Key，国内可用）
- tavily：Tavily Search API（需 Key）
"""
from __future__ import annotations

import html as _html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .providers import _make_ssl_context as make_ssl_context, build_browser_headers

ENGINE_LABELS = {
    'baidu': '百度（免 Key）',
    'duckduckgo': 'DuckDuckGo（免 Key）',
    'bocha': '博查 Bocha（需 API Key）',
    'tavily': 'Tavily（需 API Key）',
}

_HTML_ACCEPT = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
_MAX_PAGE_BYTES = 2 * 1024 * 1024


class WebSearchError(RuntimeError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ''


def _clean_text(fragment: str) -> str:
    fragment = re.sub(r'<(script|style|noscript|svg)\b.*?</\1\s*>', ' ', fragment, flags=re.S | re.I)
    fragment = re.sub(r'<[^>]+>', ' ', fragment)
    return ' '.join(_html.unescape(fragment).split())


def _http_get(url: str, *, timeout: float, verify_ssl: bool, accept: str = _HTML_ACCEPT, max_bytes: int = _MAX_PAGE_BYTES):
    req = urllib.request.Request(url, headers=build_browser_headers({'Accept': accept}), method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=make_ssl_context(verify_ssl)) as resp:
            raw = resp.read(max_bytes)
            charset = resp.headers.get_content_charset() or ''
            ctype = resp.headers.get('Content-Type', '')
    except urllib.error.HTTPError as exc:
        raise WebSearchError(f'HTTP {exc.code}') from exc
    except (urllib.error.URLError, OSError) as exc:
        raise WebSearchError(f'网络连接失败：{getattr(exc, "reason", exc)}') from exc
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096], re.I)
        charset = m.group(1).decode('ascii', 'ignore') if m else 'utf-8'
    try:
        text = raw.decode(charset, 'replace')
    except LookupError:
        text = raw.decode('utf-8', 'replace')
    return text, ctype


def _http_post_json(url: str, payload: dict, headers: dict, *, timeout: float, verify_ssl: bool) -> dict:
    hdrs = build_browser_headers({'Content-Type': 'application/json', 'Accept': 'application/json'})
    hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=hdrs, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=make_ssl_context(verify_ssl)) as resp:
            return json.loads(resp.read(_MAX_PAGE_BYTES).decode('utf-8', 'replace'))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1024).decode('utf-8', 'replace')
        if exc.code in (401, 403):
            raise WebSearchError(f'搜索 API Key 无效或无权限（HTTP {exc.code}）') from exc
        raise WebSearchError(f'HTTP {exc.code}：{" ".join(detail.split())[:200]}') from exc
    except (urllib.error.URLError, OSError) as exc:
        raise WebSearchError(f'网络连接失败：{getattr(exc, "reason", exc)}') from exc
    except ValueError as exc:
        raise WebSearchError('搜索服务返回了无效 JSON') from exc


# ------------------------------------------------------------------ 解析器（纯函数，便于测试）

def parse_duckduckgo_html(page: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    for chunk in re.split(r'<div class="result results_links', page)[1:]:
        if 'result--ad' in chunk[:300]:
            continue
        m = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        href = _html.unescape(m.group(1))
        if href.startswith('//'):
            href = 'https:' + href
        target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get('uddg', [href])[0]
        if 'duckduckgo.com/y.js' in target:
            continue
        s = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', chunk, re.S)
        results.append(SearchResult(_clean_text(m.group(2)), target, _clean_text(s.group(1)) if s else ''))
        if len(results) >= limit:
            break
    return results


def parse_baidu_html(page: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for chunk in re.split(r'(?=<div[^>]*class="result(?:-op)? c-container)', page)[1:]:
        mu = re.search(r'\bmu="([^"]*)"', chunk[:2000])
        h3 = re.search(r'<h3[^>]*>(.*?)</h3>', chunk, re.S)
        if not mu or not h3:
            continue
        url = _html.unescape(mu.group(1)).strip()
        host = urllib.parse.urlparse(url).netloc
        if (not url.startswith('http') or 'nourl' in url or url in seen
                or (host.endswith('baidu.com') and ('/s?' in url or 'recommend' in host))):
            continue
        title = _clean_text(h3.group(1))
        snippet = _clean_text(chunk[h3.end():h3.end() + 8000])
        snippet = re.split(r'"\}|"clamp"|\{"', snippet)[0].strip()[:300]
        if not title:
            continue
        seen.add(url)
        results.append(SearchResult(title, url, snippet))
        if len(results) >= limit:
            break
    return results


def html_to_text(page: str) -> str:
    """粗粒度正文提取：去脚本/样式/导航，块级标签转换行。"""
    title_m = re.search(r'<title[^>]*>(.*?)</title>', page, re.S | re.I)
    body = re.sub(r'<(script|style|noscript|svg|nav|footer|header|form|iframe)\b.*?</\1\s*>', ' ', page, flags=re.S | re.I)
    body = re.sub(r'<!--.*?-->', ' ', body, flags=re.S)
    body = re.sub(r'<(br|/p|/div|/li|/h[1-6]|/tr|/section|/article)\b[^>]*>', '\n', body, flags=re.I)
    body = _html.unescape(re.sub(r'<[^>]+>', ' ', body))
    lines = [' '.join(line.split()) for line in body.splitlines()]
    text = '\n'.join(line for line in lines if len(line) > 1)
    title = _clean_text(title_m.group(1)) if title_m else ''
    return (f'标题：{title}\n' if title else '') + text


# ------------------------------------------------------------------ 对外入口

# 免 Key 引擎靠抓网页，频繁请求会被要求人机验证（不会也不应自动绕过）：
# 一个失败时自动换另一个兜底，两个都失败才报错。
_FREE_FALLBACK = {'baidu': 'duckduckgo', 'duckduckgo': 'baidu'}
_CAPTCHA_HINT = '，请稍后再试；需要稳定联网可在 AI 设置里改用博查 / Tavily'


def search(query: str, settings, *, timeout: float = 15.0, verify_ssl: bool = True) -> list[SearchResult]:
    query = ' '.join(str(query or '').split())
    if not query:
        raise WebSearchError('搜索词为空')
    engine = getattr(settings, 'engine', 'baidu')
    limit = max(1, min(10, int(getattr(settings, 'max_results', 5) or 5)))
    if engine not in _FREE_FALLBACK:
        return _search_engine(engine, query, limit, settings, timeout, verify_ssl)
    try:
        return _search_engine(engine, query, limit, settings, timeout, verify_ssl)
    except WebSearchError as first:
        try:
            return _search_engine(_FREE_FALLBACK[engine], query, limit, settings, timeout, verify_ssl)
        except WebSearchError as second:
            raise WebSearchError(f'{first}；备用引擎也失败：{second}') from second


def _search_engine(engine, query, limit, settings, timeout, verify_ssl) -> list[SearchResult]:
    if engine == 'baidu':
        page, _ = _http_get('https://www.baidu.com/s?ie=utf-8&wd=' + urllib.parse.quote(query), timeout=timeout, verify_ssl=verify_ssl)
        if 'wappass.baidu.com' in page[:5000] or '百度安全验证' in page[:5000]:
            raise WebSearchError('百度要求人机验证' + _CAPTCHA_HINT)
        return parse_baidu_html(page, limit)
    if engine in ('bocha', 'tavily'):
        key = settings.resolve_api_key() if hasattr(settings, 'resolve_api_key') else getattr(settings, 'api_key', '')
        if not key:
            raise WebSearchError(f'{ENGINE_LABELS.get(engine, engine)} 未配置 API Key（AI 设置 → 深度思考与联网）')
        if engine == 'bocha':
            data = _http_post_json('https://api.bochaai.com/v1/web-search',
                                   {'query': query, 'summary': True, 'count': limit, 'freshness': 'noLimit'},
                                   {'Authorization': f'Bearer {key}'}, timeout=timeout, verify_ssl=verify_ssl)
            pages = (((data or {}).get('data') or {}).get('webPages') or {}).get('value') or []
            return [SearchResult(str(p.get('name', '')), str(p.get('url', '')), str(p.get('summary') or p.get('snippet') or '')[:600])
                    for p in pages[:limit] if isinstance(p, dict)]
        data = _http_post_json('https://api.tavily.com/search',
                               {'query': query, 'max_results': limit, 'search_depth': 'basic'},
                               {'Authorization': f'Bearer {key}'}, timeout=timeout, verify_ssl=verify_ssl)
        return [SearchResult(str(r.get('title', '')), str(r.get('url', '')), str(r.get('content', ''))[:600])
                for r in (data or {}).get('results', [])[:limit] if isinstance(r, dict)]
    page, _ = _http_get('https://html.duckduckgo.com/html/?q=' + urllib.parse.quote(query), timeout=timeout, verify_ssl=verify_ssl)
    if 'anomaly-modal' in page or 'bots use DuckDuckGo too' in page:
        raise WebSearchError('DuckDuckGo 要求人机验证' + _CAPTCHA_HINT)
    return parse_duckduckgo_html(page, limit)


def fetch_page_text(url: str, *, timeout: float = 15.0, verify_ssl: bool = True, max_chars: int = 5000) -> str:
    url = str(url or '').strip()
    if urllib.parse.urlparse(url).scheme not in ('http', 'https'):
        raise WebSearchError('只支持 http/https 网址')
    page, ctype = _http_get(url, timeout=timeout, verify_ssl=verify_ssl)
    if ctype and 'html' not in ctype and 'text' not in ctype:
        raise WebSearchError(f'不支持的网页类型：{ctype.split(";")[0]}')
    text = html_to_text(page) if '<' in page[:2000] else page
    return text[:max_chars] + ('…（已截断）' if len(text) > max_chars else '')


def format_results(query: str, results: list[SearchResult]) -> str:
    if not results:
        return f'搜索「{query}」没有找到结果。'
    lines = [f'搜索「{query}」的结果：']
    for i, r in enumerate(results, 1):
        lines.append(f'[{i}] {r.title}\n链接：{r.url}\n摘要：{r.snippet or "（无）"}')
    return '\n\n'.join(lines)

