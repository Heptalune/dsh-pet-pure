# -*- coding: utf-8 -*-
"""「网络信息」数据层单测（``pet/network_info.py``）。

opener 全注入（FakeOpener），**不发真实请求**；活体取数由实机验证覆盖。
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from pet import network_info as ni


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self, _size: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeOpener:
    """按 URL 脚本化响应；记录每次请求（方法/超时）。"""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.calls: list[tuple[str, str, float]] = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.calls.append((getattr(request, "method", "GET"), url, float(timeout)))
        result = self.routes.get(url)
        if result is None:
            raise urllib.error.URLError("no route")
        if isinstance(result, Exception):
            raise result
        return result


def _probe(opener, **kwargs) -> ni.NetworkInfoProbe:
    return ni.NetworkInfoProbe(opener=opener, clock=_Clock(), stamp=lambda: "12:00:00", **kwargs)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.1
        return self.now


# ---------------------------------------------------------------- 编码/解析
def test_decode_json_body_handles_gbk_from_domestic_source():
    """境内源声明 GBK：按 UTF-8 硬解会乱码/抛错，解码顺序必须兜到 gb18030。"""
    payload = {"ip": "1.2.3.4", "pro": "广东", "city": "深圳", "addr": "广东省深圳市 电信"}
    raw = json.dumps(payload, ensure_ascii=False).encode("gb18030")
    assert ni.decode_json_body(raw) == payload


def test_decode_json_body_tolerates_wrapper_text():
    raw = b'callback({"ip": "1.2.3.4", "pro": "\xe5\x8c\x97\xe4\xba\xac"})'
    assert ni.decode_json_body(raw)["ip"] == "1.2.3.4"


def test_decode_json_body_rejects_non_json():
    with pytest.raises(ValueError):
        ni.decode_json_body(b"<html>502 Bad Gateway</html>")


def test_decode_json_body_rejects_unreadable_encoding():
    with pytest.raises(ValueError):
        ni.decode_json_body(b"\xff\xfe\xff\xfe\x00\x01")


def test_parse_pconline_maps_fields_and_isp():
    geo = ni.parse_pconline({
        "ip": "1.2.3.4", "pro": "广东", "city": "深圳", "addr": "广东省深圳市 电信",
    })
    assert geo.ip == "1.2.3.4"
    assert geo.place == "广东 深圳"
    assert geo.isp == "电信"
    assert geo.source == "太平洋 IP 库"


def test_parse_pconline_requires_ip():
    with pytest.raises(ValueError):
        ni.parse_pconline({"pro": "广东"})


def test_parse_ip_api_maps_fields_and_rejects_failure_status():
    geo = ni.parse_ip_api({
        "status": "success", "query": "1.2.3.4", "country": "中国",
        "countryCode": "CN", "regionName": "北京", "city": "北京", "isp": "联通",
    })
    assert geo.ip == "1.2.3.4"
    assert geo.country_code == "CN"
    assert geo.place == "北京 北京"
    with pytest.raises(ValueError):
        ni.parse_ip_api({"status": "fail", "message": "reserved range"})


# ---------------------------------------------------------------- 属地判定
@pytest.mark.parametrize("country,code,region,expected", [
    ("中国", "", "", True),
    ("", "CN", "", True),
    ("", "US", "", False),
    ("United States", "", "", False),
    ("", "", "广东省深圳市", True),
    ("", "", "广东", True),
    ("", "", "内蒙古自治区", True),
    ("", "", "香港", False),
    ("", "", "台湾省", False),
    ("", "", "", None),
    ("", "", "某个没听说过的地方", None),
])
def test_is_china_geo(country, code, region, expected):
    assert ni.is_china_geo(country, code, region) is expected


# ---------------------------------------------------------------- 连通性
def test_reachability_treats_http_error_as_reachable():
    """4xx/5xx 也是响应：服务器回了头就证明链路通，别把 403 说成"不可访问"。"""
    opener = FakeOpener({
        "https://www.google.com/generate_204": FakeResponse(b"", 204),
        "https://github.com/": urllib.error.HTTPError(
            "https://github.com/", 403, "Forbidden", {}, io.BytesIO(b"")),
    })
    probe = _probe(opener)
    google = probe.check_reachability(*ni.REACHABILITY_TARGETS[0])
    github = probe.check_reachability(*ni.REACHABILITY_TARGETS[1])
    assert google.ok is True and "204" in google.detail
    assert github.ok is True and "403" in github.detail


@pytest.mark.parametrize("failure,needle", [
    (TimeoutError("timed out"), "超时"),
    (urllib.error.URLError(TimeoutError("timed out")), "超时"),
    (urllib.error.URLError(OSError("connect failed")), "连接失败"),
])
def test_reachability_reports_failures_truthfully(failure, needle):
    opener = FakeOpener({"https://github.com/": failure})
    result = _probe(opener).check_reachability("GitHub", "https://github.com/")
    assert result.ok is False
    assert needle in result.detail


def test_reachability_uses_head_requests():
    """连通性只读响应头（HEAD）：两个目标合计约 1KB，不拉正文。"""
    targets = {url for _label, url in ni.REACHABILITY_TARGETS}
    opener = FakeOpener({
        ni.PCONLINE_URL: FakeResponse(json.dumps({"ip": "1.2.3.4"}).encode("utf-8")),
        **{url: FakeResponse(b"", 204) for url in targets},
    })
    _probe(opener).collect()
    for method, url, _timeout in opener.calls:
        if url in targets:
            assert method == "HEAD", url


# ---------------------------------------------------------------- 取数流程
def test_collect_prefers_domestic_source_and_never_touches_fallback():
    opener = FakeOpener({
        ni.PCONLINE_URL: FakeResponse(
            json.dumps({"ip": "1.2.3.4", "pro": "广东", "city": "深圳", "addr": "广东省深圳市 电信"},
                       ensure_ascii=False).encode("gb18030")),
        **{url: FakeResponse(b"", 204) for _label, url in ni.REACHABILITY_TARGETS},
    })
    snapshot = _probe(opener).collect()
    assert snapshot.geo is not None and snapshot.geo.source == "太平洋 IP 库"
    assert snapshot.geo_error == ""
    urls = [url for _method, url, _timeout in opener.calls]
    assert ni.IP_API_URL not in urls  # 境内源成功就不把出口 IP 交给第二家
    assert snapshot.elapsed_s > 0
    assert snapshot.fetched_at_text == "12:00:00"


def test_collect_falls_back_to_second_source_and_records_first_failure():
    opener = FakeOpener({
        ni.PCONLINE_URL: urllib.error.URLError(OSError("blocked")),
        ni.IP_API_URL: FakeResponse(json.dumps({
            "status": "success", "query": "5.6.7.8", "country": "美国",
            "countryCode": "US", "regionName": "California", "city": "SF", "isp": "ISP",
        }).encode("utf-8")),
        **{url: FakeResponse(b"", 204) for _label, url in ni.REACHABILITY_TARGETS},
    })
    snapshot = _probe(opener).collect()
    assert snapshot.geo is not None and snapshot.geo.country_code == "US"
    assert snapshot.geo_error == ""
    assert len(snapshot.source_errors) == 1
    assert "太平洋 IP 库" in snapshot.source_errors[0]
    assert ni.china_text(snapshot) == "否（属地不在中国大陆）"


def test_collect_reports_failure_when_every_source_fails():
    opener = FakeOpener({
        ni.PCONLINE_URL: urllib.error.URLError(OSError("no route")),
        ni.IP_API_URL: TimeoutError("timed out"),
        **{url: urllib.error.URLError(OSError("no route")) for _label, url in ni.REACHABILITY_TARGETS},
    })
    snapshot = _probe(opener).collect()
    assert snapshot.geo is None
    assert "获取失败" in ni.ip_text(snapshot)
    assert "获取失败" in ni.place_text(snapshot)
    assert "获取失败" in ni.china_text(snapshot)
    assert "属地获取失败" in ni.error_text(snapshot)
    assert ni.reachability_text(snapshot) == "均不可访问"
    assert all(not item.ok for item in snapshot.reachability)


def test_collect_limits_body_size():
    opener = FakeOpener({
        ni.PCONLINE_URL: FakeResponse(b"{" + b"x" * (ni.MAX_BODY_BYTES + 10) + b"}"),
        ni.IP_API_URL: FakeResponse(json.dumps({
            "status": "success", "query": "1.1.1.1", "countryCode": "CN",
        }).encode("utf-8")),
        **{url: FakeResponse(b"", 204) for _label, url in ni.REACHABILITY_TARGETS},
    })
    snapshot = _probe(opener).collect()
    assert snapshot.geo is not None and snapshot.geo.source == "ip-api.com"
    assert "响应过大" in snapshot.source_errors[0]


# ---------------------------------------------------------------- 展示文案
def _snapshot_with(geo, results=None):
    return ni.NetworkInfoSnapshot(
        geo=geo,
        geo_error="",
        source_errors=(),
        reachability=tuple(results or ()),
        elapsed_s=1.2,
        fetched_at_text="12:00:00",
    )


def test_placeholder_texts_never_silently_blank():
    snapshot = _snapshot_with(None, [ni.ReachabilityResult("Google", "u", False, "超时，5.0s")])
    assert ni.ip_text(snapshot) == "获取失败"
    assert ni.china_text(snapshot) == "获取失败"
    assert ni.source_text(snapshot) == "无（属地获取失败）"
    assert ni.reachability_text(snapshot) == "均不可访问"
    assert "Google：不可访问" in ni.reachability_detail(snapshot)


def test_texts_for_partial_reachability_and_unknown_verdict():
    geo = ni.GeoInfo(ip="1.2.3.4", region="某个没听说过的地方", source="太平洋 IP 库")
    snapshot = _snapshot_with(geo, [
        ni.ReachabilityResult("Google", "u1", False, "超时，5.0s"),
        ni.ReachabilityResult("GitHub", "u2", True, "HTTP 200，0.4s"),
    ])
    assert ni.place_text(snapshot) == "某个没听说过的地方"
    assert ni.china_text(snapshot) == "判定不了"
    assert ni.reachability_text(snapshot) == "1/2 可访问"
    assert ni.error_text(snapshot) == ""


def test_source_note_only_for_partial_source_failure():
    """回退源出结果时也要说清"前一个源为什么没用上"（代理排查看得见）。"""
    geo = ni.GeoInfo(ip="1.2.3.4", country_code="CN", source="ip-api.com")
    ok = ni.NetworkInfoSnapshot(
        geo=geo, geo_error="", source_errors=(), reachability=(), elapsed_s=0.5,
        fetched_at_text="12:00:00")
    assert ni.source_note(ok) == ""

    fallback = ni.NetworkInfoSnapshot(
        geo=geo, geo_error="", source_errors=("太平洋 IP 库：连接失败（blocked）",),
        reachability=(), elapsed_s=0.5, fetched_at_text="12:00:00")
    assert "太平洋 IP 库" in ni.source_note(fallback)

    failed = ni.NetworkInfoSnapshot(
        geo=None, geo_error="都失败了", source_errors=("x",), reachability=(),
        elapsed_s=0.5, fetched_at_text="12:00:00")
    assert ni.source_note(failed) == ""  # 整体失败由 error_text 说，不重复


def test_source_label_is_readable():
    assert ni.source_label(ni.PCONLINE_URL) == "太平洋 IP 库"
    assert ni.source_label(ni.IP_API_URL) == "ip-api.com"
    assert ni.source_label("https://example.com/x") == "example.com"
