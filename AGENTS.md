# AGENTS.md — dsh-pet-pure 开发指南

本仓库是 **[Heptalune/dsh-pet-pure](https://github.com/Heptalune/dsh-pet-pure)**：
上游 [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)
（再上游 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)）的
「纯桌宠」定制版。**只发布 pure 变体**——不联网、无 AI 功能的桌面宠物。

## 一图看懂

```
DSH_PET_PURE=1（packaging/pet_entry_pure.py 在 import 前设置）
   ↓
pet/feature_activation.py  → 16 个可选功能模块冻结为空（菜单/服务/设置页自动消失）
pet/chat/                  → 从打包产物整体排除（--exclude-module pet.chat）
requirements-pure.txt      → 仅 5 个依赖（PySide6 / imageio-ffmpeg / Pillow / tzdata / pyinstaller）
```

纯变体的所有差异都以 `pet/pure_build.py` 的 `is_pure_build()` 门控，
**不改动上游完整版语义**。完整设计见 [docs/PURE-VARIANT.md](docs/PURE-VARIANT.md)。

## 常用命令

```powershell
# 构建（产物 dist-onedir\dsh-pet-pure\ + 便携 zip；-SkipHotFrameseq 再省 137MB）
powershell -ExecutionPolicy Bypass -File scripts\build_pure.ps1

# 测试（开发依赖：pip install pytest lunar-python）
python -m pytest -q

# 动画菜单分类/显示名：pet/context_menus/shared.py
#   ANIMATION_CATEGORY_MAP（精选分类）+ ANIMATION_DISPLAY_NAMES（改名）
#   + _split_elevated_acts（顶级分类：搬砖日常/钱包状态）+ build_animation_categories
```

## 改动纪律（必读）

1. **砍 UI ≠ 砍控件**：把设置行/页从界面拿掉时，控件必须**照常构建并保活**
   （挂到 dialog 名下或保 Python 引用）。`_write_config` 保存时会无条件读很多
   控件的值——控件随旧页 `deleteLater` 连坐销毁后，读值就是 RuntimeError，
   用户看到的就是"保存失败：未预期的错误"（v6 实际翻过车，前车之鉴）。
2. **判活先于读控件**：功能模块的设置读写都按 `feature_activation.is_active`
   门控（上游既有纪律），纯变体靠它自动短路。
3. **菜单动作三态**：`pet/context_menus/registry.py` 的 available（能力）/
   enabled（运行态）与 `feature_for_action`（模块投影）分层，新入口先想清楚
   归哪层。
4. **素材分类**：新增动画先看名字能否被 `ANIMATION_CATEGORY_PREFIXES` 自动
   归类，特殊名字才进 `ANIMATION_CATEGORY_MAP` 精选表；上游语义池参考
   PC2005-cloud/dsh-pet 的 `assets/config.jsonc`。

## 目录速览

| 路径 | 内容 |
|---|---|
| `pet/` | 主程序（Qt 桌宠、菜单、设置、动画引擎 webm_clip/frameseq） |
| `packaging/` | 入口（pet_entry_pure.py）与 Inno Setup 脚本 |
| `scripts/` | build_pure.ps1 与素材/图标工具 |
| `assets/characters/shenshen/` | 106 段 WebM 动画与角色清单 |
| `tests/` | pytest 套件（源码树内跑，不依赖打包） |
| `docs/` | PURE-VARIANT.md（变体设计）、ASSET-PIPELINE.md（做动画） |
