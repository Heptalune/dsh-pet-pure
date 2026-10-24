# -*- coding: utf-8 -*-
"""白底反解（unblend）+ 阶段二编码的单测（W1/W2 两段式管线第二步）。

素材在白底上抠图 ⇒ 半透明像素的存值 = ``角色色 × a + 255 × (1-a)``；阶段二
（``frameseq_provision.unblend_frames_in_place``）用这一步把它按 alpha 除回原色，
再编码 Q70。本文件只测**纯函数**口径（``unblend_rgba``）——精确的数学断言放在
这里，管线级的有损噪声留在 ``test_frameseq_provision.py``。

覆盖：
- 反解后颜色回到角色色（alpha ≥ 64 时 ±2 色阶；更低的 alpha 用解析上界
  ``255×0.5/a`` 收口，见 ``unblend_rgba`` 的精度说明）；
- **alpha 逐位不变**（A band 原样并回）；
- 白底合成等价：反解结果重新按白底合成，与原图逐像素相差 ≤1 色阶；
- 两端不反解：``a < UNBLEND_MIN_ALPHA``（≈0.08×255）与 ``a >= UNBLEND_MAX_ALPHA``
  的像素一个色阶都不动；
- 反解只会**变暗**（数学上 ``rectified ≤ stored``）：无像素异常变亮（用户实测
  241 帧全段"零异常变亮"的那个口径的纯函数版本）；
- 非 RGBA 输入按 ``ValueError`` 收口（不静默算错），单帧 640×360 的吞吐守住
  ≤15ms 量级（宽预算，别在慢 CI 上抖）。
"""
from __future__ import annotations

import time

import pytest
from PIL import Image

from pet import frameseq_provision as fp


def _blend(rgb: tuple[int, int, int], alpha: int) -> tuple[int, int, int]:
    """白底合成（素材的存值形态）：角色色 × a + 255 × (1-a)。"""
    return tuple(round(c * alpha / 255 + 255 * (1 - alpha / 255)) for c in rgb)


def _reference_rectified(stored: tuple[int, int, int], alpha: int) -> tuple[int, int, int]:
    """参考实现：``clip((stored - 255×(1-a)) / a, 0, 255)``（两端不反解）。

    入参是**白底混合后的存值**（``_blend`` 的输出），出参是反解结果。与产品实现
    同公式但独立写法（这里用浮点 + 显式裁剪），避免"两边抄同一段错代码"。
    """
    if not (fp.UNBLEND_MIN_ALPHA <= alpha < fp.UNBLEND_MAX_ALPHA):
        return stored
    out = []
    for value in stored:
        rectified = (value - 255 * (1 - alpha / 255)) / (alpha / 255)
        out.append(max(0, min(255, round(rectified))))
    return tuple(out)


def _column(*, alphas, character) -> Image.Image:
    """一行像素：alpha 由 ``alphas`` 给定，颜色是 ``character`` 的白底混合存值。"""
    payload = bytearray()
    for alpha in alphas:
        payload += bytes((*_blend(character, alpha), alpha))
    return Image.frombytes("RGBA", (len(alphas), 1), bytes(payload))


def _white_composite(rgba, alpha: int) -> tuple[int, int, int]:
    """把反解结果按**白底**重新合成（观众看到的像素）。"""
    return tuple(round(c * alpha / 255 + 255 * (1 - alpha / 255)) for c in rgba[:3])


CHARACTERS = ((200, 100, 40), (0, 0, 0), (255, 255, 255), (17, 240, 128))


# ---------------------------------------------------------------- 反解正确性
@pytest.mark.parametrize("character", CHARACTERS)
def test_rectifies_back_to_character_color(character):
    """反解后颜色回到角色色：alpha ≥ 64 时 ±2 色阶（更低 alpha 见下一条）。"""
    alphas = list(range(64, fp.UNBLEND_MAX_ALPHA))
    rectified = fp.unblend_rgba(_column(alphas=alphas, character=character))

    for x, alpha in enumerate(alphas):
        got = rectified.getpixel((x, 0))
        want = _reference_rectified(_blend(character, alpha), alpha)
        for channel, (value, expected) in enumerate(zip(got[:3], want)):
            assert abs(value - expected) <= 2, (character, alpha, channel, got)
        for channel, (value, expected) in enumerate(zip(got[:3], character)):
            assert abs(value - expected) <= 2, (character, alpha, channel, got)


def test_low_alpha_error_follows_analytic_bound():
    """a 很小的时候反解误差按 ``255×0.5/a`` 放大（存值量化到整数带来的）。

    这不是"实现不准"，是「存量化的整数 ÷ 小 a」的信息损失：a=21 时上界 ≈6 色阶。
    口径收在解析上界（+1 取整余量）而不是拍一个常数——越透明越不准，也越看不清。
    """
    character = (200, 100, 40)
    alphas = list(range(fp.UNBLEND_MIN_ALPHA, 64))
    rectified = fp.unblend_rgba(_column(alphas=alphas, character=character))

    for x, alpha in enumerate(alphas):
        got = rectified.getpixel((x, 0))
        bound = 255 * 0.5 / alpha + 1
        want = _reference_rectified(_blend(character, alpha), alpha)
        for value, expected in zip(got[:3], want):
            assert abs(value - expected) <= 1, (alpha, got, want)
        for value, expected in zip(got[:3], character):
            assert abs(value - expected) <= bound, (alpha, got)


def test_exact_blend_round_trips_exactly():
    """白底混合存值恰好是整数时，反解逐位回到角色色（没有误差可言）。"""
    rectified = fp.unblend_rgba(_column(alphas=[51, 51], character=(200, 100, 40)))

    assert [rectified.getpixel((x, 0)) for x in range(2)] == [
        (200, 100, 40, 51), (200, 100, 40, 51)]


def test_alpha_is_bit_exact():
    """alpha 一个位都不动：0..255 全扫一遍，A band 与入参逐位相同。"""
    source = _column(alphas=range(256), character=(200, 100, 40))

    rectified = fp.unblend_rgba(source)

    assert list(rectified.split()[3].get_flattened_data()) == \
        list(source.split()[3].get_flattened_data())
    assert rectified.getpixel((0, 0)) == source.getpixel((0, 0))     # a=0


def test_white_composite_of_rectified_matches_original_pixelwise():
    """白底合成等价：反解结果按白底重新合成 = 原图（≤1 色阶，整数除法取整余量）。

    这是"屏幕上观感不变"的形式化口径——换了深色底，白晕才会消失。只在**反解带**
    （21 ≤ a < 250）上成立：带外像素本来就不动，"拿任意存值再合成一次"落到的整数
    与存值本身可以差 1-2 档（两次取整），那条属性由下一条单测守着。
    """
    source = _column(alphas=range(256), character=(200, 100, 40))
    rectified = fp.unblend_rgba(source)

    for alpha in range(fp.UNBLEND_MIN_ALPHA, fp.UNBLEND_MAX_ALPHA):
        before = source.getpixel((alpha, 0))
        after = _white_composite(rectified.getpixel((alpha, 0)), alpha)
        for value, expected in zip(after, before[:3]):
            assert abs(value - expected) <= 1, (alpha, after, before)


def test_pixels_outside_alpha_band_are_untouched():
    """两端不动：``a < 21`` 与 ``a >= 250`` 的像素逐位保持原样。"""
    alphas = list(range(fp.UNBLEND_MIN_ALPHA)) + \
        list(range(fp.UNBLEND_MAX_ALPHA, 256))
    source = _column(alphas=alphas, character=(200, 100, 40))

    rectified = fp.unblend_rgba(source)

    for x, alpha in enumerate(alphas):
        assert rectified.getpixel((x, 0)) == source.getpixel((x, 0)), alpha


def test_rectification_only_darkens_never_brightens():
    """反解只会变暗或不变（数学上 ``rectified ≤ stored``）：没有像素被"异常变亮"。

    用户实测口径是"零异常变亮（>30）"；这里断言更强的不变量（一阶都不变亮）。
    一旦公式方向写反（或对已经反解过的帧再反解一次），这条会立刻变红。
    """
    alphas = list(range(256))
    source = _column(alphas=alphas, character=(200, 100, 40))
    rectified = fp.unblend_rgba(source)

    for alpha in alphas:
        before = source.getpixel((alpha, 0))[:3]
        after = rectified.getpixel((alpha, 0))[:3]
        assert all(new <= old for new, old in zip(after, before)), (alpha, after, before)


def test_rejects_non_rgba_input():
    """非 RGBA 输入按 ValueError 收口（不静默算错、不掀调用方）。"""
    with pytest.raises(ValueError):
        fp.unblend_rgba(Image.new("RGB", (2, 1), (1, 2, 3)))


def test_unblend_is_pure_and_does_not_mutate_input():
    """纯函数：入参一帧都不改（阶段二原地覆盖写盘要靠这条才安全）。"""
    source = _column(alphas=[0, 128, 255], character=(200, 100, 40))
    before = list(source.get_flattened_data())

    fp.unblend_rgba(source)

    assert list(source.get_flattened_data()) == before


def test_frame_throughput_is_within_budget():
    """640×360 单帧反解的吞吐守住 ≤15ms 量级（宽预算：慢 CI 上也不抖）。

    实测（本机，Python 3.13 + Pillow 12.2）：640×360 RGBA 单帧 ≈14ms，其中 3 个
    通道各一次"减-乘-除-convert"的 C 逐像素运算各占 ≈1.1ms。测试只挡数量级回归
    （比如误把 C 路径换成逐像素 Python）。
    """
    width, height = 640, 360
    payload = bytearray()
    for y in range(height):
        for x in range(width):
            alpha = (x * 7 + y * 13) % 256
            payload += bytes((*_blend((200, 100, 40), alpha), alpha))
    source = Image.frombytes("RGBA", (width, height), bytes(payload))

    fp.unblend_rgba(source)                       # 预热（首次调用要载 PIL/建常量）
    started = time.perf_counter()
    for _ in range(3):
        fp.unblend_rgba(source)
    per_frame_ms = (time.perf_counter() - started) / 3 * 1000

    assert per_frame_ms < 60, f"单帧反解 {per_frame_ms:.1f}ms，超出预算量级"
