# Third-Party Software Notices and Information

本文件声明 dsh-pet-pure 随包分发与使用的第三方素材、组件及其许可。
源码许可见根目录 [LICENSE](LICENSE)（MIT）。

## 1. 点击音效素材（assets/sounds/duck）

内置点击音效（`Ya1.mp3` / `Ya2.mp3`）来自 MeteorNOX 的 MIT 项目
[DeepSeek-Balance-Whale-Widget](https://github.com/MeteorNOX/DeepSeek-Balance-Whale-Widget)。
许可正文（MIT License, Copyright (c) 2025 MeteorNOX）：
<https://opensource.org/licenses/MIT>。
这两个文件随安装包一起分发，本节声明随之分发。

## 2. 角色动画素材（assets/characters/shenshen）

鲸鱼娘"深深"的全部 WebM 动画素材来自 PC2005-cloud 的
[dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 项目，版权归原作者所有。
本项目基于其素材构建桌面宠物，仅限个人使用与学习，不得商用或再分发素材本体。

## 3. Qt / PySide6（LGPLv3）

界面框架 PySide6（The Qt Company 的 Python 绑定）按 **LGPLv3** 许可使用。
- 许可证全文：<https://www.gnu.org/licenses/lgpl-3.0.html>
- Qt 源码获取：<https://www.qt.io/download/open-source>
- 本应用以动态链接方式使用 Qt，不修改 Qt 本体；随包分发 Qt 运行库
  （`_internal/PySide6/`，含少量 zh_CN / zh_TW / en 翻译文件）。

## 4. Pillow（HPND 许可）

图像处理库 Pillow（图片解码、帧序列处理、图标生成）按
**HPND（Historical Permission Notice and Disclaimer）**许可使用。
- 许可证全文：<https://github.com/python-pillow/Pillow/blob/main/LICENSE>

## 5. imageio-ffmpeg 与 FFmpeg（BSD-2 + GPL）

动画播放引擎依赖 `imageio-ffmpeg`（BSD-2-Clause），其包内附带一份
**静态链接的 FFmpeg 可执行文件**（Windows x64）——WebM/VP9 透明通道解码的
动画引擎本体。FFmpeg 按其 **GPL 许可**分发（该构建启用了 GPL 组件）。
- imageio-ffmpeg 许可证：<https://github.com/imageio/imageio-ffmpeg/blob/main/LICENSE>
- FFmpeg 许可与源码：<https://ffmpeg.org/legal.html> / <https://ffmpeg.org/download.html>
- 本包不修改 FFmpeg 本体。

## 6. Inno Setup（仅构建期）

Windows 安装器由 Inno Setup 6（JR Software）打包，安装器工具不随最终程序运行。
- 许可证：<https://jrsoftware.org/files/is/license.txt>

## 7. 彩蛋配图（assets/big_blue_fat_fish）

右键菜单彩蛋入口的配图随包分发，仅限个人使用，不得商用再分发。

---

未在此列出的 Python 标准库与构建工具（pip / PyInstaller 等）按其各自许可使用，
均不修改其本体。疑问请在 [Issues](https://github.com/Heptalune/dsh-pet-pure/issues) 提出。
