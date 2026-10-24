# -*- coding: utf-8 -*-
"""白底反解 v3（低 alpha 亮色收色 + alpha 微缩）的单测。

**v2 的结构性盲区**（用户实机目验：深色角色边缘仍有明显白边）：按 alpha 除白的公式
``rec = (stored - 255×(1-a))/a`` 在 ``stored ≈ 255`` 时**恒等于 255**——「白色艺术品
边缘」（鲸鳍、白蕾丝的 AA 边）与「纯白底污染」在它眼里一模一样；``UNBLEND_MIN_ALPHA
= 21`` 的下限又让 a=13..20 的白像素原样残留。

**v3 换判据**：不问"这个像素白不白"，问"它比**最近的不透明邻居**亮多少"。所以两个
夹具就是这条判据的两侧：

- ``_frame(DARK)``：深色核 + 三圈白底污染（存值≈白）⇒ 邻居是深色 ⇒ 收色，亮度显著
  下降（用户口径：轮廓环黑底亮度 v2 14.0 → v3 的 7.1 → 加 1px 微缩 3.4）；
- ``_frame(WHITE)``：白色艺术品 + 白色 AA 边 ⇒ 邻居就是白的 ⇒ 亮度差 ≈ 0 ⇒
  **一位不动**（±8 色阶）——这正是 v2 分不开、而 v3 必须不伤的那一类。

本文件还钉住四条容易被改坏的契约：**收色只在反解之后**（顺序）、**收色只会变暗**
（逐像素不变量，不是事后检查）、**alpha 微缩只啃边界**（内部 255 不动）、**裁剪与
整帧等价**（``_pull_region`` 把扩散限制在收色带包围盒里，是纯性能手段，不许改变结果）。
"""
from __future__ import annotations

import time

import pytest
from PIL import Image

from pet import frameseq_provision as fp

# 夹具几何：居中正方核（切比雪夫距离 0）+ 三圈污染（距离 1/2/3）
SIZE = 40
CORE_LO, CORE_HI = 13, 26                       # 核的闭区间（14×14）
RING_ALPHAS = (100, 40, 13)                     # 距离 1/2/3 圈的 alpha（都在收色带内）
DARK = (24, 28, 60)                             # 深色角色（luma ≈ 30）
WHITE = (250, 250, 250)                         # 白色艺术品（luma ≈ 250）
HAZE = (252, 252, 255)                          # 存值≈白：被编码器抹开的背景白


def _luma(rgb: tuple[int, int, int]) -> int:
    """Rec.601 整数亮度——**与产品判据同一个量**（``fp.luma_band`` 的标量版）。"""
    return (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) // 1000


def _distance(x: int, y: int) -> int:
    """到核矩形的切比雪夫距离（核内为 0）。"""
    return max(CORE_LO - x, x - CORE_HI, CORE_LO - y, y - CORE_HI, 0)


def _frame(core_rgb: tuple[int, int, int], *, size: int = SIZE,
           ring: int = 3) -> Image.Image:
    """核（不透明）+ 三圈白底污染（存值≈白）+ 背景全透明。"""
    payload = bytearray()
    for y in range(size):
        for x in range(size):
            distance = _distance(x, y)
            if distance == 0:
                payload += bytes((*core_rgb, 255))
            elif distance <= ring:
                payload += bytes((*HAZE, RING_ALPHAS[distance - 1]))
            else:
                payload += bytes((0, 0, 0, 0))
    return Image.frombytes("RGBA", (size, size), bytes(payload))


def _ring_pixels(*, ring: int = 3, size: int = SIZE):
    """三圈污染像素的 ``(x, y, distance)``（按核外由近到远）。"""
    return [(x, y, _distance(x, y)) for y in range(size) for x in range(size)
            if 0 < _distance(x, y) <= ring]


def _reference_full_frame(im: Image.Image) -> Image.Image:
    """整帧参考实现：与 ``unblend_v3`` 同算法，但**不裁剪**（钉住裁剪等价性）。"""
    rectified = fp.unblend_rgba(im)
    neighbor, reach = fp.neighbor_color_map(rectified)
    mask = fp.pull_mask(rectified, neighbor, reach)
    rectified = fp.pull_white_contamination(rectified, neighbor, mask)
    return fp.erode_alpha(rectified)


# ---------------------------------------------------------------- 夹具自证
def test_dark_fixture_haze_would_keep_white_under_v2():
    """夹具自证：v2 对这些像素**一点办法都没有**（存值≈白 ⇒ 除白后仍是白）。

    没有这条，"v3 把白边修好了"就可能只是夹具本来就没白边。a=13 的一圈 v2 连碰都不
    碰（< UNBLEND_MIN_ALPHA），a=40/100 的两圈除白后仍 ≈ 236/247 色阶。
    """
    rectified = fp.unblend_rgba(_frame(DARK))

    for x, y, distance in _ring_pixels():
        before = rectified.getpixel((x, y))
        assert before[3] == RING_ALPHAS[distance - 1]
        assert _luma(before[:3]) > 200, (x, y, before)


# ---------------------------------------------------------------- 收色：两类夹具
def test_dark_character_white_haze_is_pulled_to_neighbor_color():
    """深色角色 + 白底污染 ⇒ 边缘像素被拉向深色（亮度显著下降）。

    判据两侧：污染像素 ≈ 252 色阶，邻居（不透明核）≈ 30 色阶，差远超
    ``UNBLEND_EXCESS_LUMA``；收完颜色就是邻居色，所以逐像素等于 ``DARK``。
    """
    rectified = fp.unblend_rgba(_frame(DARK))
    out = fp.unblend_v3(_frame(DARK))

    pulled = 0
    for x, y, distance in _ring_pixels():
        before = rectified.getpixel((x, y))
        after = out.getpixel((x, y))
        assert _luma(before[:3]) - _luma(after[:3]) > 60, (x, y, before, after)
        assert max(abs(a - b) for a, b in zip(after[:3], DARK)) <= 2, (x, y, after)
        pulled += 1
    assert pulled == len(_ring_pixels())
    # 核本身不被收色（判据第一项就不成立：a=255 在带外）
    assert out.getpixel(((CORE_LO + CORE_HI) // 2, (CORE_LO + CORE_HI) // 2))[:3] == DARK


def test_white_art_edge_is_left_alone():
    """白色艺术品 + 白色 AA 边 ⇒ 颜色基本不变（±8 色阶），只有 alpha 被微缩。

    这是 v2 分不开、v3 必须不伤的那一类：邻居本身是白的，亮度差 ≈ 0。
    """
    source = _frame(WHITE)
    rectified = fp.unblend_rgba(source)
    out = fp.unblend_v3(source)

    for x, y, _distance_ in _ring_pixels():
        before = rectified.getpixel((x, y))
        after = out.getpixel((x, y))
        assert max(abs(a - b) for a, b in zip(after[:3], before[:3])) <= 8, (x, y, before, after)
    # 微缩仍然生效（这条不是"整帧没动"）：最外圈 alpha 归零
    outer = [(x, y) for x, y, d in _ring_pixels() if d == 3]
    assert all(out.getpixel((x, y))[3] == 0 for x, y in outer)


def test_pull_never_brightens_any_pixel():
    """逐像素不变量：修后**没有任何像素变亮**（亮度差 ≤ 0，更别说 >30）。

    用户口径是"零异常变亮（>30）"；这里断言更强的一阶性质。它是判据方向的直接推论：
    只有"本像素比邻居亮出 30"的像素才会被替换，而替换色就是那个邻居色 ⇒ 新亮度
    = 邻居亮度 < 旧亮度 − 30。夹具里两侧都验（深色与白色）。
    """
    for core in (DARK, WHITE):
        source = _frame(core)
        rectified = fp.unblend_rgba(source)
        out = fp.unblend_v3(source)
        increases = [
            _luma(out.getpixel((x, y))[:3]) - _luma(rectified.getpixel((x, y))[:3])
            for x in range(SIZE) for y in range(SIZE)
            if rectified.getpixel((x, y))[3] > 0
        ]
        assert max(increases) <= 0, core
        assert sum(1 for value in increases if value > 30) == 0, core


def test_pull_runs_on_rectified_pixels_not_stored_ones():
    """顺序契约：判据必须拿**反解后**的颜色算，不是拿阶段一存下来的白底混合值。

    夹具：核 (150,150,150)（阈值 180），污染圈的真色是 (170,170,170)——反解后 170
    **不该**被收（170 ≤ 180）；而它的存值 215 高于阈值——若先收色再反解，这一圈会被
    误判成污染并收成 150。断言它保持 ≈170。
    """
    core = (150, 150, 150)
    true_color = (170, 170, 170)
    alpha = 120
    stored = tuple(round(c * alpha / 255 + 255 * (1 - alpha / 255)) for c in true_color)
    assert _luma(stored) > _luma(core) + fp.UNBLEND_EXCESS_LUMA     # 夹具自证：存值越线
    assert _luma(true_color) <= _luma(core) + fp.UNBLEND_EXCESS_LUMA  # 反解值不越线

    payload = bytearray()
    for y in range(SIZE):
        for x in range(SIZE):
            distance = _distance(x, y)
            pixel = ((*core, 255) if distance == 0
                     else (*stored, alpha) if distance == 1 else (0, 0, 0, 0))
            payload += bytes(pixel)
    source = Image.frombytes("RGBA", (SIZE, SIZE), bytes(payload))

    out = fp.unblend_v3(source)

    for x, y, distance in _ring_pixels(ring=1):
        assert max(abs(a - b) for a, b in zip(out.getpixel((x, y))[:3], true_color)) <= 2


# ---------------------------------------------------------------- alpha 微缩
def test_erode_shrinks_outer_ring_and_keeps_interior():
    """微缩：轮廓外圈 alpha 下降、内部 a=255 区不变，且 alpha **只降不升**。"""
    source = _frame(DARK)
    rectified = fp.unblend_rgba(source)
    out = fp.unblend_v3(source)

    for x in range(SIZE):
        for y in range(SIZE):
            before = rectified.getpixel((x, y))[3]
            after = out.getpixel((x, y))[3]
            assert after <= before, (x, y, before, after)
    # 最外圈（贴着全透明背景）被收掉一整圈
    assert all(out.getpixel((x, y))[3] == 0
               for x, y, d in _ring_pixels() if d == 3)
    # 内部（离核边界 ≥2 圈）保持 255：微缩只啃边界
    for x in range(CORE_LO + 2, CORE_HI - 1):
        for y in range(CORE_LO + 2, CORE_HI - 1):
            assert out.getpixel((x, y))[3] == 255, (x, y)


def test_erode_alpha_unit_and_zero_is_noop():
    """``erode_alpha`` 的单元口径：3x3 取最小、边界按 edge pad、``px<=0`` 原样返回。"""
    payload = bytearray()
    for y in range(3):
        for x in range(3):
            payload += bytes((10, 20, 30, 200 if (x, y) == (1, 1) else 0))
    source = Image.frombytes("RGBA", (3, 3), bytes(payload))

    eroded = fp.erode_alpha(source)

    assert all(eroded.getpixel((x, y))[3] == 0 for x in range(3) for y in range(3))
    assert [eroded.getpixel((x, 0))[:3] for x in range(3)] == [(10, 20, 30)] * 3
    assert fp.erode_alpha(source, px=0) is source


# ---------------------------------------------------------------- 邻域估计
def test_neighbor_color_map_marks_reach_and_keeps_unknown_out():
    """邻居估计：可达掩膜 = "窗口里真有不透明像素"，未知像素（0 兜底）绝不被当作黑。

    远到 5 圈之外（切比雪夫距离 > ``UNBLEND_DIFFUSE_ROUNDS``）的像素必须
    ``reach == 0``——否则它的邻居色是 0（黑），收色会凭空把像素收成黑色。
    """
    source = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    source.paste(Image.new("RGBA", (4, 4), (200, 60, 20, 255)), (2, 2))
    rectified = fp.unblend_rgba(source)

    neighbor, reach = fp.neighbor_color_map(rectified)

    assert reach.getpixel((10, 10)) == 255          # 距方块 (2..5) 的边界 5 圈内
    # 估计值是逐轮归一化均值 + 8 位取整，几个色阶的余量（不是逐位精确）
    assert max(abs(a - b) for a, b in zip(neighbor.getpixel((10, 10)), (200, 60, 20))) <= 4
    assert reach.getpixel((20, 20)) == 0            # 远到扩散走不到
    assert neighbor.getpixel((20, 20)) == (0, 0, 0)  # 0 兜底，靠 reach 挡住


def test_pull_band_edges_are_exact():
    """收色带的两个端点：``a = 3`` 收、``a = 2`` 不收、``a = 127`` 收、``a = 128`` 不收。"""
    alphas = list(range(256))
    payload = bytearray()
    for alpha in alphas:
        payload += bytes((255, 255, 255, alpha))
    band = fp.pull_band(Image.frombytes("RGBA", (256, 1), bytes(payload)).split()[3])

    values = [band.getpixel((alpha, 0)) for alpha in alphas]

    assert values[:fp.UNBLEND_PULL_MIN_ALPHA] == [0] * fp.UNBLEND_PULL_MIN_ALPHA
    assert values[fp.UNBLEND_PULL_MIN_ALPHA:fp.UNBLEND_PULL_MAX_ALPHA] == \
        [255] * (fp.UNBLEND_PULL_MAX_ALPHA - fp.UNBLEND_PULL_MIN_ALPHA)
    assert values[fp.UNBLEND_PULL_MAX_ALPHA:] == [0] * (256 - fp.UNBLEND_PULL_MAX_ALPHA)


def test_pull_replaces_color_and_leaves_alpha_bit_exact():
    """``pull_white_contamination`` 是纯函数：只换颜色、alpha 一位不动、入参不改。"""
    source = _frame(DARK)
    rectified = fp.unblend_rgba(source)
    neighbor, reach = fp.neighbor_color_map(rectified)
    mask = fp.pull_mask(rectified, neighbor, reach)
    before = rectified.copy()

    out = fp.pull_white_contamination(rectified, neighbor, mask)

    assert list(rectified.get_flattened_data()) == list(before.get_flattened_data())
    assert list(out.split()[3].get_flattened_data()) == \
        list(rectified.split()[3].get_flattened_data())
    assert mask.getbbox() is not None                                 # 确实有像素要收
    assert fp.pull_white_contamination(rectified, neighbor,
                                       Image.new("L", rectified.size, 0)) is rectified


# ---------------------------------------------------------------- 裁剪等价 / 全帧
@pytest.mark.parametrize("size,core", [(64, (150, 150, 150)), (24, (24, 28, 60))])
def test_crop_path_matches_full_frame_reference(size, core):
    """裁剪是纯性能手段：``unblend_v3`` 与**整帧**参考实现逐像素相同。

    收色带只占整帧一角时（这个夹具），扩散在裁剪区里跑；带贴着图像边缘时（小尺寸
    夹具）裁剪区等于整帧。两种形状都必须与整帧算法完全等价——否则"省时间"就变成了
    "换结果"，而结果是要拿去覆盖整批素材的。
    """
    source = _frame(core, size=size, ring=2)

    assert list(fp.unblend_v3(source).get_flattened_data()) == \
        list(_reference_full_frame(source).get_flattened_data())


def test_fully_opaque_frame_is_untouched():
    """没有收色带（全不透明）的帧：颜色与 alpha 都不动（微缩在 255 区里是恒等）。"""
    source = Image.new("RGBA", (16, 16), (30, 180, 90, 255))

    out = fp.unblend_v3(source)

    assert list(out.get_flattened_data()) == list(source.get_flattened_data())


def test_throughput_stays_within_budget():
    """640×360 单帧 v3（反解 + 收色 + 微缩，不含编码）守住 ≤200ms 量级。

    实测（本机 Python 3.13 / Pillow 12.2，真素材帧）：**41ms**；阶段二整帧含 Q70
    重编码与逐帧读写 **≈94ms**（预算 150ms）。这条只挡数量级回归（例如把裁剪去掉、
    或把 C 级滤波换成逐像素 Python）——所以预算给到实测的 4-5 倍，慢 CI 上也不抖。

    夹具形状按真素材比例做：核占画面中部，收色带包围盒约占整帧 1/3（实测真素材
    27-29%），因此这条测的是**带裁剪的正常路径**，不是"整帧都是收色带"的最坏形状。
    """
    size, core = (640, 360), 96
    payload = bytearray()
    step = 6
    for y in range(size[1]):
        for x in range(size[0]):
            distance = max(abs(x - size[0] // 2), abs(y - size[1] // 2)) - core
            if distance <= 0:
                payload += bytes((*DARK, 255))
            elif distance <= step:
                payload += bytes((*HAZE, max(3, 128 - distance * 20)))
            else:
                payload += bytes((0, 0, 0, 0))
    source = Image.frombytes("RGBA", size, bytes(payload))

    fp.unblend_v3(source)                       # 预热（首次调用要载 PIL/建常量）
    started = time.perf_counter()
    for _ in range(3):
        fp.unblend_v3(source)
    per_frame_ms = (time.perf_counter() - started) / 3 * 1000

    assert per_frame_ms < 200, f"单帧 v3 {per_frame_ms:.1f}ms，超出预算量级"
