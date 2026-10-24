# -*- coding: utf-8 -*-
"""「网络信息」弹窗的数据层（零 Qt；联网面全部可注入）。

弹窗**打开时取一次**（约 1KB，不轮询）：公网 IP + IP 属地 + 外网连通性。

数据源（都免 key、失败不静默）：

1. **境内优先**：太平洋电脑网 IP 库（``whois.pconline.com.cn/ipJson.jsp``）。
   境内可达、中文字段（``pro``/``city``/``addr``），响应体是 **GBK**——按 UTF-8
   硬解会乱码，解码顺序见 :func:`decode_json_body`。
2. **回退**：ip-api.com（``lang=zh-CN``，中文国家/省份/城市/ISP）。它是海外
   服务，只在境内源失败时用——把用户的出口 IP 交给谁，是有隐私口径的取舍，
   境内源够用时不多给一家。

外网连通性：对 Google 的 204 探测点与 GitHub 各发一次 HEAD，**有响应即达**
（4xx/5xx 也算达：服务器回了响应头就证明链路通）。只读响应头，两个请求合计
约 1KB。

出口口径：属地按**当前出口 IP** 判定，所以开着系统代理 / VPN 时显示的是出口
节点的位置，"在中国：否"也可能是代理造成的——这是 feature 不是 bug（见
``docs/NETWORK-PROXY-AND-VPN-2026-09-22.md``），文案里明说。取数一律走系统
代理（与 TTS / 更新同类，不做逐功能绕过），只有实测证明"代理有害"才改。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

#: 境内源（无 key、中文字段；响应体 GBK）。
PCONLINE_URL = "http://whois.pconline.com.cn/ipJson.jsp?json=true"
#: 回退源（无 key、中文字段；海外服务，仅在境内源失败时使用）。
IP_API_URL = (
    "http://ip-api.com/json/?lang=zh-CN"
    "&fields=status,message,country,countryCode,regionName,city,isp,query"
)
#: 外网连通性探测点（HEAD，只看响应头/状态码）。
REACHABILITY_TARGETS = (
    ("Google", "https://www.google.com/generate_204"),
    ("GitHub", "https://github.com/"),
)
#: 每个源最多读多少字节（正常响应几百字节；限长防"对方换成大文件"）。
MAX_BODY_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0
_USER_AGENT = "dsh-pet-standalone-network-info"

#: 中国大陆省级行政区（用于"源只给了省/市、没给国家码"时的属地判定）。
CN_MAINLAND_REGIONS = frozenset({
    "北京", "天津", "上海", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆",
})
#: 港澳台不算"中国大陆"（判定行的文案写的是大陆，别含糊）。
NON_MAINLAND_REGIONS = frozenset({"香港", "澳门", "台湾"})


@dataclass(frozen=True)
class GeoInfo:
    """一次属地查询的结果（字段缺省为空串，不编造）。"""

    ip: str = ""
    country: str = ""
    country_code: str = ""
    region: str = ""
    city: str = ""
    isp: str = ""
    source: str = ""

    @property
    def place(self) -> str:
        """「省 市」形式的属地描述（ISP 单独一列展示）。"""
        parts = [part for part in (self.region, self.city) if part]
        if not parts:
            return self.country
        return " ".join(parts)

    @property
    def is_china(self) -> bool | None:
        return is_china_geo(self.country, self.country_code, self.region)


def is_china_geo(country: str = "", country_code: str = "", region: str = "") -> bool | None:
    """属地是否在中国大陆；判不出来时返回 None（界面显示"判定不了"，不猜）。"""
    code = str(country_code or "").strip().upper()
    if code:
        return code == "CN"
    name = str(country or "").strip()
    if name:
        lowered = name.lower()
        if "中国" in name or lowered in {"china", "cn"} or "china" in lowered:
            return True
        return False
    normalized = _normalize_region(region)
    if not normalized:
        return None
    if normalized in NON_MAINLAND_REGIONS:
        return False
    return normalized in CN_MAINLAND_REGIONS


def _normalize_region(region: str) -> str:
    """「广东省」/「广东深圳」→「广东」；**认不出来返回空串**（判定交给调用方按
    "判不了"处理，不硬猜）。港澳台按各自的名字原样返回，由调用方归为非大陆。"""
    text = str(region or "").strip()
    if not text:
        return ""
    for name in CN_MAINLAND_REGIONS | NON_MAINLAND_REGIONS:
        if text.startswith(name):
            return name
    return ""


def decode_json_body(raw: bytes) -> dict:
    """响应体 → dict。编码按 UTF-8 → GBK(gb18030) 依次尝试。

    境内源（pconline）声明 GBK；用 UTF-8 硬解会 UnicodeDecodeError（直接判失败）
    或解出乱码（更糟：看着成功，显示成问号）。有些源还会在 JSON 外面套一层说明/
    回调，故解析失败时再退一步取首个 ``{`` 到末个 ``}`` 的子串。
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("响应体不是字节")
    text = ""
    for encoding in ("utf-8", "gb18030"):
        try:
            text = bytes(raw).decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("响应体编码无法识别")
    candidate = text.strip()
    try:
        data = json.loads(candidate)
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("响应不是 JSON")
        data = json.loads(candidate[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("响应 JSON 不是对象")
    return data


def parse_pconline(payload: dict) -> GeoInfo:
    """太平洋 IP 库的字段映射；缺 IP 视为失败（调用方走下一个源）。"""
    ip = str(payload.get("ip") or "").strip()
    if not ip:
        raise ValueError("响应里没有 IP")
    region = str(payload.get("pro") or "").strip()
    city = str(payload.get("city") or "").strip()
    addr = str(payload.get("addr") or "").strip()
    isp = ""
    if addr:
        # addr 形如「广东省深圳市 电信」：空格后那段是运营商
        tail = addr.split()[-1] if len(addr.split()) > 1 else ""
        if tail and tail not in {region, city}:
            isp = tail
    return GeoInfo(ip=ip, region=region, city=city, isp=isp, source="太平洋 IP 库")


def parse_ip_api(payload: dict) -> GeoInfo:
    """ip-api.com 的字段映射；``status != success`` 视为失败。"""
    if str(payload.get("status") or "").strip().lower() != "success":
        message = str(payload.get("message") or "").strip() or "查询失败"
        raise ValueError(message)
    ip = str(payload.get("query") or "").strip()
    if not ip:
        raise ValueError("响应里没有 IP")
    return GeoInfo(
        ip=ip,
        country=str(payload.get("country") or "").strip(),
        country_code=str(payload.get("countryCode") or "").strip(),
        region=str(payload.get("regionName") or "").strip(),
        city=str(payload.get("city") or "").strip(),
        isp=str(payload.get("isp") or "").strip(),
        source="ip-api.com",
    )


def describe_error(exc: BaseException) -> str:
    """异常 → 用户看得懂的短说明（失败要说清是哪一类，不吞成"失败"）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, TimeoutError):
        return "超时"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            return "超时"
        text = str(reason) or type(reason).__name__
        return f"连接失败（{text[:60]}）"
    if isinstance(exc, ValueError):
        return str(exc)[:60] or "响应无法解析"
    return f"{type(exc).__name__}: {str(exc)[:60]}"


@dataclass(frozen=True)
class ReachabilityResult:
    """一个外网目标的实测结论。``ok`` = 拿到了 HTTP 响应（含 4xx/5xx）。"""

    label: str
    url: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class NetworkInfoSnapshot:
    """弹窗展示用的一次取样结果。"""

    geo: GeoInfo | None
    geo_error: str
    source_errors: tuple[str, ...]
    reachability: tuple[ReachabilityResult, ...]
    elapsed_s: float
    fetched_at_text: str


class NetworkInfoProbe:
    """公网 IP / 属地 / 连通性的取数器（opener 可注入，测试零真实请求）。"""

    def __init__(
        self,
        *,
        opener=None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        clock=time.monotonic,
        stamp=None,
        sources=None,
        targets=REACHABILITY_TARGETS,
    ):
        self._opener = opener if opener is not None else _default_opener()
        self._timeout = float(timeout)
        self._clock = clock
        self._stamp = stamp if stamp is not None else (lambda: time.strftime("%H:%M:%S"))
        self._sources = tuple(sources) if sources is not None else (
            (PCONLINE_URL, parse_pconline),
            (IP_API_URL, parse_ip_api),
        )
        self._targets = tuple(targets)

    # ------------------------------------------------------------ 取数
    def fetch_geo(self, url: str, parser) -> GeoInfo:
        """取一个属地主；失败抛异常（原因由调用方按源记录，不静默）。"""
        request = urllib.request.Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        })
        with self._opener.open(request, timeout=self._timeout) as response:
            raw = response.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("响应过大")
        return parser(decode_json_body(raw))

    def check_reachability(self, label: str, url: str) -> ReachabilityResult:
        """HEAD 一次：拿到响应（含 4xx/5xx）即算可达；超时/连不上算不可达。"""
        request = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
        })
        started = self._clock()
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 0) or 0)
            return ReachabilityResult(
                label, url, True, f"HTTP {status}，{self._clock() - started:.1f}s",
            )
        except urllib.error.HTTPError as exc:
            # 有状态码＝链路通（403/404/405 都说明请求到了服务器）
            return ReachabilityResult(
                label, url, True, f"HTTP {exc.code}，{self._clock() - started:.1f}s",
            )
        except Exception as exc:  # noqa: BLE001 - 逐目标收口，失败如实展示
            return ReachabilityResult(
                label, url, False, f"{describe_error(exc)}，{self._clock() - started:.1f}s",
            )

    def collect(self) -> NetworkInfoSnapshot:
        """完整取一次（串行；最坏 2 个属地源 + 2 个连通性目标各超时一次）。"""
        started = self._clock()
        geo: GeoInfo | None = None
        errors: list[str] = []
        for url, parser in self._sources:
            try:
                geo = self.fetch_geo(url, parser)
                break
            except Exception as exc:  # noqa: BLE001 - 换下一个源，原因累计上报
                errors.append(f"{source_label(url)}：{describe_error(exc)}")
        reachability = tuple(
            self.check_reachability(label, url) for label, url in self._targets
        )
        return NetworkInfoSnapshot(
            geo=geo,
            geo_error="" if geo is not None else "；".join(errors) or "没有可用的属地数据源",
            source_errors=tuple(errors),
            reachability=reachability,
            elapsed_s=self._clock() - started,
            fetched_at_text=str(self._stamp()),
        )


def source_label(url: str) -> str:
    """URL → 展示用的源名（报告与失败行都用它，避免出现原始长 URL）。"""
    text = str(url or "")
    if "pconline" in text:
        return "太平洋 IP 库"
    if "ip-api" in text:
        return "ip-api.com"
    return text.split("/")[2] if "//" in text else text[:40]


def _default_opener():
    """默认 opener：走系统代理（与 TTS / 更新同口径，见模块 docstring）。"""
    return urllib.request.build_opener()


# ---------------------------------------------------------------- 展示文案（纯）
def ip_text(snapshot: NetworkInfoSnapshot) -> str:
    if snapshot.geo is None or not snapshot.geo.ip:
        return "获取失败"
    return snapshot.geo.ip


def place_text(snapshot: NetworkInfoSnapshot) -> str:
    if snapshot.geo is None:
        return "获取失败"
    place = snapshot.geo.place
    isp = snapshot.geo.isp
    if place and isp:
        return f"{place}（{isp}）"
    return place or isp or "获取失败"


def china_text(snapshot: NetworkInfoSnapshot) -> str:
    if snapshot.geo is None:
        return "获取失败"
    verdict = snapshot.geo.is_china
    if verdict is None:
        return "判定不了"
    return "是（中国大陆）" if verdict else "否（属地不在中国大陆）"


def source_text(snapshot: NetworkInfoSnapshot) -> str:
    if snapshot.geo is None:
        return "无（属地获取失败）"
    return snapshot.geo.source or "未知源"


def source_note(snapshot: NetworkInfoSnapshot) -> str:
    """部分源失败时的诊断（整体成功但有源回退）；无失败 / 整体失败时空串。

    整体失败的原因由 :func:`error_text` 单独给出，这里只补"用了哪个回退源、
    前一个源为什么没用上"——用户排查代理问题时看得到。
    """
    if snapshot.geo is None or not snapshot.source_errors:
        return ""
    return "（前序源失败：" + "；".join(snapshot.source_errors) + "）"


def reachability_text(snapshot: NetworkInfoSnapshot) -> str:
    results = snapshot.reachability
    if not results:
        return "未检测"
    ok_count = sum(1 for item in results if item.ok)
    if ok_count == len(results):
        return "均可访问"
    if ok_count == 0:
        return "均不可访问"
    return f"{ok_count}/{len(results)} 可访问"


def reachability_detail(snapshot: NetworkInfoSnapshot) -> str:
    return "\n".join(
        f"· {item.label}：{'可访问' if item.ok else '不可访问'}（{item.detail}）"
        for item in snapshot.reachability
    )


def error_text(snapshot: NetworkInfoSnapshot) -> str:
    """失败原因汇总（成功时为空串）。界面上一行小字展示，不静默。"""
    if snapshot.geo is not None:
        return ""
    return f"属地获取失败：{snapshot.geo_error}"
