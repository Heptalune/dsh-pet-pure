from __future__ import annotations
import functools
import json, ssl, threading, urllib.error, urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from .models import ProviderConfig

import re as _re

try:
    import certifi
except Exception:  # 未安装/未打进包时回退系统默认 CA 库
    certifi = None

def _make_ssl_context(verify: bool):
    """按配置构造 SSL 上下文：verify=False 跳过证书校验（本地网关/自签名）；
    verify=True 优先使用 certifi 的 CA 包（PyInstaller 需 --collect-all certifi）。

    构造结果按 ``verify`` 缓存：SSLContext 建好后只读复用是安全的，而
    ``create_default_context(cafile=...)`` 每次新建实测要 ~0.5 s（certifi 加载，
    2026-10-14 本机实测）——截图翻译/视觉问答这种"点一下就等"的短调用不该
    每次都付这笔钱。
    """
    if not verify:
        return ssl._create_unverified_context()
    return _verified_ssl_context()


@functools.lru_cache(maxsize=1)
def _verified_ssl_context():
    try:
        if certifi is not None:
            return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    return ssl.create_default_context()

def _is_cert_verify_error(reason) -> bool:
    text = str(reason).lower()
    return 'ssl' in text or 'certificate' in text or 'tls' in text

_CERT_HINT = '；如为代理/梯子拦截或自签名证书，可在 AI 设置中勾选"跳过 SSL 证书验证"后重试'


def build_browser_headers(extra: dict | None = None) -> dict[str, str]:
    """构造带浏览器特征的请求头，降低 Cloudflare 等 WAF 的机器人误判。

    urllib 默认 User-Agent 是 Python-urllib/3.x，容易被识别为脚本机器人
    （如 Cloudflare error 1010）。这里提供常见浏览器头，业务头经 extra 覆盖。
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }
    if extra:
        headers.update(extra)
    return headers


def test_connection(config, timeout: float = 10.0):
    """发送一个最小的非流式请求验证端点连通性（含 TLS 校验）。
    返回 (ok: bool, message: str)，供设置界面"测试连接"使用，不写入任何状态。"""
    try:
        endpoint = normalize_chat_endpoint(config.base_url, config.chat_path)
        payload = {'model': config.model, 'messages': [{'role': 'user', 'content': 'ping'}], 'max_tokens': 1, 'stream': False}
        headers = build_browser_headers({'Content-Type': 'application/json'})
        if config.api_key: headers['Authorization'] = f'Bearer {config.api_key}'
        req = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=timeout, context=_make_ssl_context(config.verify_ssl)) as resp:
            resp.read(4096)
            return True, f'连接成功（HTTP {resp.status}）'
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode('utf-8', 'replace')
        msg = _safe_error_detail(detail)
        if exc.code in (401, 403): return False, f'认证失败（HTTP {exc.code}）：{msg}'
        return False, f'请求失败（HTTP {exc.code}）：{msg}'
    except urllib.error.URLError as exc:
        reason = exc.reason
        hint = _CERT_HINT if _is_cert_verify_error(reason) else ''
        return False, f'网络连接失败：{reason}{hint}'
    except OSError as exc:
        return False, f'网络请求失败：{exc}'

def normalize_chat_endpoint(base_url,chat_path='/v1/chat/completions'):
    base=str(base_url or '').strip().rstrip('/'); path=str(chat_path or '/v1/chat/completions').strip(); path=path if path.startswith('/') else '/'+path
    if base.endswith('/chat/completions'): return base
    # base 已带版本段（/v1、/v4 等 OpenAI 兼容路径，如智谱 /api/paas/v4）→ 只补 /chat/completions
    if path=='/v1/chat/completions' and _re.search(r'/v\d+$',base): return base+'/chat/completions'
    return base+path

class ProviderError(RuntimeError):
    def __init__(self,message,status=None): self.status=status; super().__init__(f'HTTP {status}: {message}' if status else message)

class SSEParser:
    def __init__(self): self._buffer=b''; self.done=False; self.finish_reason=None
    def feed_events(self,chunk:bytes):
        self._buffer+=chunk
        if len(self._buffer)>1024*1024: raise ProviderError('SSE 缓冲超过 1MB 上限（服务端未按事件分隔）')  # 审查 GLM-M5：防异常 provider 无界堆积
        out=[]
        while True:
            found=None
            for marker in (b'\r\n\r\n',b'\n\n'):
                i=self._buffer.find(marker)
                if i>=0: found=(i,marker); break
            if not found: break
            i,marker=found; event=self._buffer[:i]; self._buffer=self._buffer[i+len(marker):]; data=[]
            for line in event.replace(b'\r\n',b'\n').split(b'\n'):
                if line and not line.startswith(b':') and line.startswith(b'data:'): data.append(line[5:].lstrip().decode('utf-8','replace'))
            if not data: continue
            payload_text='\n'.join(data).strip()
            if not payload_text: continue  # 空 data: 心跳行（部分 OpenAI 兼容服务用作 keepalive），跳过不解析——否则 json.loads('') 中止整条流（审查 DS-L20）
            if payload_text=='[DONE]': self.done=True; continue
            try: payload=json.loads(payload_text)
            except json.JSONDecodeError as exc: raise ProviderError('Provider 返回了无效 JSON') from exc
            if isinstance(payload,dict) and payload.get('error'):
                err=payload['error']; msg=err.get('message','Provider 返回错误') if isinstance(err,dict) else str(err); raise ProviderError(str(msg))
            choices=payload.get('choices',[]) if isinstance(payload,dict) else []
            if choices and isinstance(choices[0],dict):
                delta=choices[0].get('delta',{}) or {}
                if not isinstance(delta,dict): delta={}
                # reasoning_content：DeepSeek/智谱等思考模型的思维链；reasoning：OpenRouter 等网关的同义字段
                reasoning=delta.get('reasoning_content') or delta.get('reasoning')
                event={'content':str(delta.get('content') or ''),'reasoning':reasoning if isinstance(reasoning,str) else '','finish_reason':choices[0].get('finish_reason')}
                if event['finish_reason']: self.finish_reason=event['finish_reason']
                if event['content'] or event['reasoning'] or event['finish_reason']: out.append(event)
        return out
    def feed(self,chunk:bytes):
        """兼容旧接口：只返回正文增量。"""
        return [e['content'] for e in self.feed_events(chunk) if e['content']]

@dataclass(frozen=True)
class StreamEvent:
    """流中的非正文事件：kind='reasoning' 思考过程增量；kind='status' 联网等进度提示。
    正文增量仍以 str 产出，旧调用方（只拼接 str）不受影响。"""
    kind: str
    text: str


_WEEKDAYS='一二三四五六日'

def _today_text():
    now=datetime.now(); return f"{now:%Y-%m-%d} 星期{_WEEKDAYS[now.weekday()]} {now:%H:%M}"

def _sends_thinking_param(config) -> bool:
    """auto 模式下仅对 DeepSeek 官方地址发送 thinking 参数，避免严格校验参数的服务报错。"""
    return getattr(config,'thinking','auto')!='auto' or 'deepseek.com' in str(config.base_url or '').lower()

def thinking_payload(config, *, force_disable=False) -> dict[str,Any]:
    """DeepSeek V4 格式：thinking={'type':enabled/disabled}，reasoning_effort=low/high/max。"""
    mode=getattr(config,'thinking','auto')
    if force_disable:
        return {'thinking':{'type':'disabled'}} if _sends_thinking_param(config) else {}
    if mode=='enabled':
        return {'thinking':{'type':'enabled'},'reasoning_effort':getattr(config,'reasoning_effort','high') or 'high'}
    if mode=='disabled':
        return {'thinking':{'type':'disabled'}}
    return {}

_WEB_TOOLS=[
    {'type':'function','function':{'name':'web_search','description':'用搜索引擎联网搜索，获取最新/实时信息。返回若干条标题、链接和摘要。','parameters':{'type':'object','properties':{'query':{'type':'string','description':'精炼的搜索关键词'}},'required':['query']}}},
    {'type':'function','function':{'name':'fetch_url','description':'读取指定网页的正文内容，用于搜索摘要不够详细时深入阅读。','parameters':{'type':'object','properties':{'url':{'type':'string','description':'要读取的网页地址（http/https）'}},'required':['url']}}},
]
_PLANNER_PROMPT=("你是联网检索助手，当前时间：{today}。请判断：回答用户最后一条消息，是否需要联网获取最新、实时或你不确定的事实信息"
    "（新闻、天气、价格、版本、赛事、政策、具体数据、某个人/产品/事件的现状等）。\n"
    "- 需要：调用 web_search 搜索，关键词要精炼，必要时换角度多搜几次；摘要信息不足时，可调用 fetch_url 阅读最相关的网页。\n"
    "- 不需要（闲聊、常识、写作、翻译、代码、情感陪伴等）：不要调用任何工具，直接回复「无需搜索」。\n"
    "你只负责收集资料，不要直接回答用户的问题。")
_CONTEXT_PROMPT=("【联网搜索结果】（当前时间：{today}；以下资料是刚刚联网获取的）\n\n{notes}\n\n"
    "请结合以上资料回答用户最新的问题：优先采用资料中的最新信息；资料与你已有知识冲突时以资料为准；资料不足以回答时如实说明。"
    "如果引用了资料，在回答末尾用「参考来源」简短列出对应链接。")
_MAX_RESEARCH_ROUNDS=3
_MAX_CALLS_PER_ROUND=4
_MAX_NOTES_CHARS=24000

def _text_of(content) -> str:
    if isinstance(content,str): return content
    if isinstance(content,list):
        parts=[]
        for item in content:
            if isinstance(item,dict) and item.get('type')=='text': parts.append(str(item.get('text','')))
            elif isinstance(item,dict) and item.get('type')=='image_url': parts.append('[图片]')
        return '\n'.join(parts)
    return str(content or '')

def _recent_dialogue(messages, limit=6, max_chars=2000):
    out=[]
    for m in messages:
        if not isinstance(m,dict) or m.get('role') not in ('user','assistant'): continue
        text=_text_of(m.get('content')).strip()
        if text: out.append({'role':m['role'],'content':text[-max_chars:]})
    out=out[-limit:]
    while out and out[0]['role']!='user': out.pop(0)
    return out

def inject_search_context(messages, notes: str):
    """把检索资料并入首条 system 消息（无 system 时插到最前），不改动原列表。"""
    block=_CONTEXT_PROMPT.format(today=_today_text(),notes=notes)
    messages=[dict(m) if isinstance(m,dict) else m for m in messages]
    if messages and isinstance(messages[0],dict) and messages[0].get('role')=='system' and isinstance(messages[0].get('content'),str):
        messages[0]['content']=(messages[0]['content'].rstrip()+'\n\n'+block).strip()
    else:
        messages.insert(0,{'role':'system','content':block})
    return messages


class OpenAICompatibleProvider:
    def _open(self,payload,config,accept):
        endpoint=normalize_chat_endpoint(config.base_url,config.chat_path)
        headers=build_browser_headers({'Content-Type':'application/json','Accept':accept})
        if config.api_key: headers['Authorization']=f'Bearer {config.api_key}'
        req=urllib.request.Request(endpoint,data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),headers=headers,method='POST')
        try: return urllib.request.urlopen(req,timeout=config.timeout,context=_make_ssl_context(config.verify_ssl))
        except urllib.error.HTTPError as exc:
            detail=exc.read(2048).decode('utf-8','replace'); raise ProviderError(_safe_error_detail(detail),exc.code) from exc
        except urllib.error.URLError as exc:
            reason=exc.reason; hint=_CERT_HINT if _is_cert_verify_error(reason) else ''
            raise ProviderError(f'网络连接失败：{reason}{hint}') from exc
        except OSError as exc: raise ProviderError(f'网络请求失败：{exc}') from exc

    def stream(self,messages:list[dict[str,Any]],config:ProviderConfig,cancel_event:threading.Event,response_holder:list|None=None)->Iterator[str]:
        web=getattr(config,'web_search',None)
        if web is not None and getattr(web,'enabled',False) and not cancel_event.is_set():
            notes=yield from self._research(messages,config,cancel_event,response_holder)
            if cancel_event.is_set(): return
            if notes:
                messages=inject_search_context(messages,notes)
                yield StreamEvent('status','已获取联网资料，正在组织回答…')
        yield from self._stream_answer(messages,config,cancel_event,response_holder)

    def _stream_answer(self,messages,config,cancel_event,response_holder):
        payload:dict[str,Any]={'model':config.model,'messages':messages,'stream':True,'temperature':config.temperature,'max_tokens':config.max_tokens}
        payload.update(thinking_payload(config))
        response=self._open(payload,config,'text/event-stream')
        if response_holder is not None:
            response_holder.append(response)
        parser=SSEParser(); got_content=False; got_reasoning=False
        try:
            while not cancel_event.is_set():
                chunk=response.read(4096)
                if not chunk: break
                for event in parser.feed_events(chunk):
                    if cancel_event.is_set(): return
                    if event['reasoning']:
                        got_reasoning=True; yield StreamEvent('reasoning',event['reasoning'])
                    if event['content']:
                        got_content=True; yield event['content']
                if parser.done: break
        finally:
            try: response.close()
            except Exception: pass
        if got_reasoning and not got_content and parser.finish_reason=='length' and not cancel_event.is_set():
            raise ProviderError(f'深度思考用完了「最大输出 Token」（当前 {config.max_tokens}），还没来得及给出回答。请在 AI 设置 → 生成参数里调大（建议 8192 以上）。')

    def _complete_json(self,payload,config,response_holder):
        response=self._open(payload,config,'application/json')
        if response_holder is not None: response_holder.append(response)
        try: raw=response.read(4*1024*1024)
        finally:
            try: response.close()
            except Exception: pass
        try: data=json.loads(raw.decode('utf-8','replace'))
        except ValueError as exc: raise ProviderError('Provider 返回了无效 JSON') from exc
        if isinstance(data,dict) and data.get('error'):
            err=data['error']; raise ProviderError(str(err.get('message','Provider 返回错误') if isinstance(err,dict) else err))
        try: return data['choices'][0]['message'] or {}
        except (KeyError,IndexError,TypeError) as exc: raise ProviderError('Provider 返回格式不符合 Chat Completions') from exc

    def _run_tool(self,name,args,web,config,timeout):
        """执行一次工具调用 → (给模型的结果文本, 是否成功, 状态提示列表)。"""
        from . import web_search as ws_mod
        if name=='web_search':
            query=str(args.get('query','')).strip()
            status=[f'🔍 正在搜索：{query}']
            try:
                results=ws_mod.search(query,web,timeout=timeout,verify_ssl=config.verify_ssl)
            except ws_mod.WebSearchError as exc:
                text=f'搜索「{query}」失败：{exc}'; return text,False,status+['　'+text]
            return ws_mod.format_results(query,results),bool(results),status+[f'　找到 {len(results)} 条结果']
        if name=='fetch_url':
            url=str(args.get('url','')).strip()
            status=[f'📄 正在阅读：{url}']
            try:
                text=ws_mod.fetch_page_text(url,timeout=timeout,verify_ssl=config.verify_ssl)
            except ws_mod.WebSearchError as exc:
                text=f'读取网页 {url} 失败：{exc}'; return text,False,status+['　'+text]
            return f'网页 {url} 的内容：\n{text}',True,status
        return f'未知工具：{name}',False,[]

    def _research(self,messages,config,cancel_event,response_holder):
        """检索阶段：非思考模式 + 工具调用，让模型决定搜什么；返回整理好的资料文本（不需要搜索时为空串）。

        单独走一轮非思考请求，而不是在正式回答里挂 tools：DeepSeek 思考模式带 tools 时
        要求回传历史所有轮次的 reasoning_content（缺失即 400），而会话历史并不保存思维链。"""
        web=config.web_search
        dialogue=_recent_dialogue(messages)
        if not dialogue: return ''
        tools=_WEB_TOOLS if getattr(web,'fetch_pages',True) else _WEB_TOOLS[:1]
        planner=[{'role':'system','content':_PLANNER_PROMPT.format(today=_today_text())},*dialogue]
        notes=[]; used=0; searched=False; net_timeout=max(5.0,min(float(config.timeout),20.0))
        yield StreamEvent('status','正在判断是否需要联网…')
        for round_no in range(_MAX_RESEARCH_ROUNDS):
            if cancel_event.is_set(): return ''
            payload={'model':config.model,'messages':planner,'tools':tools,'tool_choice':'auto','stream':False,'max_tokens':1024,'temperature':0.3}
            payload.update(thinking_payload(config,force_disable=True))
            try: msg=self._complete_json(payload,config,response_holder)
            except ProviderError as exc:
                if cancel_event.is_set(): return ''
                if round_no==0:
                    yield StreamEvent('status',f'联网检索不可用（{exc}），将直接回答')
                    return ''
                break
            calls=[c for c in (msg.get('tool_calls') or []) if isinstance(c,dict) and isinstance(c.get('function'),dict)][:_MAX_CALLS_PER_ROUND]
            if not calls: break
            searched=True
            assistant={'role':'assistant','content':msg.get('content') or '','tool_calls':calls}
            if msg.get('reasoning_content'): assistant['reasoning_content']=msg['reasoning_content']
            planner.append(assistant)
            for call in calls:
                if cancel_event.is_set(): return ''
                fn=call['function']
                try: args=json.loads(fn.get('arguments') or '{}')
                except ValueError: args={}
                text,ok,status=self._run_tool(str(fn.get('name','')),args if isinstance(args,dict) else {},web,config,net_timeout)
                for line in status: yield StreamEvent('status',line)
                if ok and used<_MAX_NOTES_CHARS:
                    notes.append(text[:_MAX_NOTES_CHARS-used]); used+=len(notes[-1])
                planner.append({'role':'tool','tool_call_id':str(call.get('id','')),'content':text})
        if not searched:
            yield StreamEvent('status','无需联网，直接回答')
        return '\n\n---\n\n'.join(notes)

def _safe_error_detail(raw):
    try:
        data=json.loads(raw)
        if isinstance(data,dict) and isinstance(data.get('error'),dict): return str(data['error'].get('message','Provider 请求失败'))
    except Exception: pass
    return ' '.join(raw.split())[:300] or 'Provider 请求失败'
