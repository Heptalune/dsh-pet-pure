# -*- coding: utf-8 -*-
"""同角色多宠：只读媒体视图共享 + 首帧解码跨库复用（overlay 单进程多 sprite）。

背景（实机调查）：overlay 单进程架构下，每只与主宠同角色的子宠都会走
``overlay_shell._create_sprite_library`` → ``app._create_library`` 造一份**全新的
MovieLibrary**：各自 106 个 clip 对象、各自一份 _paths/manifest/move_curves、各自
把同一批素材重新预热一遍（重复的 ffmpeg spawn 与源哈希）。

本文件锁三条：
1. 同角色（同素材目录）的库共享**只读**媒体视图（路径表/分类表/曲线/帧序列世代
   映射是同一批对象），异角色/异目录不共享，最后一个库回收后共享视图失效；
2. 低优先级随机动作池的 clip 建构与预热只由**预热责任持有者**跑一次，兄弟库不重复
   （可通过 clip 对象总数与 ffmpeg spawn 次数验证）；
3. **播放态绝不共享**：同角色两只宠同时播同名 clip 时，各自拿各自的 clip 实例，
   跳帧/当前帧/finished 互不干扰。
"""
from __future__ import annotations

import gc
import json
import os
import time
import weakref
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

import tests.test_overlay_window_capabilities as cap
from pet import catalog, frameseq_provision
from pet import library as library_mod
from pet import webm_clip
from pet.library import MovieLibrary
from tests.test_frameseq_clip import _make_frames

app = QApplication.instance() or QApplication([])

#: 临时角色包的目录布局：全部落在"热集"（无损）目录下，帧序列世代才被采纳
FOLDERS = {
    "click": ["点击回应-开心跃动", "点击回应-傲娇生气"],
    "turn": ["东张西望"],
    "drag": ["被鼠标拖拽悬空反馈"],
    "idle": ["待机呼吸休闲"],
    "move": ["螃蟹走路"],
}


class FakeClip:
    """记录预热调用的假 clip（不碰 ffmpeg/Qt 解码）。"""

    def __init__(self, path, parent=None):
        self.path = Path(path)
        self.warmed_meta = 0
        self.warmed_frame = 0

    def warm_meta(self):
        self.warmed_meta += 1

    def warm_first_frame(self):
        self.warmed_frame += 1


def _wait_until(cond, timeout_s: float = 30.0, what: str = "条件") -> None:
    """轮询等待（宽预算；不赌时序、不固定 sleep 猜时长）。"""
    t0 = time.monotonic()
    while not cond():
        app.processEvents()
        assert time.monotonic() - t0 < timeout_s, f"等待超时：{what}"
        time.sleep(0.01)


def _make_videos(tmp_path: Path, folders=None) -> Path:
    """临时素材目录（只写占位文件；FakeClip 用例不需要真 webm）。"""
    videos = tmp_path / "videos"
    for folder, names in (folders or FOLDERS).items():
        directory = videos / folder
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / f"{name}.webm").write_bytes(b"fake")
    return videos


def _make_pack(tmp_path: Path, folders=None) -> Path:
    """临时角色包：videos + 与之匹配的帧序列世代目录（真 FrameSeqClip，无 ffmpeg）。"""
    videos = _make_videos(tmp_path, folders)
    root = tmp_path / "frameseq"
    for webm in sorted(videos.rglob("*.webm")):
        out = frameseq_provision.clip_out_dir(webm, videos, root)
        _make_frames(out, count=6)
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        meta["source_sha256"] = frameseq_provision.source_sha256(webm)
        (out / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return videos


def _make_real_webm_pack(tmp_path: Path) -> Path:
    """临时素材目录 + 真 webm（从仓库真素材 stream copy 裁几帧，秒级）。

    帧序列目录**故意缺席**：这样 clip 走 WebMClip 的 ffmpeg 首帧路径，才能数
    ffmpeg spawn 次数（帧序列 clip 的首帧是 QImage 直读，不走 ffmpeg）。
    """
    import subprocess

    real = (Path(__file__).resolve().parent.parent / "assets" / "characters"
            / "shenshen" / "videos" / "idle" / "待机呼吸休闲.webm")
    if not real.is_file():
        pytest.skip("仓库真素材缺席，无法造 webm 夹具")
    ffmpeg = webm_clip.imageio_ffmpeg.get_ffmpeg_exe()
    videos = tmp_path / "videos"
    for folder, names in FOLDERS.items():
        directory = videos / folder
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            out = directory / f"{name}.webm"
            proc = subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(real),
                 "-frames:v", "6", "-c:v", "copy", str(out)],
                capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr
    return videos


# ---------------------------------------------------------------- 1. 只读视图共享
def test_same_character_libraries_share_readonly_media_view(tmp_path, monkeypatch):
    monkeypatch.setattr(library_mod, "WebMClip", FakeClip)
    videos = _make_videos(tmp_path)
    lib1 = MovieLibrary(asset_dir=videos)
    lib2 = MovieLibrary(asset_dir=videos)
    try:
        assert lib2._shared is lib1._shared
        # 逐项同一对象：路径表 / 分类表 / 曲线 / 帧序列世代映射
        assert lib2._paths is lib1._paths
        assert lib2.manifest is lib1.manifest
        assert lib2._manifest is lib1._manifest
        assert lib2.folder_map is lib1.folder_map
        assert lib2.folder_files is lib1.folder_files
        assert lib2.no_mirror is lib1.no_mirror
        assert lib2.move_strides is lib1.move_strides
        assert lib2.move_curves is lib1.move_curves
        assert lib2._frameseq_dirs is lib1._frameseq_dirs
        assert lib2._priority_names() == lib1._priority_names()
    finally:
        lib1.shutdown()
        lib2.shutdown()


def test_different_asset_dirs_do_not_share_media_view(tmp_path, monkeypatch):
    monkeypatch.setattr(library_mod, "WebMClip", FakeClip)
    lib1 = MovieLibrary(asset_dir=_make_videos(tmp_path / "a"))
    lib2 = MovieLibrary(asset_dir=_make_videos(tmp_path / "b"))
    try:
        assert lib2._shared is not lib1._shared
        assert lib2._paths is not lib1._paths
    finally:
        lib1.shutdown()
        lib2.shutdown()


def test_shared_view_released_with_last_library(tmp_path, monkeypatch):
    """共享视图只由在用库持有：最后一个库回收后注册表条目失效（不留常驻残影）。"""
    monkeypatch.setattr(library_mod, "WebMClip", FakeClip)
    videos = _make_videos(tmp_path)
    lib = MovieLibrary(asset_dir=videos)
    ref = weakref.ref(lib._shared)
    assert library_mod.lookup_shared_media(lib.character_id, videos) is not None
    lib.shutdown()
    del lib
    gc.collect()
    assert ref() is None
    assert library_mod.lookup_shared_media("shenshen", videos) is None


# ---------------------------------------------------------------- 2. 预热责任
def test_peer_library_defers_low_priority_pool(tmp_path, monkeypatch):
    """同角色第二/三份库不再重复建低优先级池，预热责任只在首个库。"""
    monkeypatch.setattr(library_mod, "WebMClip", FakeClip)
    videos = _make_videos(tmp_path)
    lib1 = MovieLibrary(asset_dir=videos)
    try:
        high, low = lib1._priority_names()
        assert low, "夹具必须含低优先级池，否则本用例没意义"
        lib1.schedule_high_priority_warm()
        lib1.schedule_low_priority_warm()
        assert lib1._warm_peer is False
        assert lib1._low_warm_timer.isActive() is True

        lib2 = MovieLibrary(asset_dir=videos)
        try:
            lib2.schedule_high_priority_warm()
            lib2.schedule_low_priority_warm()
            assert lib2._warm_peer is True
            assert lib2._low_warm_timer.isActive() is False
            # 池级残留回收照旧开（它管内存，与预热责任无关）
            assert lib2._idle_trim_timer.isActive() is True
            # 兄弟库只建了高优先级交互核，低优先级池一个 clip 都不建
            assert set(lib2.movies()) == set(high)
            assert lib2._warm_low_priority_background() is None
            assert set(lib2.movies()) == set(high)
        finally:
            lib2.shutdown()

        # 责任持有者照旧把整池建齐
        lib1._warm_low_priority_background()
        _wait_until(lambda: not lib1._low_warm_in_flight
                    and lib1._low_first_frames_done, what="责任持有者低优先级批次")
        assert set(lib1.movies()) == set(lib1.names())
    finally:
        lib1.shutdown()


def test_owner_exit_hands_warm_responsibility_to_peer(tmp_path, monkeypatch):
    """责任持有者退出（主宠退出/子宠被提升）：责任交给仍活着的兄弟库。"""
    monkeypatch.setattr(library_mod, "WebMClip", FakeClip)
    videos = _make_videos(tmp_path)
    lib1 = MovieLibrary(asset_dir=videos)
    lib1.schedule_high_priority_warm()
    lib1.schedule_low_priority_warm()
    lib2 = MovieLibrary(asset_dir=videos)
    try:
        lib2.schedule_high_priority_warm()
        lib2.schedule_low_priority_warm()
        assert lib2._warm_peer is True

        lib1.shutdown()
        assert lib1._shared.is_warm_owner(lib2) is True
        assert lib2._warm_peer is False
        assert lib2._low_warm_timer.isActive() is True

        lib2._warm_low_priority_background()
        _wait_until(lambda: not lib2._low_warm_in_flight
                    and lib2._low_first_frames_done, what="接手的低优先级批次")
        assert set(lib2.movies()) == set(lib2.names())
    finally:
        lib1.shutdown()
        lib2.shutdown()


def test_peer_can_still_create_low_priority_clip_on_demand(tmp_path, monkeypatch):
    """兄弟库只是不重复预热：播放时按需建 clip 的能力一点不减。"""
    monkeypatch.setattr(library_mod, "WebMClip", FakeClip)
    videos = _make_videos(tmp_path)
    lib1 = MovieLibrary(asset_dir=videos)
    lib1.schedule_high_priority_warm()
    lib1.schedule_low_priority_warm()
    lib2 = MovieLibrary(asset_dir=videos)
    try:
        lib2.schedule_high_priority_warm()
        lib2.schedule_low_priority_warm()
        _, low = lib2._priority_names()
        clip = lib2.movie(low[0])
        assert clip is not None
        assert set(lib2.movies()) == set(lib2._priority_names()[0]) | {low[0]}
    finally:
        lib1.shutdown()
        lib2.shutdown()


def test_peer_pause_resume_keeps_warm_responsibility(tmp_path, monkeypatch):
    """隐藏/恢复（pause_warm/resume_warm）不改变预热责任归属。"""
    monkeypatch.setattr(library_mod, "WebMClip", FakeClip)
    videos = _make_videos(tmp_path)
    lib1 = MovieLibrary(asset_dir=videos)
    lib1.schedule_high_priority_warm()
    lib1.schedule_low_priority_warm()
    lib2 = MovieLibrary(asset_dir=videos)
    lib2.schedule_high_priority_warm()
    lib2.schedule_low_priority_warm()
    try:
        lib2.pause_warm()
        lib2.resume_warm()
        assert lib2._warm_peer is True
        assert lib2._low_warm_timer.isActive() is False

        lib1.pause_warm()
        assert lib1._low_warm_timer.isActive() is False
        lib1.resume_warm()
        assert lib1._warm_peer is False
        assert lib1._low_warm_timer.isActive() is True
    finally:
        lib1.shutdown()
        lib2.shutdown()


def test_peer_skips_trivial_first_frame_warm_but_owner_does_not(tmp_path,
                                                               monkeypatch):
    """帧 0 冷解码代价可忽略的 clip（FrameSeqClip）在兄弟库不重复预热。"""

    class TrivialClip(FakeClip):
        FIRST_FRAME_WARM_TRIVIAL = True

    monkeypatch.setattr(library_mod, "WebMClip", FakeClip)
    videos = _make_videos(tmp_path)
    lib1 = MovieLibrary(asset_dir=videos)
    lib1.schedule_high_priority_warm()
    lib2 = MovieLibrary(asset_dir=videos)
    try:
        lib2.schedule_high_priority_warm()
        assert lib1._warm_peer is False and lib2._warm_peer is True

        owner_clip = TrivialClip(videos / "idle" / "x.webm")
        peer_clip = TrivialClip(videos / "idle" / "x.webm")
        lib1._warm_objects([owner_clip], 1)
        lib2._warm_objects([peer_clip], 1)
        # 元数据照旧预热（廉价且与首帧无关）；帧 0 在兄弟库跳过
        assert owner_clip.warmed_meta == 1 and owner_clip.warmed_frame == 1
        assert peer_clip.warmed_meta == 1 and peer_clip.warmed_frame == 0
    finally:
        lib1.shutdown()
        lib2.shutdown()


# ------------------------------------------------------ 3. 首帧解码跨库只解一次
def test_webm_first_frame_decode_is_shared_across_libraries(tmp_path, monkeypatch):
    """三份同角色库预热同一批素材：ffmpeg 首帧解码次数 == 单库口径。"""
    videos = _make_real_webm_pack(tmp_path)
    webm_clip.reset_first_frame_share()
    spawns: list = []
    real_read_frames = webm_clip.imageio_ffmpeg.read_frames

    def counting_read_frames(*args, **kwargs):
        spawns.append(args[0] if args else kwargs.get("uri"))
        return real_read_frames(*args, **kwargs)

    monkeypatch.setattr(webm_clip.imageio_ffmpeg, "read_frames",
                        counting_read_frames)

    libs = []
    try:
        for _ in range(3):
            lib = MovieLibrary(asset_dir=videos)
            libs.append(lib)
            lib.schedule_high_priority_warm()
            clips = list(lib.movies().values())
            assert clips, "高优先级 clip 应立即创建"
            _wait_until(
                lambda clips=clips: all(
                    getattr(c, "_first_image", None) is not None for c in clips),
                what="高优先级首帧预热")
        single = len(libs[0].movies())
        stats = webm_clip.first_frame_share_stats()
        # 单库口径 = 高优先级 clip 数；三库合计仍等于它（后两库全部命中共享表）
        assert len(spawns) == single, f"ffmpeg 首帧解码 {len(spawns)} 次（单库口径 {single}）"
        assert stats["spawns"] == single
        assert stats["hits"] == 2 * single
        # 三份库的首帧是同一张 QImage 对象（不产生第二/第三份像素）
        images = [lib.movies()[name]._first_image
                  for lib in libs for name in lib.movies()]
        assert len({id(img) for img in images}) == single
    finally:
        for lib in libs:
            lib.shutdown()
        webm_clip.reset_first_frame_share()


def test_concurrent_peer_warm_does_not_respawn_ffmpeg(tmp_path, monkeypatch):
    """三库**并发**预热（连续 spawn / 活跃清单复活）：兄弟库等责任持有者解出再取用。

    并发是最容易漏掉的窗口：三份库的高优先级预热同时跑，谁都还没解出时共享表是
    空的——不做有界等待就各自 spawn 一个 ffmpeg 解同一段素材（实测 3 库 × 4 段 =
    12 次 spawn）。
    """
    videos = _make_real_webm_pack(tmp_path)
    webm_clip.reset_first_frame_share()
    spawns: list = []
    real_read_frames = webm_clip.imageio_ffmpeg.read_frames
    monkeypatch.setattr(
        webm_clip.imageio_ffmpeg, "read_frames",
        lambda *a, **kw: (spawns.append(a[0] if a else None),
                          real_read_frames(*a, **kw))[1])

    libs = []
    try:
        for _ in range(3):
            libs.append(MovieLibrary(asset_dir=videos))
        for lib in libs:
            lib.schedule_high_priority_warm()   # 三库并发起预热，不排队
        def _ready():
            return all(all(getattr(c, "_first_image", None) is not None
                           for c in lib.movies().values()) for lib in libs)
        _wait_until(_ready, timeout_s=60.0, what="三库并发首帧预热")
        single = len(libs[0].movies())
        assert len(spawns) == single, f"并发预热下 ffmpeg 首帧解码 {len(spawns)} 次（单库口径 {single}）"
    finally:
        for lib in libs:
            lib.shutdown()
        webm_clip.reset_first_frame_share()


def test_shared_first_frame_table_is_hard_bounded_even_when_pinned(tmp_path):
    """跨库首帧共享表是**硬上界**：全 pin 也照逐最久未用。

    防的是"切角色反复建库"把每个角色交互核的首帧都钉在表里永不释放
    （7 段 × 角色数 × 0.88MB）。pin 只在还有别的条目可逐时起保护作用。
    """
    videos = _make_videos(tmp_path)
    webm_clip.reset_first_frame_share()
    old_budget = webm_clip._first_frame_budget_bytes
    webm_clip.set_first_frame_budget(1024 * 1024)  # ≈ 一张 640×360 RGBA 首帧
    try:
        paths = [p for p in sorted(videos.rglob("*.webm"))]
        assert len(paths) >= 3
        for path in paths:
            webm_clip.pin_shared_first_frame(path)
            webm_clip.share_first_frame(path, QImage(640, 360, QImage.Format.Format_RGBA8888))
        stats = webm_clip.first_frame_share_stats()
        assert stats['bytes'] <= stats['budget']
        assert stats['entries'] < len(paths)
    finally:
        webm_clip.set_first_frame_budget(old_budget)
        webm_clip.reset_first_frame_share()


# ---------------------------------------------------------------- 4. 播放态隔离
def test_same_name_clip_playback_state_is_per_library(tmp_path):
    """同角色两库同时播同名 clip：clip 实例与播放位置各自独立。"""
    videos = _make_pack(tmp_path)
    lib1 = MovieLibrary(asset_dir=videos, prewarm_enabled=False)
    lib2 = MovieLibrary(asset_dir=videos, prewarm_enabled=False)
    try:
        name = lib1._priority_names()[0][0]
        clip1 = lib1.movie(name)
        clip2 = lib2.movie(name)
        assert clip1 is not clip2

        fired1, fired2 = [], []
        clip1.finished.connect(lambda: fired1.append(1))
        clip2.finished.connect(lambda: fired2.append(1))

        clip1.jumpToFrame(0)
        clip2.jumpToFrame(4)
        assert clip1.currentFrameNumber() == 0
        assert clip2.currentFrameNumber() == 4
        assert clip1.currentImage() is not clip2.currentImage()

        # 一只的停止/跳帧不动另一只的播放位置
        clip1.stop()
        assert clip2.currentFrameNumber() == 4
        assert fired1 == [] and fired2 == []
    finally:
        lib1.shutdown()
        lib2.shutdown()


# ------------------------------------------------- 5. overlay 三宠（真库、真壳）
class _RealLibraryInstance:
    """按角色 id 建**真** MovieLibrary 的实例桩（对齐 app._create_library 顺序）。"""

    def __init__(self, config, dirs: dict):
        self.config = config
        self.dirs = dirs
        self.made: list = []

    def _create_library(self, character_id):
        videos = self.dirs.get(character_id, self.dirs[catalog.DEFAULT_CHARACTER])
        lib = MovieLibrary(character_id=character_id, asset_dir=videos)
        self.made.append(lib)
        lib.schedule_high_priority_warm()
        lib.schedule_low_priority_warm()
        return lib


def _make_real_shell(tmp_path, dirs):
    from pet.config import Config
    from pet.overlay_shell import OverlayShell
    from pet.pet_sprite import PetSprite

    config = Config(str(tmp_path / "cfg"))
    config.set("character", catalog.DEFAULT_CHARACTER)
    config.save()
    instance = _RealLibraryInstance(config, dirs)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    # 主宠库由 OverlayShell.__init__ 经 instance 建好（与产品同一条路径），
    # 这里不再另建一份，否则主宠会拿到"兄弟库"身份。
    assert shell.lib is instance.made[0]
    return shell, instance


def _teardown(shell) -> None:
    try:
        shell.clear_spawned_pets()
    except Exception:
        pass
    lib = getattr(shell, "lib", None)
    shutdown = getattr(lib, "shutdown", None)
    if callable(shutdown):
        shutdown()
    shell._delete_runtime_marker()


def test_three_same_character_pets_share_one_media_view(tmp_path):
    """同角色三宠（主宠 + 两只子宠）：一份只读视图、低优先级池只建一次。"""
    videos = _make_pack(tmp_path)
    shell, instance = _make_real_shell(tmp_path, {catalog.DEFAULT_CHARACTER: videos})
    try:
        shell.spawn_pet()
        shell.spawn_pet()
        libs = [shell.lib] + [shell._spawned_libs[s] for s in shell._spawned]
        assert len(libs) == 3
        assert len({id(lib._shared) for lib in libs}) == 1
        assert len({id(lib._paths) for lib in libs}) == 1
        assert len({id(lib._frameseq_dirs) for lib in libs}) == 1
        assert [lib._warm_peer for lib in libs] == [False, True, True]

        # 责任持有者把整池建齐；两只兄弟库只留高优先级交互核
        shell.lib._warm_low_priority_background()
        _wait_until(lambda: not shell.lib._low_warm_in_flight
                    and shell.lib._low_first_frames_done, what="责任持有者低优先级批次")
        assert set(shell.lib.movies()) == set(shell.lib.names())
        for lib in libs[1:]:
            assert set(lib.movies()) == set(lib._priority_names()[0])
        assert len(libs[0].movies()) == len(libs[0].names())
        # 三宠同名 clip 仍是各自的实例（播放态隔离）
        name = sorted(libs[0].movies())[0]
        assert len({id(lib.movie(name)) for lib in libs}) == 3
    finally:
        _teardown(shell)


def test_child_character_switch_rebuilds_and_can_re_adopt(tmp_path):
    """子宠换角色：按新身份重建库并释放旧库；换回旧角色时复用旧视图。"""
    videos = _make_pack(tmp_path / "a")
    other = _make_pack(tmp_path / "b")
    shell, instance = _make_real_shell(
        tmp_path,
        {catalog.DEFAULT_CHARACTER: videos, "other-character": other})
    try:
        shell.spawn_pet()
        child = shell._spawned[0]
        main_shared = shell.lib._shared
        old_child_lib = shell._spawned_libs[child]
        assert old_child_lib._shared is main_shared

        shell.switch_character("other-character", child)
        new_lib = shell._spawned_libs[child]
        assert new_lib is not old_child_lib
        assert new_lib.character_id == "other-character"
        assert new_lib._shared is not main_shared
        assert new_lib._paths is not main_shared.paths
        assert new_lib._warm_peer is False          # 异角色：自己就是责任持有者
        lib_shutdown = getattr(old_child_lib, "_shutdown", None)
        assert lib_shutdown is True                 # 旧库已收尾

        # 换回原角色：主宠的视图还活着 → 直接复用，不再重读素材
        shell.switch_character(catalog.DEFAULT_CHARACTER, child)
        back_lib = shell._spawned_libs[child]
        assert back_lib is not new_lib
        assert back_lib._shared is main_shared
        assert back_lib._paths is main_shared.paths
        assert back_lib._warm_peer is True
    finally:
        _teardown(shell)
