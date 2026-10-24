# -*- coding: utf-8 -*-
"""灵动岛「网络槽」的采样核心（零 Qt，依赖全部可注入）。

槽内容 = 上下行网速 + 延迟。两条数据源各自的取舍：

- **网速（零流量成本）**：读网卡累计字节计数器算 ``Δ字节/Δt``。用
  ``requirements.txt`` 里已有的 psutil（``net_io_counters``）。本机实测单次调用
  **23ms**（63 张网卡的表遍历 + 具名元组构造），所以采样固定在后台线程
  （:class:`NetMonitor`），GUI 线程只读一次快照（:meth:`NetMonitor.snapshot`）——
  不把 23ms 的 syscall 塞进 16ms 的动画帧预算里。
- **延迟（要发小包）**：UDP 往返，默认目标 ``223.5.5.5:53``（阿里公共 DNS：
  Anycast、境内、免 key、查询内容无隐私）。**绝不默认打国外地址**——拿墙外 RTT
  冒充当前网络延迟是假数据。载荷是一条合法 DNS A 查询（``www.aliyun.com``，
  32 字节），应答约 70 字节，单次往返在网量约 160 字节（含 IP/UDP 头）；用合法
  查询才指望解析器回包，发垃圾包会被静默丢弃、延迟永远测不出来，也就谈不上"探测"。
  开着系统代理 / VPN 时 RTT 反映的是那一刻隧道内的路径，这是 feature 不是 bug
  （见 ``docs/NETWORK-PROXY-AND-VPN-2026-09-22.md``）。

线程纪律：采样线程由配置开关驱动（默认关），岛隐藏 / 开关关闭即停；探测超时
≤ 1s，故 :meth:`NetMonitor.stop` 默认只置停止事件、**不在 GUI 线程 join**
（join 会把"隐藏岛"卡住一个探测周期）。
"""
from __future__ import annotations

import logging
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 延迟探测目标（IP 字面量：不依赖系统 DNS 解析，探测本身不产生解析请求）。
#: 选 223.5.5.5 而不是网关/运营商 DNS：网关要走路由表查询（Windows 上得读
#: GetIpForwardTable），运营商 DNS 地址各家不同且可能被劫持；阿里公共 DNS 是
#: Anycast、境内、地址稳定，测出来的就是"到最近公共解析点"的真实 RTT。
DEFAULT_PROBE_TARGET = ("223.5.5.5", 53)
#: 探测载荷里查询的域名（只要有合法应答即可，内容不参与展示）。
PROBE_QUERY_NAME = "www.aliyun.com"
#: 单次探测的**在网字节量**估计（含 IPv4 20 + UDP 8 头 ×2）：查询 32 字节、
#: 应答约 70 字节（A 记录 + 常见 EDNS0 OPT）。设置页文案按它写流量成本。
PROBE_BYTES_PER_ROUND = 160

MIN_INTERVAL_SECONDS = 0.5
MAX_INTERVAL_SECONDS = 60.0
DEFAULT_INTERVAL_SECONDS = 1.0
#: 探测超时 = 间隔的 0.8 倍（留出余量，不让待收包跨越下一个探测周期），
#: 并夹在 [0.2, 1.0] 秒：太短会在慢链路上系统性丢结果，太长会拖慢停止响应。
_MIN_TIMEOUT_SECONDS = 0.2
_MAX_TIMEOUT_SECONDS = 1.0


def clamp_interval(value) -> float:
    """``network_probe_interval_seconds`` 的区间口径（0.5~60 秒，坏值回默认）。"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_INTERVAL_SECONDS
    if number != number:  # NaN
        return DEFAULT_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, number))


def probe_timeout_for(interval: float) -> float:
    """按探测间隔给出单次收包超时（见 :data:`_MIN_TIMEOUT_SECONDS`）。"""
    return max(_MIN_TIMEOUT_SECONDS, min(_MAX_TIMEOUT_SECONDS, clamp_interval(interval) * 0.8))


# ---------------------------------------------------------------- 网速（计数器）
def read_counters() -> tuple[float, float] | None:
    """累计 (收字节, 发字节)；psutil 不可用时返回 None（槽显示空值，不抛）。

    延迟 import + 兜底：psutil 缺失 / 版本不含 net_io_counters 时，网络槽降级为
    "读不到计数器"而不是让桌宠启动失败（同 ``pet/now_playing.py`` 的既有口径）。
    """
    try:
        import psutil
    except Exception:  # pragma: no cover - 环境缺 psutil 时走这里
        logger.debug("网络槽：psutil 不可用，网速读数停用", exc_info=True)
        return None
    try:
        counters = psutil.net_io_counters()
    except Exception:  # pragma: no cover - 平台不支持时走这里
        logger.debug("网络槽：net_io_counters 调用失败", exc_info=True)
        return None
    return float(counters.bytes_recv), float(counters.bytes_sent)


@dataclass(frozen=True)
class RateSample:
    """一次速率读数（字节/秒）。"""

    down_bps: float
    up_bps: float


class NetRateMeter:
    """累计计数器 → 瞬时速率（纯计算，计数器与时钟可注入）。

    ``smoothing`` = EMA 系数：1 秒粒度的原始差值跳动很大（同一段视频播放，
    每秒差值能差 ±50%），不平滑的数字会让岛上的文字一直乱蹦。默认 0.5 相当于
    约 1 秒半衰期；传 None 关闭平滑（测试与"要原始值"的调用方用）。
    """

    def __init__(self, counters=read_counters, clock=time.monotonic, smoothing: float | None = 0.5):
        self._counters = counters
        self._clock = clock
        self._smoothing = smoothing
        self._prev: tuple[float, float] | None = None
        self._prev_at: float | None = None
        self._rate = RateSample(0.0, 0.0)

    @property
    def rate(self) -> RateSample:
        return self._rate

    def sample(self, now: float | None = None) -> RateSample:
        """采样一次。首次调用只建立基线（速率按 0 报，下一周期起是真值）。"""
        now = self._clock() if now is None else float(now)
        counters = self._counters()
        if counters is None:
            return self._rate
        if self._prev is None or self._prev_at is None:
            self._prev, self._prev_at = counters, now
            return self._rate
        elapsed = now - self._prev_at
        if elapsed <= 0:
            return self._rate
        down = _delta(counters[0], self._prev[0]) / elapsed
        up = _delta(counters[1], self._prev[1]) / elapsed
        self._prev, self._prev_at = counters, now
        if self._smoothing is None:
            self._rate = RateSample(down, up)
        else:
            alpha = max(0.0, min(1.0, float(self._smoothing)))
            self._rate = RateSample(
                _ema(self._rate.down_bps, down, alpha),
                _ema(self._rate.up_bps, up, alpha),
            )
        return self._rate


def _delta(current: float, previous: float) -> float:
    diff = float(current) - float(previous)
    if diff < 0:
        # 32 位计数器回绕，或换网卡 / 断开重连导致累计值变小：按 0 计（宁少不假）。
        return 0.0
    return diff


def _ema(previous: float, current: float, alpha: float) -> float:
    return previous + (current - previous) * alpha


# ---------------------------------------------------------------- 延迟（UDP 往返）
@dataclass(frozen=True)
class ProbeOutcome:
    """一次探测的结论：``rtt_ms`` 为 None 表示本轮没收到达标应答（``lost``）。"""

    rtt_ms: float | None
    lost: bool


def encode_dns_name(name: str) -> bytes:
    """域名 → DNS 线上格式（长度前缀标签 + 根结束符）。"""
    out = bytearray()
    for label in str(name).strip(".").split("."):
        if not label:
            continue
        raw = label.encode("ascii")
        if len(raw) > 63:
            raise ValueError("DNS 标签超过 63 字节")
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def build_dns_query(transaction_id: int, name: str = PROBE_QUERY_NAME) -> bytes:
    """构造最小 DNS A 查询（标准查询、期望递归）。"""
    header = struct.pack("!HHHHHH", int(transaction_id) & 0xFFFF, 0x0100, 1, 0, 0, 0)
    return header + encode_dns_name(name) + struct.pack("!HH", 1, 1)


def probe_udp_latency(
    target: tuple[str, int] = DEFAULT_PROBE_TARGET,
    timeout: float = DEFAULT_INTERVAL_SECONDS,
    *,
    socket_factory=socket.socket,
    clock=time.monotonic,
    transaction_id: int | None = None,
) -> ProbeOutcome:
    """一次性 UDP 往返（阻塞至多 ``timeout``）。socket 由本函数创建并关闭。"""
    probe = UdpLatencyProbe(target, timeout=timeout, socket_factory=socket_factory, clock=clock)
    try:
        return probe.measure(transaction_id=transaction_id)
    finally:
        probe.close()


class UdpLatencyProbe:
    """复用同一 socket 的 UDP 探测会话（采样线程内一轮一次）。

    只认**事务号匹配**的应答：迟到包 / 上一次探测的回包不算数，否则 RTT 会
    被"上一轮的答案"算成极小值（假快）。
    """

    def __init__(
        self,
        target: tuple[str, int] = DEFAULT_PROBE_TARGET,
        timeout: float = DEFAULT_INTERVAL_SECONDS,
        *,
        socket_factory=socket.socket,
        clock=time.monotonic,
    ):
        self.target = (str(target[0]), int(target[1]))
        self.timeout = float(timeout)
        self._socket_factory = socket_factory
        self._clock = clock
        self._sock = None

    def _ensure_socket(self):
        if self._sock is not None:
            return self._sock
        family = socket.AF_INET6 if ":" in self.target[0] else socket.AF_INET
        sock = self._socket_factory(family, socket.SOCK_DGRAM)
        # connect：允许 send/recv 不带地址，并把 ICMP 端口不可达变成 recv 上的
        # OSError（能当场判"不通"，不用等到超时）。
        sock.connect(self.target)
        self._sock = sock
        return sock

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:  # pragma: no cover - 已关闭/被系统收回
            logger.debug("网络槽：探测 socket 关闭失败", exc_info=True)

    def measure(self, *, transaction_id: int | None = None, now: float | None = None) -> ProbeOutcome:
        """发一次探测并等应答；返回 RTT（毫秒）或 lost。"""
        try:
            sock = self._ensure_socket()
        except OSError:
            logger.debug("网络槽：探测 socket 创建失败", exc_info=True)
            return ProbeOutcome(None, True)
        tid = random.randint(0, 0xFFFF) if transaction_id is None else int(transaction_id) & 0xFFFF
        query = build_dns_query(tid)
        started = self._clock() if now is None else float(now)
        try:
            sock.send(query)
        except OSError:
            logger.debug("网络槽：探测包发送失败", exc_info=True)
            return ProbeOutcome(None, True)
        deadline = started + self.timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return ProbeOutcome(None, True)
            try:
                sock.settimeout(remaining)
                data = sock.recv(512)
            except (TimeoutError, OSError):
                # 超时（socket.timeout 是 TimeoutError）与 ICMP 不可达都归"本轮无结果"。
                return ProbeOutcome(None, True)
            if len(data) >= 12 and data[0:2] == query[0:2]:
                return ProbeOutcome((self._clock() - started) * 1000.0, False)


# ---------------------------------------------------------------- 文本（定宽）
_UNKNOWN_LATENCY = "--"
_RATE_FIELD_WIDTH = 5
_LATENCY_FIELD_WIDTH = 5


def format_rate(bps) -> str:
    """字节/秒 → 紧凑单位，**最长 5 列**（``0B`` / ``812K`` / ``1.2M`` / ``941M``）。

    宽度上界是可见契约（定宽槽位靠它不抖），所以单位跨档时改用整数：宁可丢掉
    大数率的小数点，也不让槽位忽宽忽窄。
    """
    try:
        value = max(0.0, float(bps))
    except (TypeError, ValueError):
        value = 0.0
    if value < 1024:
        return f"{value:.0f}B"
    if value < 1024 ** 2:
        return f"{value / 1024:.0f}K"
    if value < 100 * 1024 ** 2:
        return f"{value / 1024 ** 2:.1f}M"
    if value < 1024 ** 3:
        return f"{value / 1024 ** 2:.0f}M"
    if value < 100 * 1024 ** 3:
        return f"{value / 1024 ** 3:.1f}G"
    return f"{value / 1024 ** 3:.0f}G"


def format_latency(latency_ms, *, lost: bool = False) -> str:
    """延迟 → ``23ms``；未测到 / 超时 → ``--``。"""
    if lost or latency_ms is None:
        return _UNKNOWN_LATENCY
    try:
        return f"{float(latency_ms):.0f}ms"
    except (TypeError, ValueError):
        return _UNKNOWN_LATENCY


def format_network_slot(
    down_bps, up_bps, latency_ms=None, *, lost: bool = False,
) -> str:
    """岛上网络槽的定宽文案，例如 ``↓  1.2M ↑   340K  23ms``。

    **定宽是刻意的**：胶囊宽度由文本宽度算出（``_capsule_width``），数字长度
    每秒变化会让岛左右抖动；按字段右对齐填充后，各字段宽度恒定（速率 5 列、
    延迟 5 列），1 秒级刷新不再改窗口几何。
    """
    return (
        f"↓{format_rate(down_bps):>{_RATE_FIELD_WIDTH}}"
        f" ↑{format_rate(up_bps):>{_RATE_FIELD_WIDTH}}"
        f" {format_latency(latency_ms, lost=lost):>{_LATENCY_FIELD_WIDTH}}"
    )


# ---------------------------------------------------------------- 采样线程
@dataclass(frozen=True)
class NetSample:
    """一个采样周期的结果快照（GUI 线程只读它，不做任何 syscall）。"""

    down_bps: float
    up_bps: float
    latency_ms: float | None
    latency_lost: bool
    sampled_at: float


class NetMonitor:
    """后台采样线程：每 ``interval`` 秒读一次计数器 + 发一次延迟探测。

    生命周期由调用方（灵动岛）按"开关开着且岛可见"驱动：隐藏 / 关闭即
    :meth:`stop`。停止只置事件，**不 join**——探测在途最多 1s，join 会把
    GUI 线程卡住（隐藏岛是常见交互）。

    **代（generation）隔离**：``stop()`` 立刻允许 ``start()``，而上一轮线程可能
    还卡在收包里——所以每轮自带 generation token、meter 与停止事件，发布快照前
    核验"自己还是不是当前代"：

    - 旧代晚到的结果被丢弃，不会用更晚的时间戳盖掉新代快照；
    - 速率基线（``NetRateMeter``）也是每代一份，两代重叠读计数器不会把基线推来
      推去（新一代重新建基线，宁可少报一个周期的速率，也不报错值）。
    """

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        *,
        counters=read_counters,
        clock=time.monotonic,
        socket_factory=socket.socket,
        target: tuple[str, int] = DEFAULT_PROBE_TARGET,
        smoothing: float | None = 0.5,
        on_sample=None,
    ):
        self._interval = clamp_interval(interval)
        self._clock = clock
        self._counters = counters
        self._smoothing = smoothing
        self._target = (str(target[0]), int(target[1]))
        self._socket_factory = socket_factory
        # 探测会话：采样线程每次 start 各带一份（见 _run），这里的一份只给
        # 线程外的同步采样（测试 / 一次性调用）用，惰性创建、close() 收口。
        self._prober: UdpLatencyProbe | None = None
        self._on_sample = on_sample
        self._lock = threading.Lock()
        self._snapshot: NetSample | None = None
        self._generation, self._meter = self._new_generation()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    # ------------------------------------------------------------ 状态
    @property
    def interval(self) -> float:
        return self._interval

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def snapshot(self) -> NetSample | None:
        """最近一次采样（还没跑完第一轮时返回 None）。线程安全。"""
        with self._lock:
            return self._snapshot

    def set_interval(self, interval: float) -> float:
        """运行中改采样间隔（设置页改完即时生效，不重建线程）。"""
        self._interval = clamp_interval(interval)
        return self._interval

    def _new_generation(self) -> tuple[object, NetRateMeter]:
        """开一代新采样：token（身份）+ 独立 meter（基线不跨代）。"""
        return object(), NetRateMeter(
            counters=self._counters, clock=self._clock, smoothing=self._smoothing)

    def _retire_generation(self) -> None:
        """换代：在途的旧代结果从此不再被接受（stop 时也走这里）。"""
        self._generation, self._meter = self._new_generation()

    def _new_prober(self) -> UdpLatencyProbe:
        return UdpLatencyProbe(
            self._target, timeout=probe_timeout_for(self._interval),
            socket_factory=self._socket_factory, clock=self._clock,
        )

    def _shared_prober(self) -> UdpLatencyProbe:
        if self._prober is None:
            self._prober = self._new_prober()
        return self._prober

    # ------------------------------------------------------------ 采样
    def sample_once(self, now: float | None = None,
                    prober: UdpLatencyProbe | None = None) -> NetSample:
        """同步采样一轮（当前代；线程循环与测试共用同一实现）。"""
        return self._sample(
            self._generation, self._meter,
            prober if prober is not None else self._shared_prober(), now)

    def _sample(self, generation, meter: NetRateMeter,
                prober: UdpLatencyProbe, now: float | None = None) -> NetSample:
        moment = self._clock() if now is None else float(now)
        prober.timeout = probe_timeout_for(self._interval)
        rate = meter.sample(moment)
        outcome = prober.measure(now=moment)
        sample = NetSample(
            down_bps=rate.down_bps,
            up_bps=rate.up_bps,
            latency_ms=outcome.rtt_ms,
            latency_lost=outcome.lost,
            sampled_at=moment,
        )
        self._publish(generation, sample)
        return sample

    def _publish(self, generation, sample: NetSample) -> bool:
        """发布一次采样；**只认当前代**（旧代晚到的结果直接丢弃）。"""
        with self._lock:
            if self._generation is not generation:
                return False
            self._snapshot = sample
        if callable(self._on_sample):
            try:
                self._on_sample(sample)
            except Exception:  # pragma: no cover - 回调是宿主注入面，异常不外溢
                logger.debug("网络槽：采样回调异常", exc_info=True)
        return True

    # ------------------------------------------------------------ 线程
    def start(self) -> bool:
        if self.running:
            return False
        # 每次 start 一份独立的停止事件 + 一代独立的 generation/meter：紧接着
        # stop→start（隐藏后马上显示）时，上一轮线程只认自己的事件（不会被新一轮
        # clear() 复活成第二条常驻线程），晚到的结果也会被 _publish 按代拒收。
        event = threading.Event()
        self._stop_event = event
        self._retire_generation()
        generation, meter = self._generation, self._meter
        self._thread = threading.Thread(
            target=self._run, args=(event, generation, meter),
            daemon=True, name="pet-net-monitor",
        )
        self._thread.start()
        return True

    def stop(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        """置停止事件并换代；``wait=True`` 仅在测试 / 退出路径用（阻塞至多 timeout）。

        默认不 join：探测在途最多 1s，在 GUI 线程 join 会把隐藏岛卡住。socket
        由线程自己收口（``_run`` 的 finally），所以这里也不碰它；换代保证仍在途的
        旧代结果不会写进 ``snapshot()``。
        """
        event, self._stop_event = self._stop_event, None
        if event is not None:
            event.set()
        thread, self._thread = self._thread, None
        self._retire_generation()
        if wait and thread is not None:
            thread.join(timeout)

    def close(self) -> None:
        """停止线程并释放线程外那份探测 socket（测试 / 退出用）。"""
        self.stop()
        prober, self._prober = self._prober, None
        if prober is not None:
            prober.close()

    def _run(self, stop: threading.Event, generation, meter: NetRateMeter) -> None:
        prober = self._new_prober()
        try:
            while not stop.is_set() and self._stop_event is stop:
                started = self._clock()
                try:
                    self._sample(generation, meter, prober)
                except Exception:
                    logger.exception("网络槽：采样异常（线程继续）")
                sleep = max(0.05, self._interval - (self._clock() - started))
                if stop.wait(sleep):
                    break
        finally:
            prober.close()
