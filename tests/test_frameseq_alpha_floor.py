# -*- coding: utf-8 -*-
"""白底反解 v5：alpha 底板清理（``floor_faint_alpha``）单测 + 真实素材回归。

**用户症状**：桌宠整个矩形在白色区域里显出一块浅灰（"白底整块色差"），不是拖动才有。
**根因（实测）**：抠图把整块背景留成了 ``a = 1``（源 webm 与 v3 产物同有此值）——
640×360 上 ≈82% 的像素是 ``a = 1``。``a = 1`` 叠在任意背景上都是 ``dst × 254/255``：
白底 255 → **254**，整块矩形因此比背景暗一档；alpha 微缩（``erode_alpha``）也够不到它
（它不在轮廓的 1px 环上，而是铺满整个底板）。

覆盖：

1. **只清 a==1**（0/1/2/3/255 梯度）：``a = 0`` 保持 0、``a >= 2`` 一个位都不动，
   归零像素的 RGB 置 0（不用 255）；
2. **管线末段位置**：``unblend_v3`` 的产物里**不存在** ``a == 1``，且与"改动前的
   管线"逐像素对账——只有 ``a == 1`` 那些像素变了（alpha 归零 + RGB 归零）；
3. **真实素材回归**（idle/drag/move 各一帧：真 ffmpeg 抓帧 → 真阶段二 Q70 落盘 →
   读回）：角落 alpha 1 → 0（白底合成 254 → 255）、``a >= 2`` 的像素 alpha 逐位不变，
   编码损失（有损 RGB）另做**带宽**断言（不冒充逐位相同）；
4. **改档即失效**：``white-v3`` 世代不再被采纳、按当前档重出的同一路径世代被采纳，
   由 ``tests/test_frameseq_provision.py`` 的库级用例钉住（同一份 ``current_generation``
   谓词，生产侧与库侧共用）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from pet import frameseq_provision as fp
from pet.webm_clip import set_session_ending

ROOT = Path(__file__).resolve().parents[1]
# 每个动作一帧的真实素材（源 webm 入库；帧序列产物刻意不入库）
REAL_CLIPS = {
    "idle": ROOT / "assets" / "characters" / "shenshen" / "videos" / "idle" / "待机呼吸休闲.webm",
    "drag": ROOT / "assets" / "characters" / "shenshen" / "videos" / "drag" / "被鼠标拖拽悬空反馈.webm",
    "move": ROOT / "assets" / "characters" / "shenshen" / "videos" / "move" / "左转奔跑.webm",
}


def _mask_at_least(alpha: Image.Image, floor: int) -> Image.Image:
    """L 掩膜（0/255）：``a >= floor`` 的像素。"""
    return alpha.point(lambda v: 255 if v >= floor else 0)


@pytest.fixture(autouse=True)
def _session_still_alive():
    """阶段二有会话闸门（issue #111）：真落盘的用例要求"会话未结束"。"""
    set_session_ending(False)
    yield


def _band_stats(diff: Image.Image, mask: Image.Image) -> tuple[int, float]:
    """``diff`` 在 ``mask`` 覆盖范围内的 ``(最大差值, 各通道平均差)``（分带直方图，逐位精确）。"""
    hist = diff.histogram(mask)
    bands = len(diff.getbands())
    maxima: list[int] = []
    means: list[float] = []
    for band in range(bands):
        counts = hist[band * 256:(band + 1) * 256]
        total = sum(counts)
        maxima.append(max((index for index, count in enumerate(counts) if count), default=0))
        means.append(sum(index * count for index, count in enumerate(counts)) / total if total else 0.0)
    return max(maxima), sum(means) / len(means)


def _delta_max(left: Image.Image, right: Image.Image, mask: Image.Image) -> int:
    return _band_stats(ImageChops.difference(left, right), mask)[0]


def _over_white(rgb: tuple[int, int, int], alpha: int) -> tuple[int, int, int]:
    """straight alpha 合成到纯白底（屏幕上真正看到的那个值）。"""
    return tuple((channel * alpha + 255 * (255 - alpha) + 127) // 255
                 for channel in rgb)


# ---------------------------------------------------------------- 纯函数：只清 a==1
def test_faint_alpha_mask_selects_exactly_one():
    """掩膜判据：``a == 1`` 命中，0 与 2..255 一律不命中（全 256 档扫一遍）。"""
    payload = bytearray()
    for alpha in range(256):
        payload += bytes((10, 20, 30, alpha))
    alpha = Image.frombytes("RGBA", (256, 1), bytes(payload)).split()[3]

    mask = fp.faint_alpha_mask(alpha)

    values = [mask.getpixel((index, 0)) for index in range(256)]
    assert values[1] == 255
    assert values[:1] + values[2:] == [0] * 255


def test_only_alpha_one_is_cleared_and_zeroed_pixels_rgb_is_black():
    """梯度 0/1/2/3/255：只有 a==1 变 0，它的 RGB 置 0，其余像素逐位不动。"""
    alphas = [0, 1, 2, 3, 255]
    payload = bytearray()
    for index, alpha in enumerate(alphas):
        payload += bytes((40 + index * 30, 200 - index * 20, 90, alpha))
    source = Image.frombytes("RGBA", (len(alphas), 1), bytes(payload))
    before = list(source.get_flattened_data())

    out = fp.floor_faint_alpha(source)

    assert list(source.get_flattened_data()) == before          # 纯函数：入参不改
    assert [out.getpixel((x, 0))[3] for x in range(len(alphas))] == [0, 0, 2, 3, 255]
    assert out.getpixel((1, 0))[:3] == (0, 0, 0)                # 归零像素 RGB = 0（不是 255）
    for x in (0, 2, 3, 4):
        assert out.getpixel((x, 0)) == source.getpixel((x, 0))


def test_floor_is_object_identity_when_nothing_faints_and_guards_mode():
    """没有 a==1 的帧**原对象返回**（稳态零成本）；模式不对当场报错。"""
    clean = Image.new("RGBA", (8, 8), (30, 180, 90, 128))
    assert fp.floor_faint_alpha(clean) is clean
    with pytest.raises(ValueError):
        fp.floor_faint_alpha(Image.new("RGB", (8, 8), (0, 0, 0)))


# ---------------------------------------------------------------- 管线末段位置
def _board_fixture() -> Image.Image:
    """真素材的形状：a==1 的整块底板 + 一角 a==0 + 一块不透明角色。"""
    source = Image.new("RGBA", (24, 24), (0, 0, 0, fp.FAINT_ALPHA))
    source.paste(Image.new("RGBA", (12, 12), (60, 70, 120, 255)), (6, 6))
    source.paste(Image.new("RGBA", (2, 2), (0, 0, 0, 0)), (0, 0))
    return source


def test_unblend_v3_output_has_no_alpha_one_and_changes_only_that(monkeypatch):
    """``unblend_v3`` 的产物里没有 ``a == 1``；与改动前的管线只差这些像素。

    "改动前的管线" = 把 ``floor_faint_alpha`` 换成直通后跑同一个 ``unblend_v3``：
    两边共享反解/收色/微缩的全部实现，差异只可能来自清理这一步。
    """
    source = _board_fixture()
    with monkeypatch.context() as patch:
        patch.setattr(fp, "floor_faint_alpha", lambda im: im)
        before = fp.unblend_v3(source)
    out = fp.unblend_v3(source)

    before_alpha = before.split()[3]
    out_alpha = out.split()[3]
    faint = fp.faint_alpha_mask(before_alpha)
    assert faint.getbbox() is not None                  # 断言有效性：改动前的产物确实有 a==1
    assert out_alpha.histogram()[fp.FAINT_ALPHA] == 0   # 产物里 a==1 一个不剩
    # a>=2 的像素：alpha 与 RGB 都逐位不变
    keep = _mask_at_least(before_alpha, 2)
    assert _delta_max(before_alpha, out_alpha, keep) == 0
    assert _delta_max(before.convert("RGB"), out.convert("RGB"), keep) == 0
    # a==1 的像素：alpha 归零 + RGB 置 0
    assert ImageChops.multiply(out_alpha, faint).getbbox() is None
    assert _delta_max(out.convert("RGB"),
                      Image.new("RGB", out.size, (0, 0, 0)), faint) == 0
    # 没有像素的 alpha 被抬高
    assert ImageChops.subtract(out_alpha, before_alpha).getbbox() is None


# ---------------------------------------------------------------- 真实素材回归
def _extract_first_frame(webm: Path, tmp_path: Path) -> Path:
    """真 ffmpeg（产品同一份 exe + 同一组阶段一参数）抓源 webm 的第 1 帧（无损）。"""
    exe = fp.ffmpeg_exe()
    if exe is None:
        pytest.skip("无 ffmpeg（imageio-ffmpeg 不可用）")
    if not webm.is_file():
        pytest.skip(f"本机缺源素材 {webm}")
    out = tmp_path / "f_%04d.webp"
    proc = subprocess.run(
        [exe, "-nostdin", "-v", "error", "-y", "-c:v", "libvpx-vp9", "-i", str(webm),
         "-frames:v", "1", "-pix_fmt", "bgra", "-c:v", "libwebp", "-lossless", "1",
         str(out)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return tmp_path / "f_0001.webp"


def _stage2(source: Path, out_dir: Path, *, baseline: bool, monkeypatch) -> Image.Image:
    """真阶段二（``unblend_frames_in_place`` 原地 Q70 重编码）→ 读回解码后的一帧。

    ``baseline=True`` 时把 ``floor_faint_alpha`` 换成直通 = 改动前（white-v3）的产物。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, out_dir / "f_0001.webp")
    if baseline:
        with monkeypatch.context() as patch:
            patch.setattr(fp, "floor_faint_alpha", lambda im: im)
            result = fp.unblend_frames_in_place(out_dir)
    else:
        result = fp.unblend_frames_in_place(out_dir)
    assert result == (1, 0, False, "")
    with Image.open(out_dir / "f_0001.webp") as opened:
        return opened.convert("RGBA")


@pytest.mark.parametrize("key", sorted(REAL_CLIPS))
def test_real_frame_loses_the_faint_alpha_floor(key, tmp_path, monkeypatch):
    """真素材单帧：角落 alpha 1 → 0（白底 254 → 255），可见像素不受清理影响。"""
    source_frame = _extract_first_frame(REAL_CLIPS[key], tmp_path)
    source = Image.open(source_frame).convert("RGBA")
    with monkeypatch.context() as patch:
        patch.setattr(fp, "floor_faint_alpha", lambda im: im)
        baseline_pipeline = fp.unblend_v3(source)
    new_pipeline = fp.unblend_v3(source)
    baseline = _stage2(source_frame, tmp_path / "baseline", baseline=True,
                       monkeypatch=monkeypatch)
    new = _stage2(source_frame, tmp_path / "new", baseline=False, monkeypatch=monkeypatch)

    base_alpha, new_alpha = baseline.split()[3], new.split()[3]
    total = base_alpha.width * base_alpha.height

    # 1) 前提：源素材与改动前的产物都铺着 a==1 的底板（占比 > 50%）
    assert source.split()[3].histogram()[fp.FAINT_ALPHA] / total > 0.5
    assert base_alpha.histogram()[fp.FAINT_ALPHA] / total > 0.5
    # 2) 新产物里 a==1 一个不剩——WebP 的 alpha 走无损位流，落盘读回仍是 0
    assert new_alpha.histogram()[fp.FAINT_ALPHA] == 0
    # 3) 角落像素（底板）：alpha 1 → 0，白底合成 254 → 255
    assert base_alpha.getpixel((0, 0)) == fp.FAINT_ALPHA
    assert new_alpha.getpixel((0, 0)) == 0
    assert _over_white(baseline.convert("RGB").getpixel((0, 0)), 1) == (254, 254, 254)
    assert _over_white(new.convert("RGB").getpixel((0, 0)), 0) == (255, 255, 255)
    # 4) 可见像素（a>=2）的 alpha 逐位不变，且没有像素的 alpha 被抬高
    keep = _mask_at_least(base_alpha, 2)
    assert _delta_max(base_alpha, new_alpha, keep) == 0
    assert ImageChops.subtract(new_alpha, base_alpha).getbbox() is None
    # 5) 管线末段：只有 a==1 的像素变（alpha 归零 + RGB 归零），a>=2 逐位不变
    before_alpha, after_alpha = baseline_pipeline.split()[3], new_pipeline.split()[3]
    keep_before = _mask_at_least(before_alpha, 2)
    assert _delta_max(before_alpha, after_alpha, keep_before) == 0
    assert _delta_max(baseline_pipeline.convert("RGB"),
                      new_pipeline.convert("RGB"), keep_before) == 0
    assert after_alpha.histogram()[fp.FAINT_ALPHA] == 0
    # 6) 编码损失（另算，不冒充逐位相同）：可见像素上有损 RGB 与阶段二输出的偏差
    #    不许比改动前更差一个量级（本机实测 idle：new mean 4.5 / max 53 对 base 6.2 / 201；
    #    带宽留足余量——它挡的是"清理误伤可见像素"这类灾难，不是编码器的逐位复现）
    base_band = _band_stats(ImageChops.difference(baseline.convert("RGB"),
                                                  baseline_pipeline.convert("RGB")), keep)
    new_band = _band_stats(ImageChops.difference(new.convert("RGB"),
                                                 new_pipeline.convert("RGB")), keep)
    assert new_band[0] <= base_band[0] * 2 + 32, (new_band, base_band)
    assert new_band[1] <= base_band[1] * 2 + 2, (new_band, base_band)
