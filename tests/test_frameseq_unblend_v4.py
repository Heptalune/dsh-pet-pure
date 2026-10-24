# -*- coding: utf-8 -*-
"""白底反解 v4「镂空还原」的单测（收色之后、alpha 微缩之前的那一步）。

**⚠ 工序现状：默认挂起**（``HOLE_RESTORE_ENABLED = False``）。父代理目视裁定
确认 v4 在实机上**误切了白色鲸鳍**：它的判据走到"发色包围的灰白实心块"这一步就
再也分不下去——抠图残留与白色艺术品（鲸鳍、蕾丝）在这个形状特征上**完全同构**
（亮、低饱和、被蓝调发色包围、贴着半透明 AA 边）。等用户给出精确的残留位置后
重标定再开启。

因此本文件分两半：

- **判据本身的用例**（下面 17 条）用模块级 autouse fixture **显式开开关**再跑——
  它们钉的是"开关打开时这套判据的行为"，与工序当前是否挂起无关；
- **挂起语义的用例**（最后 2 条）用 ``hole_restore_off`` fixture 把开关关回去，
  钉"默认关闭时全流程一位不动"与"开关往返语义"。

**判据三层**（开关打开时，全中才切透）：

1. 候选 ``hole_candidate_mask``：``a >= 250`` 且亮度 > ``HOLE_LUMA_MIN``
   且通道极差 < ``HOLE_RANGE_MAX`` 且 ``r - b <= HOLE_WARM_MAX``；
2. 包围 ``surround_mask``：``HOLE_WINDOW × HOLE_WINDOW`` 邻域里**蓝调发色**
   占比 >= ``HOLE_SURROUND_MIN``；
3. 结构（``_hole_cut_mask``）：核心尺寸 >= ``HOLE_SIZE_MIN``、核心里有像素落在
   ``a < 250`` 像素的 ``HOLE_EDGE_MAX`` 邻域内（贴缝证据）、块的包围带里不透明
   像素占比 >= ``HOLE_RING_OPAQUE_MIN``（实心包围证据）、且切出去的那一整块
   （候选掩膜的连通块）<= ``HOLE_SIZE_MAX``。

夹具按每一层各自的两侧成对写；保护清单（眼睛高光 / 白蕾丝 / 白围裙 / 皮肤）
**错杀即失败**，所以它们每一个都有自己的用例——而**白色鲸鳍**正是这一版判据拦
不住的那一类（``_white_fin`` 夹具），它只在挂起语义那两条里被断言。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image

from pet import frameseq_provision as fp

# 仓库内真实素材（git 跟踪）：真素材断言从它现场解一帧，不依赖 .scratch 临时产物
REAL_WEBM = (Path(__file__).resolve().parent.parent
             / "assets" / "characters" / "shenshen" / "videos"
             / "idle" / "待机呼吸休闲.webm")

SIZE = (160, 120)

# 蓝调发色（真素材实测 (50,56,103) 一档）：b > r + 20 且 b > 90
HAIR = (50, 56, 103)
# 墙色残留（真素材实测 (198,204,221)、(216,210,215) 一档）：亮、低饱和、不暖
WALL = (200, 205, 215)
# 皮肤（真素材实测 (243,228,218)）：亮、极差 25（仍在 HOLE_RANGE_MAX 内）、
# 但 r - b = 25 越线 ⇒ 必须靠暖调口径挡
SKIN = (243, 228, 218)
HIGHLIGHT = (252, 252, 255)
IRIS = (36, 44, 120)                       # 深蓝虹膜：b > r + 20 且 b > 90
# 白色艺术品（鲸鳍 / 白蕾丝）：与墙色残留同一个亮度档、同样低饱和
WHITE_ART = (248, 250, 252)


@pytest.fixture(autouse=True)
def _hole_restore_on(monkeypatch):
    """判据类用例一律显式开开关（工序默认挂起；把"挂起"与"判据"解耦）。

    autouse ⇒ 在本模块每个用例之前生效；挂起语义那两条再用 ``hole_restore_off``
    覆盖回去（同 scope 下 autouse fixture 先执行，顺序确定）。
    """
    monkeypatch.setattr(fp, "HOLE_RESTORE_ENABLED", True)


@pytest.fixture
def hole_restore_off(monkeypatch):
    """把工序关回默认挂起态（product default，见 ``HOLE_RESTORE_ENABLED``）。"""
    monkeypatch.setattr(fp, "HOLE_RESTORE_ENABLED", False)


def _blank(size=SIZE):
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _fill(im, box, rgba):
    im.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), rgba), box[:2])
    return im


def _hair_with_gap(*, block=(70, 50, 96, 62), gap=True, block_color=WALL,
                   size=SIZE):
    """蓝调发色块 + 半透明发缝 + 缝里残留的实心块。

    ``gap=False`` 不给半透明缝边——用来钉住"贴缝证据"这一层真的是判据之一。
    """
    im = _fill(_blank(size), (20, 20, size[0] - 20, size[1] - 20), (*HAIR, 255))
    if gap:
        # 发缝：贴着残留块的一条 1px 半透明缝（matting 在这里生效了一半）
        _fill(im, (block[0] - 10, block[1], block[0] - 9, block[3]),
              (*HAIR, 120))
    _fill(im, block, (*block_color, 255))
    return im


def _white_fin(size=SIZE):
    """白色鲸鳍：**和残留块逐像素同形**的白色实心块 + 贴着它的半透明 AA 边。

    这正是 v4 实机误切的那一类（也是工序挂起的原因）：判据眼里它与
    ``_hair_with_gap()`` 没有区别。
    """
    return _hair_with_gap(block=(70, 50, 96, 62), block_color=WHITE_ART,
                          size=size)


def _eye_highlight(size=SIZE):
    """眼睛高光：白点 + 深蓝虹膜 + 皮肤，整块埋在实心区里（离半透明边界 > 40px）。"""
    im = _blank(size)
    _fill(im, (40, 40, 120, 90), (*SKIN, 255))          # 脸（皮肤色实心块）
    _fill(im, (66, 52, 94, 78), (*IRIS, 255))           # 虹膜（蓝调 ⇒ 过包围判定）
    _fill(im, (74, 58, 86, 70), (*HIGHLIGHT, 255))      # 高光
    return im


def _lace_band(size=(200, 160)):
    """白蕾丝带：2px 细线织成的**单个**巨大连通网（远超 ``HOLE_SIZE_MAX``）。

    每个线像素的 15×15 窗口里 80% 以上是发色 ⇒ 逐像素包围判定会过——这正是用户
    点名的陷阱①，唯一拦住它的是尺寸上限。
    """
    im = _fill(_blank(size), (10, 10, size[0] - 10, size[1] - 10), (*HAIR, 255))
    for y in range(20, size[1] - 20, 4):                 # 横线
        _fill(im, (20, y, size[0] - 20, y + 2), (*HIGHLIGHT, 255))
    for x in range(20, size[0] - 20, 24):                # 竖线：把横线连成一块
        _fill(im, (x, 20, x + 2, size[1] - 22), (*HIGHLIGHT, 255))
    return im


def _skin_patch(size=SIZE):
    """皮肤色实心块 + 半透明缝边：暖调 ⇒ 候选以外（第一层就拦住）。"""
    return _hair_with_gap(block=(70, 50, 96, 62), block_color=SKIN, size=size)


def _chain_without_holes(im):
    """无镂空工序的参考产物：反解 → 收色 → 微缩 → alpha 底板清理。

    与 ``unblend_v3`` 同口径（当前档 v5：链尾多一步 ``floor_faint_alpha`` 清 ``a == 1``），
    只少了镂空还原那一步——挂起语义要证的正是"少了它，逐位相同"。
    """
    rectified = fp.unblend_rgba(im)
    box = fp.pull_band(rectified.split()[3]).getbbox()
    if box is not None:
        rectified = fp._pull_region(
            rectified, fp._pad_box(box, rectified.size, fp.UNBLEND_DIFFUSE_ROUNDS))
    return fp.floor_faint_alpha(fp.erode_alpha(rectified))


def _real_frame():
    """真素材（idle 待机呼吸休闲）首帧 RGBA；解码链路不可用时返回 None。

    用 imageio_ffmpeg 自带的 exe 与产品同一条解码链（``-c:v libvpx-vp9``、
    ``bgra`` 直通），因此不需要系统 ffmpeg、也不会静默丢掉 VP9 的 alpha。
    """
    if not REAL_WEBM.exists():
        return None
    try:
        import imageio_ffmpeg
        gen = imageio_ffmpeg.read_frames(
            str(REAL_WEBM), pix_fmt="bgra", bits_per_pixel=32,
            input_params=["-c:v", "libvpx-vp9", "-threads", "1"])
    except Exception:
        return None
    try:
        meta = next(gen)
        raw = next(gen)
    except Exception:
        return None
    finally:
        try:
            gen.close()
        except Exception:
            pass
    return Image.frombytes("RGBA", tuple(meta["size"]), raw,
                           "raw", "BGRA").convert("RGBA")


def _alpha_region(im, box):
    """``box`` 内 alpha 的取值分布（用于断言"整块被切"或"一位没动"）。"""
    return list(im.crop(box).split()[3].get_flattened_data())


# ---------------------------------------------------------------- 第一层：候选
def test_candidate_mask_hits_wall_color_and_skips_blue_warm_and_transparent():
    """候选掩膜四个两侧：墙色中；蓝调发色、暖调皮肤、透明都不中。"""
    im = _blank((40, 20))
    _fill(im, (0, 0, 10, 20), (*HAIR, 255))
    _fill(im, (10, 0, 20, 20), (*WALL, 255))
    _fill(im, (20, 0, 30, 20), (*SKIN, 255))
    _fill(im, (30, 0, 40, 20), (0, 0, 0, 0))

    mask = fp.hole_candidate_mask(im)

    assert [mask.getpixel((x, 10)) for x in (5, 15, 25, 35)] == [0, 255, 0, 0]


def test_candidate_mask_rejects_semi_transparent_and_dark_and_saturated():
    """``a < 250``（收色的地盘）、亮度不足、高饱和都不是候选。"""
    im = _blank((40, 10))
    _fill(im, (0, 0, 10, 10), (*WALL, 249))
    _fill(im, (10, 0, 20, 10), (*WALL, 255))
    _fill(im, (20, 0, 30, 10), (40, 45, 50, 255))
    _fill(im, (30, 0, 40, 10), (250, 200, 150, 255))

    mask = fp.hole_candidate_mask(im)

    assert [mask.getpixel((x, 5)) for x in (5, 15, 25, 35)] == [0, 255, 0, 0]


def test_candidate_mask_boundaries_are_exact():
    """三个阈值的端点：亮度 ``> 150``、极差 ``< 30``、``r - b <= 12``（含端点）。"""
    im = _blank((4, 4))
    _fill(im, (0, 0, 1, 4), (150, 150, 150, 255))     # 亮度恰好 150 ⇒ 不收
    _fill(im, (1, 0, 2, 4), (151, 151, 151, 255))     # 151 ⇒ 收
    _fill(im, (2, 0, 3, 4), (151, 160, 180, 255))     # 极差 29 ⇒ 收
    _fill(im, (3, 0, 4, 4), (150, 160, 180, 255))     # 极差 30 ⇒ 不收

    mask = fp.hole_candidate_mask(im)

    assert [mask.getpixel((x, 2)) for x in range(4)] == [0, 255, 255, 0]
    warm = _blank((2, 4))
    _fill(warm, (0, 0, 1, 4), (200, 200, 188, 255))   # r - b = 12 ⇒ 收（含端点）
    _fill(warm, (1, 0, 2, 4), (200, 200, 187, 255))   # r - b = 13 ⇒ 不收
    assert [fp.hole_candidate_mask(warm).getpixel((x, 2)) for x in range(2)] == \
        [255, 0]


# ---------------------------------------------------------------- 第二层：包围
def test_surround_mask_counts_blue_hair_share_in_window():
    """包围掩膜：15×15 窗口里蓝调发色占比过 ``HOLE_SURROUND_MIN`` 才置位。"""
    mask = Image.new("L", (60, 40), 255)
    for y in range(10, 30):                       # 中央 20×20 不是发色
        for x in range(20, 40):
            mask.putpixel((x, y), 0)

    out = fp.surround_mask(mask)

    assert out.getpixel((2, 2)) == 255            # 全发色区
    assert out.getpixel((30, 20)) == 0            # 非发色中心：窗口占比不足


def test_hair_blue_mask_needs_all_three_terms():
    """发色掩膜三条件：蓝调（b > r + 20）、够蓝（b > 90）、且实心（a >= 250）。"""
    im = _blank((3, 4))
    _fill(im, (0, 0, 1, 4), (*HAIR, 255))         # (50,56,103)：b - r = 53 ✓
    _fill(im, (1, 0, 2, 4), (80, 80, 95, 255))    # b - r = 15 ✗
    _fill(im, (2, 0, 3, 4), (*HAIR, 249))         # 半透明 ✗

    mask = fp.hair_blue_mask(im)

    assert [mask.getpixel((x, 2)) for x in range(3)] == [255, 0, 0]


def test_white_block_away_from_hair_is_untouched():
    """贴着透明背景的白块（白围裙下摆的形状）一位不动：邻域发色占比不足。"""
    im = _fill(_blank(), (60, 20, 100, 60), (*WALL, 255))
    before = fp.unblend_rgba(im)

    out = fp.restore_hair_holes(before)

    assert list(out.get_flattened_data()) == list(before.get_flattened_data())


# ---------------------------------------------------------------- 切透：残留块
def test_wall_block_in_hair_gap_is_cut_transparent():
    """发色包围 + 贴缝的灰白实心块 ⇒ **整块** alpha 归零（v4 存在的理由）。"""
    source = _hair_with_gap()
    rectified = fp.unblend_rgba(source)
    assert _alpha_region(rectified, (70, 50, 96, 62)) == [255] * (26 * 12)

    out = fp.unblend_v3(source)

    assert _alpha_region(out, (70, 50, 96, 62)) == [0] * (26 * 12)


def test_cut_is_block_level_not_pixel_level():
    """切出去的是**整块**，不是逐像素命中的那一圈。

    包围判定对块内部天然不成立（15×15 窗口被块自己占满），只按逐像素切会留下
    一块灰芯。夹具的块是 26×12，逐像素过线的只有四角附近 ⇒ 断言整块归零。
    """
    source = _hair_with_gap()
    rectified = fp.unblend_rgba(source)

    out = fp.restore_hair_holes(rectified)

    assert _alpha_region(out, (70, 50, 96, 62)) == [0] * (26 * 12)
    # 块外的发色一位没动
    assert out.getpixel((30, 30))[3] == 255
    assert out.getpixel((120, 90))[3] == 255


def test_block_without_gap_evidence_is_left_alone():
    """没有贴缝证据（整块埋在实心发色里、离半透明像素 > ``HOLE_EDGE_MAX``）⇒ 不动。

    这条钉住第三层的贴缝口径真的在起作用：否则第 1+2 层就能切掉一切埋在头发里
    的浅色块——眼睛高光、白色配件都在这个形状里。
    """
    source = _hair_with_gap(gap=False)
    rectified = fp.unblend_rgba(source)

    out = fp.restore_hair_holes(rectified)

    assert _alpha_region(out, (70, 50, 96, 62)) == [255] * (26 * 12)


def test_cut_only_touches_alpha():
    """镂空只动 alpha：同一帧的 RGB 逐像素不变。"""
    rectified = fp.unblend_rgba(_hair_with_gap())

    out = fp.restore_hair_holes(rectified)

    assert list(out.convert("RGB").get_flattened_data()) == \
        list(rectified.convert("RGB").get_flattened_data())
    # 确实切了：块内 alpha 归零，块外一个像素没变
    alpha = out.split()[3]
    assert _alpha_region(out, (70, 50, 96, 62)) == [0] * (26 * 12)
    assert alpha.histogram()[0] == rectified.split()[3].histogram()[0] + 26 * 12


def test_restore_is_noop_without_candidates():
    """没有候选块（全发色）时**原对象返回**：稳态帧零成本路径。"""
    pure = _fill(_blank(), (20, 20, 140, 100), (*HAIR, 255))

    assert fp.restore_hair_holes(pure) is pure


def test_restore_rejects_non_rgba():
    """模式不符时抛错（与 v3 各步同口径，绝不静默算出个结果）。"""
    with pytest.raises(ValueError):
        fp.restore_hair_holes(Image.new("RGB", (4, 4), (0, 0, 0)))
    with pytest.raises(ValueError):
        fp.hole_candidate_mask(Image.new("RGB", (4, 4), (0, 0, 0)))
    with pytest.raises(ValueError):
        fp.hair_blue_mask(Image.new("RGB", (4, 4), (0, 0, 0)))
    with pytest.raises(ValueError):
        fp.surround_mask(Image.new("RGB", (4, 4), (0, 0, 0)))


# ---------------------------------------------------------------- 保护清单
def test_eye_highlight_is_untouched():
    """眼睛高光（白点 + 深蓝虹膜，虹膜过蓝调判定）⇒ 贴缝口径拦住，一位不动。"""
    before = fp.unblend_rgba(_eye_highlight())

    out = fp.restore_hair_holes(before)

    assert list(out.get_flattened_data()) == list(before.get_flattened_data())


def test_lace_band_is_untouched():
    """白蕾丝带（细线织成的巨大连通网）⇒ 尺寸上限拦住，一位不动。"""
    before = fp.unblend_rgba(_lace_band())

    out = fp.restore_hair_holes(before)

    assert list(out.get_flattened_data()) == list(before.get_flattened_data())
    # 自证：这网确实是一整块（否则"尺寸上限拦住了"就是空话）
    whole = fp.hole_candidate_mask(before)
    assert whole.histogram()[255] > fp.HOLE_SIZE_MAX


def test_skin_patch_is_untouched():
    """皮肤色实心块 ⇒ 暖调口径拦住（第一层），一位不动。"""
    before = fp.unblend_rgba(_skin_patch())

    out = fp.restore_hair_holes(before)

    assert list(out.get_flattened_data()) == list(before.get_flattened_data())


def test_protected_shapes_survive_inside_unblend_v3():
    """保护清单在工序位置上也成立：``unblend_v3`` 不会把高光/蕾丝切掉。"""
    for source in (_eye_highlight(), _lace_band()):
        out = fp.unblend_v3(source)
        rectified = fp.unblend_rgba(source)
        assert list(out.get_flattened_data()) == \
            list(fp.erode_alpha(rectified).get_flattened_data())


def test_throughput_stays_within_budget():
    """镂空这一步的单帧增量守住 ≤60ms（真素材实测见报告）。

    夹具按真素材的候选规模做（一个 ~150px 的残留块 + 周边发色），走的是"有候选"
    的正常路径；给 3 倍余量，慢 CI 上也不抖。
    """
    source = _hair_with_gap(size=(640, 360), block=(300, 170, 336, 180))
    rectified = fp.unblend_rgba(source)

    fp.restore_hair_holes(rectified)                       # 预热
    started = time.perf_counter()
    for _ in range(3):
        fp.restore_hair_holes(rectified)
    per_call_ms = (time.perf_counter() - started) / 3 * 1000

    assert per_call_ms < 60, f"单帧镂空 {per_call_ms:.1f}ms，超出预算"


# ------------------------------------------------- 挂起语义（工序默认关闭）
def _real_frame_rectified():
    """真素材首帧的「反解 → 收色」产物（开关打开时``restore_hair_holes`` 的输入）。"""
    frame = _real_frame()
    if frame is None:
        return None
    rectified = fp.unblend_rgba(frame)
    box = fp.pull_band(rectified.split()[3]).getbbox()
    if box is not None:
        rectified = fp._pull_region(
            rectified, fp._pad_box(box, rectified.size, fp.UNBLEND_DIFFUSE_ROUNDS))
    return frame, rectified


def test_disabled_by_default_leaves_solid_blocks_and_white_art_alone(
        hole_restore_off, monkeypatch):
    """**默认挂起**时 ``unblend_v3`` 全流程一位不动（合成夹具 + 真素材帧）。

    这是误切事故的防回归闸：挂起态的产物必须与「反解 → 收色 → 微缩」**逐位
    相同**——不是"目标块没被切"，而是**全帧每个像素**都没被这一步碰过。覆盖三类
    会被判据命中的形状：

    - ``_hair_with_gap()``：开关打开时**会**被切透的灰白实心块；
    - ``_white_fin()``：白色鲸鳍——与上一条**逐像素同形**，实机被误切的就是它；
    - ``_lace_band()``：白蕾丝网。

    末尾用仓库内真素材（idle 首帧）跑全帧逐位相同，再**反空过自证**：同一个夹具
    与同一帧在开关打开时确实会被改动（否则"挂起生效"可能只是"本来就没候选"）。
    """
    for source in (_hair_with_gap(), _white_fin(), _lace_band()):
        assert list(fp.unblend_v3(source).get_flattened_data()) == \
            list(_chain_without_holes(source).get_flattened_data())

    real = _real_frame_rectified()
    if real is None:
        pytest.skip("真素材帧不可用（缺 imageio_ffmpeg 或素材）")
    frame, rectified = real
    assert list(fp.unblend_v3(frame).get_flattened_data()) == \
        list(_chain_without_holes(frame).get_flattened_data())

    monkeypatch.setattr(fp, "HOLE_RESTORE_ENABLED", True)
    # 自证：灰白块与**同形的白色鲸鳍**在开关打开时都会被切——后者正是挂起的原因
    for source in (_hair_with_gap(), _white_fin()):
        assert _alpha_region(fp.unblend_v3(source), (70, 50, 96, 62)) == \
            [0] * (26 * 12)
    assert fp.restore_hair_holes(rectified) is not rectified


def test_switch_round_trip(hole_restore_off, monkeypatch):
    """开关往返语义：关（默认）直通原对象 → 开确实动 → 再关又直通。

    "关的时候不只是另一位像素都没变，而是**连对象都不新建**"是挂起态的零开销
    契约：调用方（``unblend_v3``）不做任何特判，靠的就是这一步自己直通。
    """
    rectified = _chain_without_holes(_hair_with_gap())

    assert fp.HOLE_RESTORE_ENABLED is False          # 产品默认值就是挂起
    assert fp.restore_hair_holes(rectified) is rectified

    monkeypatch.setattr(fp, "HOLE_RESTORE_ENABLED", True)
    enabled = fp.restore_hair_holes(rectified)
    assert enabled is not rectified                  # 开：确实产出了新帧
    assert _alpha_region(enabled, (70, 50, 96, 62)) == [0] * (26 * 12)

    monkeypatch.setattr(fp, "HOLE_RESTORE_ENABLED", False)
    assert fp.restore_hair_holes(rectified) is rectified   # 回关：直通如初
