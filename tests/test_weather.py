# -*- coding: utf-8 -*-
"""天气取数纯逻辑（无 Qt、无真实网络）。

覆盖四块领域规则：
1. **城市名容错**（「鹿城区」→「鹿城」）与城市列表操作（去重/上限 12/删除当前城市）；
2. **WMO 天气码 / 风向 / 风力等级**的中文映射（国外源缺中文文案时全靠这三张表）；
3. **源切换与失败口径**：源 A（中国天气网）失败走源 B（open-meteo），都失败抛
   ``WeatherError`` 且消息里两个源都点名——绝不静默；
4. **HTTP 出口可注入**：每个请求都记录 ``use_proxy``，钉住「国内源直连、国外源
   跟随系统代理」的分流决策（依据 docs/NETWORK-PROXY-AND-VPN-2026-09-22.md）。

测试样本取自 2026-10-07 的真机响应（见报告 §源与代理决策），不真发外网请求。
"""

from __future__ import annotations

import json
import time

import pytest

from pet import weather as W

# ---------------------------------------------------------------- 真机样本
CN_SEARCH_鹿城 = (
    '([{"ref":"101210710~zhejiang~鹿城~Lucheng~鹿城~Lucheng~577~325600~LC~浙江"},'
    '{"ref":"101210701009~zhejiang~滨江街道~binjiangjiedao~鹿城~lucheng~0577~325600~LC~浙江"},'
    '{"ref":"10121070101A~zhejiang~温州动物园~Wenzhou Zoo~温州~Wenzhou~null~325000~null~浙江省景点"}])'
)
CN_CURRENT_鹿城 = (
    'var dataSK={"nameen":"lucheng","cityname":"鹿城","city":"101210710","temp":"16.6",'
    '"tempf":"61.9","WD":"北风","wde":"N","WS":"1级","wse":"1km\\/h","SD":"67%","sd":"67%",'
    '"qy":"1019","njd":"30km","time":"03:10","rain":"0","rain24h":"0","aqi":"35",'
    '"aqi_pm25":"35","weather":"晴","weathere":"Sunny","weathercode":"d00",'
    '"limitnumber":"","date":"10月07日(星期三)"}'
)
OM_GEOCODE_北京 = json.dumps(
    {"results": [{"id": 1816670, "name": "北京", "latitude": 39.9075,
                  "longitude": 116.39723, "country_code": "CN"}]},
    ensure_ascii=False,
)
OM_CURRENT_北京 = json.dumps(
    {
        "latitude": 39.9,
        "longitude": 116.4,
        "timezone": "Asia/Shanghai",
        "current": {
            "time": "2026-10-07T03:15",
            "interval": 900,
            "temperature_2m": 9.5,
            "relative_humidity_2m": 78,
            "wind_speed_10m": 5.2,
            "wind_direction_10m": 181,
            "weather_code": 2,
        },
    },
    ensure_ascii=False,
)


class _FakeHttp:
    """可注入 HTTP 出口：按 URL 关键字给响应，并记录每次调用的出口选择。"""

    def __init__(self, routes, *, clock=None, advance: float = 0.0):
        self.routes = routes  # [(关键字, 文本 或 Exception)]
        self.calls: list[dict] = []
        self._clock = clock
        self._advance = advance

    def __call__(self, url, headers, *, use_proxy, timeout):
        self.calls.append({"url": url, "headers": headers, "use_proxy": use_proxy,
                           "timeout": timeout})
        if self._clock is not None and self._advance:
            self._clock.now += self._advance
        for key, value in self.routes:
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"未预期的请求：{url}")

    def urls(self):
        return [call["url"] for call in self.calls]


def _client(http, *, clock=time.monotonic):
    return W.WeatherClient(http=http, clock=clock)


# ---------------------------------------------------------------- 1. 城市名与列表
# M1（终审）：存储层**保留用户输入/选择的原名**，归一化必须幂等——此前每次整表
# 归一化都重新剥一次后缀，「景德镇市」→「景德镇」→「景德」，用户只是改了别的设置
# 城市名就被继续改坏；「新乡」「景德镇」这类真名也会被当成后缀裁掉。
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("鹿城区", "鹿城区"),      # 存储层不动后缀（归一化只做空白/宽窄字符）
        ("  北京市  ", "北京市"),
        ("浙江省", "浙江省"),
        ("鹿城", "鹿城"),
        ("新乡", "新乡"),          # 「乡」是后缀字，但新乡是真名
        ("景德镇", "景德镇"),      # 「镇」是后缀字，但景德镇是真名
        ("景德镇市", "景德镇市"),
        ("", ""),
        (None, ""),
        ("市", "市"),
        ("New York", "New York"),
    ],
)
def test_normalize_city_name_keeps_the_users_original_name(raw, expected):
    assert W.normalize_city_name(raw) == expected


def test_normalize_city_name_is_idempotent_on_every_sample():
    """同一输入两次归一结果必须相等——否则"保存别的设置"会继续侵蚀城市名。"""
    for raw in ("景德镇市", "鹿城区", "新乡", "乌鲁木齐县", "  北京市 ", "New York", ""):
        once = W.normalize_city_name(raw)
        assert W.normalize_city_name(once) == once, raw


def test_suffix_variants_are_query_only_and_never_shorten_storage():
    """后缀变体只作为**查询回退**存在：存储值不受影响，变体本身也不改原值。"""
    assert W.strip_city_suffix("鹿城区") == "鹿城"
    assert W.strip_city_suffix("乌鲁木齐县") == "乌鲁木齐"
    assert W.strip_city_suffix("新乡") == "新"
    assert W.strip_city_suffix("鹿城") == "鹿城"
    assert W.normalize_city_name("新乡") == "新乡"  # 剥后缀没有回写

    assert W.city_name_variants("鹿城区") == ("鹿城区", "鹿城")
    assert W.city_name_variants("新乡") == ("新乡", "新")
    assert W.city_name_variants("鹿城") == ("鹿城",)
    assert W.city_name_variants("") == ()


def test_normalize_city_list_is_idempotent_and_keeps_names_intact():
    assert W.normalize_city_list([" 北京 ", "北京", "", "   ", "上海"]) == ["北京", "上海"]
    assert W.normalize_city_list(None) == []
    assert W.normalize_city_list("北京") == []
    assert W.normalize_city_list(["北京", 42, None]) == ["北京"]
    assert len(W.normalize_city_list([f"城市{i}" for i in range(30)])) == W.MAX_CITIES
    assert W.normalize_city_list(["北" * 40]) == ["北" * W.MAX_CITY_NAME]
    # 真名不被裁短 + 反复归一稳定
    once = W.normalize_city_list(["景德镇市", "新乡", "鹿城区"])
    assert once == ["景德镇市", "新乡", "鹿城区"]
    assert W.normalize_city_list(once) == once


def test_add_city_keeps_the_original_name_and_rejects_duplicates():
    cities, error = W.add_city(["北京"], " 鹿城区 ")
    assert cities == ["北京", "鹿城区"] and error == ""

    cities, error = W.add_city(["北京", "鹿城"], "鹿城")
    assert cities == ["北京", "鹿城"] and error == W.CITY_EXISTS_TEXT

    # 同一城市的不同写法视为同一个：不会同时进列表
    cities, error = W.add_city(["鹿城"], "鹿城")
    assert error == W.CITY_EXISTS_TEXT

    cities, error = W.add_city([], "   ")
    assert cities == [] and error == W.CITY_EMPTY_TEXT

    full = [f"城市{i}" for i in range(W.MAX_CITIES)]
    cities, error = W.add_city(full, "新城市")
    assert cities == full and error == W.CITY_LIMIT_TEXT


def test_remove_city_reassigns_current_when_removing_the_current_one():
    cities, current = W.remove_city(["北京", "上海", "鹿城"], "上海", "上海")
    assert cities == ["北京", "鹿城"] and current == "北京"

    cities, current = W.remove_city(["北京"], "北京", "北京")
    assert cities == [] and current == ""

    # 删的不是当前城市 → 当前不动
    cities, current = W.remove_city(["北京", "上海"], "上海", "北京")
    assert cities == ["北京"] and current == "北京"


def test_current_city_falls_back_to_first_list_entry():
    assert W.current_city({"weather_city": "鹿城", "weather_city_list": ["北京"]}) == "鹿城"
    assert W.current_city({"weather_city": "", "weather_city_list": ["北京", "上海"]}) == "北京"
    assert W.current_city({"weather_city": "  ", "weather_city_list": []}) == ""
    assert W.current_city({}) == ""


# ------------------------------------------------ 城市身份（地区码 / 歧义标签，m1）
def test_city_label_only_disambiguates_when_the_name_is_ambiguous():
    assert W.city_label("北京", "北京", ambiguous=True) == "北京"
    assert W.city_label("北京", "北京市", ambiguous=True) == "北京"
    assert W.city_label("鹿城", "浙江", ambiguous=False) == "鹿城"
    assert W.city_label("朝阳", "辽宁", ambiguous=True) == "朝阳 · 辽宁"
    assert W.city_label("朝阳", "", ambiguous=True) == "朝阳"


def test_base_city_name_strips_the_disambiguation_suffix():
    assert W.base_city_name("朝阳 · 辽宁") == "朝阳"
    assert W.base_city_name("朝阳（辽宁）") == "朝阳"  # 手改配置的老写法也认
    assert W.base_city_name("北京") == "北京"
    assert W.base_city_name("") == ""


def test_city_label_is_stable_under_normalization():
    """标签必须过一遍存储归一化——否则「（」被 NFKC 折成「(」会让地区码表对不上号。"""
    label = W.city_label("朝阳", "辽宁", ambiguous=True)
    assert W.normalize_city_name(label) == label
    assert W.base_city_name(W.normalize_city_name(label)) == "朝阳"


def test_normalize_city_codes_keeps_only_usable_pairs():
    assert W.normalize_city_codes({"朝阳 · 辽宁": "101071201"}) == {"朝阳 · 辽宁": "101071201"}
    assert W.normalize_city_codes(["x"]) == {}
    assert W.normalize_city_codes({42: "1", "北京": 5, "": "1", "上海": ""}) == {}
    assert W.normalize_city_codes({"北京": " 101010100 "}) == {"北京": "101010100"}


def test_city_code_for_reads_the_config_map():
    assert W.city_code_for({"weather_city_codes": {"北京": "101010100"}}, "北京") == "101010100"
    assert W.city_code_for({"weather_city_codes": {"北京": "101010100"}}, "上海") == ""
    assert W.city_code_for({}, "北京") == ""
    assert W.city_code_for({"weather_city_codes": "垃圾"}, "北京") == ""


# ---------------------------------------------------------------- 2. 码表映射
@pytest.mark.parametrize(
    "code,expected",
    [(0, "晴"), (1, "多云"), (2, "局部多云"), (3, "阴"), (45, "雾"), (48, "雾凇"),
     (61, "小雨"), (63, "中雨"), (65, "大雨"), (71, "小雪"), (75, "大雪"),
     (80, "小阵雨"), (95, "雷阵雨"), (96, "雷阵雨伴小冰雹"), (123, "未知"), (None, "未知")],
)
def test_wmo_code_maps_to_chinese(code, expected):
    assert W.wmo_text(code) == expected


@pytest.mark.parametrize(
    "degrees,expected",
    [(0, "北"), (20, "北"), (45, "东北"), (90, "东"), (135, "东南"), (180, "南"),
     (225, "西南"), (270, "西"), (315, "西北"), (359, "北"), (None, "")],
)
def test_wind_direction_degrees_map_to_eight_points(degrees, expected):
    assert W.wind_direction_text(degrees) == expected


@pytest.mark.parametrize(
    "kmh,expected",
    [(0, "0级"), (0.5, "0级"), (3, "1级"), (10, "2级"), (23, "4级"), (45, "6级"),
     (70, "8级"), (95, "10级"), (130, "12级"), (None, "")],
)
def test_wind_speed_maps_to_beaufort_force(kmh, expected):
    assert W.beaufort_text(kmh) == expected


# ---------------------------------------------------------------- 3. 响应解析
def test_parse_search_payload_reads_candidates_and_drops_scenic_spots():
    candidates = W.parse_search_payload(CN_SEARCH_鹿城)
    assert [c.code for c in candidates] == ["101210710", "101210701009"]
    assert candidates[0].name == "鹿城"
    assert candidates[0].region == "浙江"


def test_parse_search_payload_tolerates_empty_and_garbage():
    assert W.parse_search_payload("([])") == []
    assert W.parse_search_payload("") == []
    assert W.parse_search_payload("not json") == []


def test_pick_candidate_prefers_exact_name_match():
    candidates = W.parse_search_payload(CN_SEARCH_鹿城)
    picked = W.pick_candidate(candidates, "鹿城")
    assert picked is not None and picked.code == "101210710"

    # 归一化后的查询同样是精确命中（鹿城区 → 鹿城 由调用方剥后缀，这里再兜一层）
    assert W.pick_candidate(candidates, "鹿城区").code == "101210710"
    assert W.pick_candidate([], "鹿城") is None


def test_exact_only_picking_protects_real_names_from_the_suffix_variant():
    """回退变体必须**精确**命中：否则「新乡」搜不到时会用「新」前缀命中别的城市。"""
    payload = (
        '([{"ref":"101240101~jiangxi~新余~Xinyu~新余~Xinyu~790~338000~XY~江西"},'
        '{"ref":"101180101~henan~新乡~Xinxiang~新乡~Xinxiang~373~453000~XX~河南"}])'
    )
    candidates = W.parse_search_payload(payload)
    assert W.pick_candidate(candidates, "新") is not None        # 宽松匹配会命中
    assert W.pick_candidate(candidates, "新", exact_only=True) is None
    assert W.pick_candidate(candidates, "新乡", exact_only=True).code == "101180101"


def test_candidates_keep_code_and_region_for_identity():
    candidates = W.parse_search_payload(CN_SEARCH_鹿城)
    assert [(c.code, c.name, c.region) for c in candidates] == [
        ("101210710", "鹿城", "浙江"),
        ("101210701009", "滨江街道", "浙江"),
    ]


def test_parse_cn_reading_extracts_current_conditions():
    reading = W.parse_cn_reading(CN_CURRENT_鹿城)
    assert reading.city == "鹿城"
    assert reading.condition == "晴"
    assert reading.temperature == "16.6℃"
    assert reading.wind == "北风 1级"
    assert reading.humidity == "67%"
    assert reading.source == W.SOURCE_CN
    assert reading.observed_at == "03:10"
    assert "鹿城" in reading.bubble_text() and "16.6℃" in reading.bubble_text()


def test_parse_cn_reading_rejects_response_without_temperature():
    with pytest.raises(W.WeatherError):
        W.parse_cn_reading('var dataSK={"cityname":"鹿城"}')
    with pytest.raises(W.WeatherError):
        W.parse_cn_reading("<html>not json</html>")


def test_parse_open_meteo_reading_maps_wmo_wind_and_humidity():
    reading = W.parse_open_meteo_reading(json.loads(OM_CURRENT_北京), "北京")
    assert reading.city == "北京"
    assert reading.source == W.SOURCE_OPEN_METEO
    assert reading.condition == "局部多云"
    assert reading.temperature == "9.5℃"
    assert reading.wind == "南风 1级"
    assert reading.humidity == "78%"


def test_parse_open_meteo_reading_rejects_payload_without_current_block():
    with pytest.raises(W.WeatherError):
        W.parse_open_meteo_reading({"error": True, "reason": "nope"}, "北京")


def test_describe_error_names_the_failure_kind():
    assert W.describe_error(TimeoutError("timed out")) == "请求超时"
    assert W.describe_error(ValueError("bad json")) == "返回数据无效"


# ---------------------------------------------------------------- 4. 源切换
def test_fetch_uses_china_source_directly_and_never_touches_proxy():
    http = _FakeHttp([("toy1.weather.com.cn", CN_SEARCH_鹿城),
                      ("d1.weather.com.cn", CN_CURRENT_鹿城)])
    reading = _client(http).fetch("鹿城")
    assert reading.source == W.SOURCE_CN and reading.temperature == "16.6℃"
    assert [call["use_proxy"] for call in http.calls] == [False, False]
    assert all(call["headers"].get("Referer") for call in http.calls)


def test_fetch_retries_with_suffix_stripped_name_before_falling_back():
    """「鹿城区」在源 A 搜不到（真机实测空数组），必须用「鹿城」重搜。"""
    http = _FakeHttp([("cityname=%E9%B9%BF%E5%9F%8E%E5%8C%BA", "([])"),
                      ("cityname=%E9%B9%BF%E5%9F%8E", CN_SEARCH_鹿城),
                      ("d1.weather.com.cn", CN_CURRENT_鹿城)])
    reading = _client(http).fetch("鹿城区")
    assert reading.city == "鹿城"
    assert reading.source == W.SOURCE_CN


def test_fetch_falls_back_to_open_meteo_through_the_system_proxy():
    http = _FakeHttp([("toy1.weather.com.cn", TimeoutError("timed out")),
                      ("geocoding-api.open-meteo.com", OM_GEOCODE_北京),
                      ("api.open-meteo.com", OM_CURRENT_北京)])
    reading = _client(http).fetch("北京")
    assert reading.source == W.SOURCE_OPEN_METEO
    assert reading.condition == "局部多云"
    # 国内源一律直连、海外源跟随系统代理（真机实测：直连 2s+ 慢于代理，且
    # 只有代理能出网的用户靠这条路兜底）
    assert [(call["url"].split("/")[2], call["use_proxy"]) for call in http.calls] == [
        ("toy1.weather.com.cn", False),
        ("geocoding-api.open-meteo.com", True),
        ("api.open-meteo.com", True),
    ]


def test_fetch_raises_naming_both_sources_when_every_source_fails():
    http = _FakeHttp([("toy1.weather.com.cn", TimeoutError("timed out")),
                      ("geocoding-api.open-meteo.com", OSError("no route"))])
    with pytest.raises(W.WeatherError) as info:
        _client(http).fetch("北京")
    message = str(info.value)
    assert W.SOURCE_CN in message and W.SOURCE_OPEN_METEO in message
    assert "失败" in message
    assert "超时" in message


# ------------------------------------------- 按身份取数（同名城市选哪项中哪项，m1）
CN_CURRENT_朝阳辽宁 = (
    'var dataSK={"nameen":"chaoyang","cityname":"朝阳","city":"101071201","temp":"11.1",'
    '"WD":"南风","WS":"2级","SD":"48%","time":"03:10","weather":"晴"}'
)


def test_known_area_code_is_fetched_directly_without_the_ambiguous_search():
    """已知地区码 → 直接按码取实况：同名城市（朝阳 北京 / 辽宁）各自中自己那条。"""
    http = _FakeHttp([("d1.weather.com.cn/sk_2d/101071201", CN_CURRENT_朝阳辽宁)])
    reading = _client(http).fetch("朝阳 · 辽宁", code="101071201")
    assert reading.city == "朝阳" and reading.temperature == "11.1℃"
    assert http.urls() == ["http://d1.weather.com.cn/sk_2d/101071201.html"]
    assert all(call["use_proxy"] is False for call in http.calls)


def test_known_area_code_falls_back_to_the_name_search_when_the_code_fails():
    http = _FakeHttp([("d1.weather.com.cn/sk_2d/101071201", TimeoutError("timed out")),
                      ("toy1.weather.com.cn", CN_SEARCH_鹿城),
                      ("d1.weather.com.cn/sk_2d/101210710", CN_CURRENT_鹿城)])
    reading = _client(http).fetch("鹿城", code="101071201")
    assert reading.city == "鹿城" and reading.source == W.SOURCE_CN


def test_ambiguous_label_is_queried_by_its_base_name():
    """「朝阳 · 辽宁」上源 B 时不能把标签当城市名去搜。"""
    http = _FakeHttp([("toy1.weather.com.cn", TimeoutError("timed out")),
                      ("geocoding-api.open-meteo.com", OM_GEOCODE_北京),
                      ("api.open-meteo.com", OM_CURRENT_北京)])
    _client(http).fetch("朝阳 · 辽宁")
    geocode = [call["url"] for call in http.calls if "geocoding" in call["url"]][0]
    assert "name=%E6%9C%9D%E9%98%B3&" in geocode      # 只查「朝阳」
    assert "%E8%BE%BD%E5%AE%81" not in geocode        # 不带「辽宁」


def test_fetch_never_fires_a_request_with_an_exhausted_budget():
    """预算已耗尽 → 直接按失败收场，一次请求都不发（用户等待有上界）。"""
    clock = _Clock()
    http = _FakeHttp([("toy1.weather.com.cn", CN_SEARCH_鹿城),
                      ("d1.weather.com.cn", CN_CURRENT_鹿城)], clock=clock)
    with pytest.raises(W.WeatherError):
        _client(http, clock=clock).fetch("鹿城", deadline=clock.now - 1.0)
    assert http.calls == []


def test_each_request_timeout_is_capped_by_the_remaining_budget():
    clock = _Clock()
    http = _FakeHttp([("toy1.weather.com.cn", CN_SEARCH_鹿城),
                      ("d1.weather.com.cn", CN_CURRENT_鹿城)], clock=clock)
    _client(http, clock=clock).fetch("鹿城", deadline=clock.now + 1.5)
    assert http.calls[0]["timeout"] == pytest.approx(1.5)


def test_budget_spent_mid_flight_blocks_the_fallback_source():
    """源 A 把预算耗光后不再试源 B——总耗时必须有上界，不是两源各自撞满超时。"""
    clock = _Clock()
    http = _FakeHttp([("toy1.weather.com.cn", "([])")], clock=clock, advance=9.0)
    with pytest.raises(W.WeatherError):
        _client(http, clock=clock).fetch("鹿城", deadline=clock.now + 8.0)
    assert all("open-meteo" not in call["url"] for call in http.calls)


def test_search_cities_returns_ranked_candidates_and_reason():
    ok = _FakeHttp([("toy1.weather.com.cn", CN_SEARCH_鹿城)])
    results, error = _client(ok).search_cities("鹿城")
    # 精确命中排第一，其余候选保留供用户改选
    assert [c.code for c in results] == ["101210710", "101210701009"] and error == ""

    bad = _FakeHttp([("toy1.weather.com.cn", TimeoutError("timed out"))])
    results, error = _client(bad).search_cities("鹿城")
    assert results == [] and "超时" in error


class _Clock:
    """可控时钟：推进时间以便确定性地测总预算。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_timeout_and_city_limit_constants_are_the_documented_ones():
    assert W.HTTP_TIMEOUT == 6.0
    assert W.MAX_CITIES == 12
    assert W.TOTAL_BUDGET_SECONDS > W.HTTP_TIMEOUT


# ------------------------------------------------ 响应体有界读取与总截止（m12）
class _Reader:
    """分块读取替身：按脚本吐块，并记录每次调用。"""

    def __init__(self, chunks, *, clock=None, chunk_cost=0.0):
        self.chunks = list(chunks)
        self.calls = 0
        self._clock = clock
        self._chunk_cost = chunk_cost

    def __call__(self, size):
        self.calls += 1
        if self._clock is not None and self._chunk_cost:
            self._clock.now += self._chunk_cost
        return self.chunks.pop(0) if self.chunks else b""


def test_read_bounded_returns_the_whole_body_when_small():
    reader = _Reader([b"abc", b"def"])
    assert W.read_bounded(reader, chunk=4) == b"abcdef"
    assert reader.calls == 3          # 末次空块 = EOF


def test_read_bounded_aborts_when_the_body_exceeds_the_cap():
    oversized = b"x" * (W.MAX_BODY_BYTES + 1)
    with pytest.raises(W.WeatherError) as info:
        W.read_bounded(_Reader([oversized]), chunk=1024)
    assert "过大" in str(info.value)
    assert str(W.MAX_BODY_BYTES // (1024 * 1024)) in str(info.value)


def test_read_bounded_aborts_when_the_deadline_passes_mid_transfer():
    """慢速持续滴流也不能拖过总截止：每块之间检查截止时间。"""
    clock = _Clock()
    reader = _Reader([b"a" * 64] * 50, clock=clock, chunk_cost=1.0)
    with pytest.raises(W.WeatherError) as info:
        W.read_bounded(reader, deadline=clock.now + 3.0, clock=clock, chunk=64)
    assert "超时" in str(info.value)
    assert reader.calls <= 5, "超过截止后必须停止读，而不是把整个响应拖完"


def test_read_bounded_without_a_deadline_still_caps_the_size():
    assert W.read_bounded(_Reader([b"ok"]), chunk=2) == b"ok"


def test_describe_error_passes_weather_errors_through():
    assert W.describe_error(W.WeatherError("响应体过大（超过 2MB）")) == "响应体过大（超过 2MB）"
