# -*- coding: utf-8 -*-
"""灵动岛网络槽采样核心的单测（``pet/net_monitor.py``）。

纯逻辑层，全部依赖注入：计数器、时钟、socket 都是替身，**不发真包**（活体探测
由实机验证覆盖，见 PR 报告的实机清单）。时序类断言一律事件同步 + 宽预算。
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from pet import net_monitor as nm


# ---------------------------------------------------------------- 替身
class FakeSocket:
    """探测 socket 替身：记录 send，按脚本回包。"""

    def __init__(self, replies=None, *, send_error=None, recv_error=None, clock=None,
                 auto_reply=False):
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.closed = False
        self.connected = None
        self.auto_reply = auto_reply
        self._replies = list(replies or [])
        self._send_error = send_error
        self._recv_error = recv_error
        self._clock = clock

    def connect(self, address):
        self.connected = address

    def send(self, payload):
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(bytes(payload))
        return len(payload)

    def settimeout(self, value):
        self.timeouts.append(float(value))

    def recv(self, _size):
        if self._recv_error is not None:
            raise self._recv_error
        if self.auto_reply:
            return _reply_for(self)
        if self._replies:
            reply = self._replies.pop(0)
            if callable(reply):
                reply = reply(self)
            return reply
        # 没脚本了：推进假时钟直到超过 deadline（模拟"一直没收到"）
        if self._clock is not None:
            self._clock.advance(1.0)
        raise TimeoutError("timeout")

    def close(self):
        self.closed = True


class Clock:
    """可手动推进的假时钟。"""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _reply_for(sock: FakeSocket, advance: float = 0.012) -> bytes:
    """脚本回包：事务号照抄刚发出的查询（延迟 advance 秒）。"""
    query = sock.sent[-1]
    if sock._clock is not None:
        sock._clock.advance(advance)
    return query[:2] + b"\x81\x80" + b"\x00" * 28


# ---------------------------------------------------------------- 区间口径
def test_clamp_interval_bounds_and_bad_values():
    assert nm.clamp_interval(0.1) == 0.5
    assert nm.clamp_interval(600) == 60.0
    assert nm.clamp_interval("2.5") == 2.5
    assert nm.clamp_interval("abc") == 1.0
    assert nm.clamp_interval(None) == 1.0
    assert nm.clamp_interval(float("nan")) == 1.0
    assert nm.clamp_interval(10 ** 400) == 1.0


def test_probe_timeout_derives_from_interval():
    assert nm.probe_timeout_for(0.5) == pytest.approx(0.4)
    assert nm.probe_timeout_for(1.0) == pytest.approx(0.8)
    # 上限 1s：60 秒间隔不至于让单次探测挂住一分钟
    assert nm.probe_timeout_for(60) == pytest.approx(1.0)


# ---------------------------------------------------------------- 网速
def test_rate_meter_first_sample_is_baseline_then_delta():
    clock = Clock()
    counts = [(1000.0, 500.0), (3048.0, 1524.0)]
    meter = nm.NetRateMeter(counters=lambda: counts.pop(0), clock=clock, smoothing=None)

    first = meter.sample()
    assert (first.down_bps, first.up_bps) == (0.0, 0.0)  # 首轮只建基线

    clock.advance(1.0)
    second = meter.sample()
    assert second.down_bps == pytest.approx(2048.0)
    assert second.up_bps == pytest.approx(1024.0)


def test_rate_meter_treats_counter_reset_as_zero_not_spike():
    """32 位计数器回绕 / 断开重连：累计值变小按 0 计，绝不报假尖峰。"""
    clock = Clock()
    counts = [(10_000.0, 10_000.0), (120.0, 80.0)]
    meter = nm.NetRateMeter(counters=lambda: counts.pop(0), clock=clock, smoothing=None)
    meter.sample()
    clock.advance(1.0)
    sample = meter.sample()
    assert (sample.down_bps, sample.up_bps) == (0.0, 0.0)


def test_rate_meter_smoothing_is_ema():
    clock = Clock()
    counts = [(0.0, 0.0), (1024.0, 0.0), (2048.0, 0.0)]
    meter = nm.NetRateMeter(counters=lambda: counts.pop(0), clock=clock, smoothing=0.5)
    meter.sample()
    clock.advance(1.0)
    assert meter.sample().down_bps == pytest.approx(512.0)  # 0 + (1024-0)*0.5
    clock.advance(1.0)
    assert meter.sample().down_bps == pytest.approx(768.0)  # 512 + (1024-512)*0.5


def test_rate_meter_survives_missing_counters():
    meter = nm.NetRateMeter(counters=lambda: None, clock=Clock(), smoothing=None)
    assert meter.sample() == nm.RateSample(0.0, 0.0)
    assert meter.rate == nm.RateSample(0.0, 0.0)


def test_rate_meter_ignores_nonpositive_elapsed():
    clock = Clock()
    counts = [(0.0, 0.0), (2048.0, 0.0)]
    meter = nm.NetRateMeter(counters=lambda: counts.pop(0), clock=clock, smoothing=None)
    meter.sample()
    assert meter.sample() == nm.RateSample(0.0, 0.0)  # 时钟未推进 → 不除零


# ---------------------------------------------------------------- 探测载荷
def test_dns_query_shape_and_size():
    query = nm.build_dns_query(0x1234, name="www.aliyun.com")
    tid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", query[:12])
    assert (tid, flags, qd, an, ns, ar) == (0x1234, 0x0100, 1, 0, 0, 0)
    # 名称：3www 6aliyun 3com 0
    assert query[12:28] == b"\x03www\x06aliyun\x03com\x00"
    assert struct.unpack("!HH", query[28:32]) == (1, 1)  # A / IN
    assert len(query) == 32
    assert nm.PROBE_BYTES_PER_ROUND >= len(query)


def test_encode_dns_name_rejects_overlong_label():
    with pytest.raises(ValueError):
        nm.encode_dns_name("a" * 64 + ".com")


def test_default_probe_target_is_domestic_and_never_a_name():
    """默认目标必须是境内 IP 字面量：打国外地址就是拿墙外 RTT 冒充当前网络延迟。"""
    host, port = nm.DEFAULT_PROBE_TARGET
    assert host == "223.5.5.5"
    assert port == 53
    assert host.replace(".", "").isdigit()


# ---------------------------------------------------------------- 探测行为
def test_probe_measures_rtt_only_for_matching_transaction_id():
    clock = Clock()
    fake = FakeSocket(clock=clock)
    probe = nm.UdpLatencyProbe(clock=clock, socket_factory=lambda *a: fake)
    fake._replies = [lambda sock: _reply_for(sock, advance=0.012)]

    outcome = probe.measure(transaction_id=0xAAAA)
    assert outcome.lost is False
    assert outcome.rtt_ms == pytest.approx(12.0, abs=0.5)
    assert fake.sent and fake.sent[-1][:2] == b"\xaa\xaa"
    assert fake.connected == nm.DEFAULT_PROBE_TARGET


def test_probe_discards_stale_reply_and_reports_lost():
    """迟到包（事务号不匹配）不算数：否则会用上一轮的答案算出"假快"的 RTT。"""
    clock = Clock()
    fake = FakeSocket(clock=clock)
    probe = nm.UdpLatencyProbe(clock=clock, socket_factory=lambda *a: fake)
    fake._replies = [b"\xbb\xbb" + b"\x81\x80" + b"\x00" * 28]

    outcome = probe.measure(transaction_id=0xAAAA)
    assert outcome.lost is True
    assert outcome.rtt_ms is None


@pytest.mark.parametrize("kwargs", [
    {"send_error": OSError("no route")},
    {"recv_error": TimeoutError("timeout")},
    {"recv_error": ConnectionResetError("icmp port unreachable")},
])
def test_probe_reports_lost_on_socket_failures(kwargs):
    clock = Clock()
    fake = FakeSocket(clock=clock, **kwargs)
    probe = nm.UdpLatencyProbe(clock=clock, socket_factory=lambda *a: fake)
    assert probe.measure(transaction_id=1).lost is True


def test_probe_reuses_socket_across_rounds_and_closes_it():
    clock = Clock()
    created: list[FakeSocket] = []

    def factory(*_args):
        fake = FakeSocket(clock=clock, auto_reply=True)
        created.append(fake)
        return fake

    probe = nm.UdpLatencyProbe(clock=clock, socket_factory=factory)
    assert probe.measure(transaction_id=1).lost is False
    assert probe.measure(transaction_id=2).lost is False
    assert len(created) == 1  # 同一会话复用 socket，不每轮新建
    probe.close()
    assert created[0].closed is True


def test_probe_socket_creation_failure_is_lost_not_raise():
    def factory(*_args):
        raise OSError("socket() failed")

    probe = nm.UdpLatencyProbe(clock=Clock(), socket_factory=factory)
    assert probe.measure(transaction_id=1).lost is True


def test_probe_udp_latency_helper_closes_socket():
    clock = Clock()
    created: list[FakeSocket] = []

    def factory(*_args):
        fake = FakeSocket(clock=clock, auto_reply=True)
        created.append(fake)
        return fake

    outcome = nm.probe_udp_latency(clock=clock, socket_factory=factory, transaction_id=7)
    assert outcome.lost is False
    assert created[0].closed is True


# ---------------------------------------------------------------- 文案
def test_format_rate_units():
    assert nm.format_rate(0) == "0B"
    assert nm.format_rate(1023) == "1023B"
    assert nm.format_rate(2048) == "2K"
    assert nm.format_rate(5 * 1024 * 1024) == "5.0M"
    assert nm.format_rate(-5) == "0B"
    assert nm.format_rate("bad") == "0B"


def test_format_rate_never_exceeds_field_width():
    """速率字段不得超过 5 列：超了定宽槽位就失效（胶囊会忽宽忽窄）。"""
    samples = [
        0, 1, 999, 1023, 1024, 1025, 100 * 1024, 1023 * 1024,
        1024 ** 2, 12 * 1024 ** 2, 99 * 1024 ** 2, 100 * 1024 ** 2,
        1023 * 1024 ** 2, 1024 ** 3, 99 * 1024 ** 3, 10 ** 13,
    ]
    for value in samples:
        text = nm.format_rate(value)
        assert len(text) <= nm._RATE_FIELD_WIDTH, (value, text)
        assert text[-1] in "BKM G".replace(" ", "")


def test_format_network_slot_is_fixed_width():
    texts = {
        nm.format_network_slot(0, 0, None),
        nm.format_network_slot(1234, 987654321, 8.0),
        nm.format_network_slot(12 * 1024 * 1024, 300 * 1024, 123.4),
    }
    assert len(texts) == 3
    assert len({len(text) for text in texts}) == 1, texts
    assert all(text.startswith("↓") and "↑" in text for text in texts)
    assert nm.format_network_slot(0, 0, None).endswith("--")
    assert nm.format_network_slot(0, 0, 12.0, lost=True).endswith("--")
    assert nm.format_network_slot(0, 0, 12.4).endswith("12ms")


def test_real_counter_reader_is_psutil_backed_or_honestly_none():
    """真实计数器读取：psutil 可用时给 (收, 发) 两个浮点，不可用给 None（不抛）。

    本机安装路径的真实集成（不联网）：psutil 已在 requirements.txt 里声明，
    读的是网卡累计字节，不产生任何流量。
    """
    sample = nm.read_counters()
    assert sample is None or (
        isinstance(sample, tuple) and len(sample) == 2
        and all(isinstance(value, float) for value in sample)
    ), sample


def test_monitor_with_real_counters_and_stub_socket():
    """把真计数器接到采样器上跑一轮：证明 psutil 那条路径能出样本（探测仍走替身）。"""
    fake = FakeSocket(auto_reply=True)
    monitor = nm.NetMonitor(
        interval=1.0,
        counters=nm.read_counters,
        socket_factory=lambda *a: fake,
        smoothing=None,
    )
    try:
        first = monitor.sample_once()
        assert first.latency_lost is False
        if nm.read_counters() is None:
            assert (first.down_bps, first.up_bps) == (0.0, 0.0)
        else:
            assert first.down_bps >= 0.0 and first.up_bps >= 0.0
    finally:
        monitor.close()


# ---------------------------------------------------------------- 线程
def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_monitor_thread_samples_then_stops():
    clock = Clock()
    counts = [(0.0, 0.0), (4096.0, 2048.0)]
    fake = FakeSocket(clock=clock, auto_reply=True)
    sampled = threading.Event()

    monitor = nm.NetMonitor(
        interval=0.5,
        counters=lambda: counts.pop(0) if counts else (4096.0, 2048.0),
        clock=clock,
        socket_factory=lambda *a: fake,
        smoothing=None,
        on_sample=lambda _sample: sampled.set(),
    )
    assert monitor.start() is True
    assert monitor.start() is False  # 已在跑：不重复起线程
    try:
        assert sampled.wait(5.0), "采样线程未在宽预算内出结果"
        sample = monitor.snapshot()
        assert sample is not None
        assert sample.latency_lost is False
        assert sample.latency_ms == pytest.approx(12.0, abs=0.5)
    finally:
        monitor.stop(wait=True)

    assert monitor.running is False
    assert _wait_until(lambda: not monitor.running, timeout=2.0)
    monitor.close()


def test_monitor_stop_then_start_does_not_leave_two_live_threads():
    """stop→start 紧邻（隐藏后马上显示）：旧线程只认自己的停止事件，不得复活。"""
    first_sampled = threading.Event()
    fake = FakeSocket(auto_reply=True)
    monitor = nm.NetMonitor(
        interval=0.5,
        counters=lambda: (0.0, 0.0),
        socket_factory=lambda *a: fake,
        smoothing=None,
        on_sample=lambda _sample: first_sampled.set(),
    )
    monitor.start()
    assert first_sampled.wait(5.0)
    monitor.stop(wait=False)
    monitor.start()
    monitor.stop(wait=True)
    # 旧线程若被 start() 里的 clear() 复活，它会一直循环 → 这里等到宽预算耗尽
    assert _wait_until(lambda: not _live_net_threads(), timeout=5.0), \
        f"停止后仍有采样线程在跑：{_live_net_threads()}"
    monitor.close()


def _live_net_threads() -> list[threading.Thread]:
    """当前存活的采样线程（线程名是 net_monitor 的固定标识）。"""
    return [thread for thread in threading.enumerate() if thread.name == "pet-net-monitor"]


def test_stale_generation_result_never_overwrites_new_snapshot():
    """旧代在途探测晚到，不得覆盖新代快照（stop→start 只有停止事件隔离是不够的）。

    复现路径：第一代卡在收包 → stop→start（第二代立刻出结果）→ 放行第一代。
    第一代的样本带更大的时间戳，若共享快照/无代核验就会盖掉新代结果。
    全程事件同步：不用 sleep 猜时序，"第一代已放弃发布"由它自己的 socket 关闭事件确认。
    """
    gate = threading.Event()      # 放行旧代的收包
    entered = threading.Event()   # 旧代已进入收包
    finished = threading.Event()  # 旧代已跑完最后一次发布尝试（线程收口）
    sockets: list[FakeSocket] = []

    class BlockingSocket(FakeSocket):
        def recv(self, _size):
            entered.set()
            gate.wait(5.0)
            return _reply_for(self)

        def close(self):
            super().close()
            finished.set()

    def factory(*_args):
        sock = BlockingSocket() if not sockets else FakeSocket(auto_reply=True)
        sockets.append(sock)
        return sock

    samples: list[nm.NetSample] = []
    monitor = nm.NetMonitor(
        interval=0.5,
        counters=lambda: (0.0, 0.0),
        socket_factory=factory,
        smoothing=None,
        on_sample=samples.append,
    )
    monitor.start()
    try:
        assert entered.wait(5.0), "旧代未进入收包"
        monitor.stop(wait=False)
        monitor.start()
        assert _wait_until(lambda: bool(samples), timeout=5.0), "新代未出样本"
        fresh = monitor.snapshot()
        assert fresh is samples[-1]

        gate.set()  # 放行旧代：它的结果晚于新代
        assert finished.wait(5.0), "旧代未收口"
        assert monitor.snapshot() is fresh, "旧代晚到的结果覆盖了新代快照"
        assert samples[-1] is fresh, "旧代晚到的结果仍然触发了回调"
    finally:
        gate.set()
        monitor.stop(wait=True)
        monitor.close()


def test_new_run_rebaselines_rate_instead_of_inheriting_old_meter():
    """每一代自带 meter：新一代第一轮只建基线，不继承旧代的计数器基线。

    若共用 meter，重置基准前计数器已跳变（1MB），新一代第一轮会算出假速率。
    """
    jumped = threading.Event()
    samples: list[nm.NetSample] = []
    monitor = nm.NetMonitor(
        interval=0.5,
        counters=lambda: (1_000_000.0, 0.0) if jumped.is_set() else (0.0, 0.0),
        socket_factory=lambda *a: FakeSocket(auto_reply=True),
        smoothing=None,
        on_sample=samples.append,
    )
    monitor.start()
    assert _wait_until(lambda: bool(samples), timeout=5.0), "第一代未出样本"
    monitor.stop(wait=True)

    jumped.set()  # 新一代开始前计数器跳变
    monitor.start()
    try:
        assert _wait_until(lambda: len(samples) > 1, timeout=5.0), "第二代未出样本"
        fresh = samples[1]
        assert fresh.down_bps == 0.0, (
            f"新一代第一轮必须重新建基线，实测 {fresh.down_bps} B/s（像是继承了旧 meter）")
    finally:
        monitor.stop(wait=True)
        monitor.close()


def test_monitor_thread_survives_sampling_error():
    """采样异常不得杀线程（否则槽永久停在最后一帧，看不出坏了）。"""
    calls = {"n": 0}
    first_done = threading.Event()

    def counters():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return (0.0, 0.0)

    monitor = nm.NetMonitor(
        interval=0.5,
        counters=counters,
        socket_factory=lambda *a: FakeSocket(auto_reply=True),
        smoothing=None,
        on_sample=lambda _sample: first_done.set(),
    )
    monitor.start()
    try:
        assert first_done.wait(5.0), "采样异常后线程未继续"
        assert calls["n"] >= 2
        assert monitor.running is True
    finally:
        monitor.stop(wait=True)
    monitor.close()


def test_monitor_sample_once_uses_injected_prober_and_snapshot_is_thread_safe():
    fake = FakeSocket(replies=[b"\x00\x01" + b"\x81\x80" + b"\x00" * 28])
    monitor = nm.NetMonitor(
        interval=1.0, counters=lambda: (0.0, 0.0),
        socket_factory=lambda *a: fake, smoothing=None,
    )
    probe = nm.UdpLatencyProbe(socket_factory=lambda *a: fake)
    sample = monitor.sample_once(now=10.0, prober=probe)
    assert monitor.snapshot() == sample
    assert sample.sampled_at == 10.0
    monitor.close()
    probe.close()
