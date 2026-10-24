# -*- coding: utf-8 -*-
"""随包本地 OCR 的**模型事实**：文件名、摘要、目录解析（PySide 无关，只 import 标准库）。

为什么单独立一层（而不是继续留在 ``ocr.py`` 里）：切片 5 把识别搬到**子进程**执行
（``ocr_worker.py``），那条路径的硬约束是"不许 import PySide"——子进程只该付
rapidocr / numpy / onnxruntime 的启动开销（实测 ``from PySide6.QtGui import QImage``
约 0.2 s，正好是子进程模型最想省掉的那一段）。``ocr.py`` 是宿主侧驱动，顶部 import
了 ``QImage``；worker 若从它那里取模型常量，就会把整个 Qt 拖进每一个识别子进程。

于是模型事实落在本模块，两个角色引用同一份：

- ``ocr_worker.py``（识别子进程）用它建 session、报"模型缺失"；
- ``ocr.py``（宿主）把它整组再导出，``scripts/verify_rapidocr_models.py`` 与既有
  测试仍从 ``ocr.MODEL_*`` 取判据——判据不会因为这次搬家漂移成两份。

模型文件的来源与体积（真机实测，2026-10-13）：

===========================================  ==========
文件                                           体积
===========================================  ==========
``PP-OCRv6_det_small.onnx``（文本检测）         9.5 MB
``PP-OCRv6_rec_small.onnx``（文本识别）        20.2 MB
``ch_ppocr_mobile_v2.0_cls_mobile.onnx``（方向分类） 0.6 MB
===========================================  ==========

三个文件都直接取自 ``rapidocr`` wheel 自带的 ``rapidocr/models/``（同一版本、
逐字节相同），重新取出/换版本的命令::

    python -c "import os, rapidocr, shutil; src=os.path.join(os.path.dirname(rapidocr.__file__),'models'); [shutil.copy2(os.path.join(src,n), 'resources/rapidocr_models/') for n in os.listdir(src) if n.endswith('.onnx')]"

识别模型的字符表（18708 个字符）**内嵌在 onnx 元数据里**（实测
``custom_metadata_map['character']``），所以不需要额外的 ``ppocr_keys_v1.txt``
——这也是"断网可用"能成立的另一半：字典不会在首次识别时被顺手下拉。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "MODEL_FILES",
    "MODEL_RELATIVE_DIR",
    "MODEL_SHA256",
    "OCR_MODEL_DIR_ENV",
    "bundled_model_dir",
    "model_problem",
    "resolve_model_dir",
]

#: 模型在源码树 / 冻结包里的相对目录。冻结包按 ``sys._MEIPASS`` 为根（与
#: ``pet/game_fps.py::bundled_backend_path`` 同一约定）。
MODEL_RELATIVE_DIR = ("resources", "rapidocr_models")

#: 三个模型文件（键就是 rapidocr 配置里的段名，注入 session 时按它对应）。
MODEL_FILES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec": "PP-OCRv6_rec_small.onnx",
}

#: 三个模型文件的 sha256（2026-10-13 按随包文件实测值登记）。
#:
#: 这是**构建期硬门**（``scripts/verify_rapidocr_models.py``）的唯一真相源：模型
#: 要是被截断、换成了同名的另一版、或者混进别的语言模型，产品**不会崩**——识别
#: 结果会静默变差（错字、漏行），用户只会觉得"这个识屏不准"。所以在打包阶段就按
#: 名字 + 摘要挡住，而不是等用户发现问题。运行期**不**校验：每次启动多读 31 MB
#: 只为一件 CI 该做的事（与 ``pet/game_fps.py::REQUIRED_SHA256`` 同一取舍）。
#:
#: 换模型版本时：改 ``MODEL_FILES`` + 本表 + THIRD_PARTY_NOTICES.md，三处必须同时改。
MODEL_SHA256 = {
    "Det": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "Cls": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    "Rec": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
}

#: 模型目录覆盖开关（开发/自定义部署用）。正式包不设它，走随包目录。
OCR_MODEL_DIR_ENV = "DSH_PET_OCR_MODEL_DIR"


def bundled_model_dir() -> Path:
    """随包模型目录（**纯函数**，不碰文件系统，也不做回退）。

    冻结包取 ``sys._MEIPASS/resources/rapidocr_models``，源码运行取仓库根的同一
    相对路径；``DSH_PET_OCR_MODEL_DIR`` 可以整个覆盖它。与
    ``pet/game_fps.py::bundled_backend_path`` 同一套写法，理由也一样：模型必须
    来自我们知道的那一份，不能在 PATH / 环境里"就近找一个"。
    """
    override = os.environ.get(OCR_MODEL_DIR_ENV)
    if override:
        return Path(override)
    meipass = getattr(sys, "_MEIPASS", None)
    root = Path(meipass) if meipass else Path(__file__).resolve().parents[2]
    return root.joinpath(*MODEL_RELATIVE_DIR)


def model_problem(directory: Path | None = None) -> str:
    """模型目录不可用的**人话**原因；可用返回 ``""``。

    只判存在性：文件在不在是安装完整性问题；内容对不对要等 ONNXRuntime 加载时
    才知道（那时由 ``OcrLoadError`` 接手）。缺哪个文件就报哪个，用户拿着这句话
    才知道该重装还是该补文件。
    """
    target = Path(directory) if directory is not None else bundled_model_dir()
    missing = [name for name in MODEL_FILES.values() if not (target / name).is_file()]
    if not missing:
        return ""
    return (
        f"缺少本地 OCR 模型文件 {', '.join(missing)}（找过的目录：{target}）"
        "——模型随安装包分发，缺失说明安装不完整，请重新安装"
    )


def _installed_models_dir() -> Path | None:
    """已安装的 rapidocr wheel 自带的模型目录（开发环境的回退）。"""
    try:
        import rapidocr
    except ImportError:
        return None
    return Path(rapidocr.__file__).resolve().parent / "models"


def resolve_model_dir(explicit: Path | None = None) -> Path:
    """实际使用的模型目录：显式指定 > 随包 ``resources/`` > 已装 wheel 自带。

    中间那条回退是给**开发环境**的：干净 clone 里 ``resources/rapidocr_models/``
    可能还没落盘，而 ``rapidocr`` 依赖本身就带着同一批模型文件（版本钉死）。

    ``DSH_PET_OCR_MODEL_DIR``（以及 ``explicit``）**不参与那条回退**：显式指定的
    目录就是用户要的那一份，它不可用时回退到"另一批模型"是静默换版——识别结果会
    变，而用户以为自己换目录只是换了个位置。两者都不可用时返回随包路径——报错要报
    用户机器上的那一个，不是开发机的。

    本函数只在**识别子进程**里被调用（见 ``ocr_worker.py``）：它会把 rapidocr
    拖进来，而宿主进程（``ocr.py``）没有理由为一次路径探测付这份导入成本。
    """
    if explicit is not None:
        return Path(explicit)
    override = os.environ.get(OCR_MODEL_DIR_ENV)
    if override:
        return Path(override)
    packaged = bundled_model_dir()
    if model_problem(packaged) == "":
        return packaged
    installed = _installed_models_dir()
    if installed is not None and model_problem(installed) == "":
        return installed
    return packaged
