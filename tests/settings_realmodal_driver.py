"""真模态框流程的子进程驱动器（test_settings_stale_form_exit.py 的两条真 exec 用例用）。

为什么走进程边界：主套件所有用例共享一个 QApplication 进程，任何「事件循环在跑时
兑现 quit()」的历史都会把 quitNow 旗标留在 Qt 线程数据上（qtbase 6.9
qcoreapplication.cpp:1512），之后 QEventLoop.exec 进入即 -1 秒退且不清旗
（qeventloop.cpp exec 入口检查）——真模态框"弹了即死"（2026-10-13 PR CI 三端连红）。
进程内清旗只有 QCoreApplication.exec 一条路（qcoreapplication.cpp:1445/1450），但它会
泵全局事件队列，把别用例遗留的排队事件派发到正在销毁的对象上（conftest 明令禁止
全局冲刷）。所以真框验证放干净子进程：AGENTS.md「Qt 回归用真实事件循环和进程边界」。

用法：
    python tests/settings_realmodal_driver.py <mode> <base_dir> <slot>
    mode = confirm-discard | unverified-notice

结果：stdout 打一行 ``RESULT {json}``；父进程按 JSON 字段断言。流程始终 os._exit，
避开解释器退出期的 Qt 对象树拆除（套件史上有原生段错误记录）。
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DISCARD_LABEL = "放弃未保存的改动并关闭"


def _emit(result) -> None:
    sys.stdout.write("RESULT " + json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0)


def main() -> None:
    mode, base, slot = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    # 与 conftest 同规的音频防卡/防崩桩：headless 环境不许碰真实音频设备。
    try:
        from PySide6.QtMultimedia import QMediaPlayer, QSoundEffect

        QSoundEffect.play = lambda self: None
        QSoundEffect.setSource = lambda self, source: None
        QMediaPlayer.play = lambda self: None
    except Exception:
        pass

    from pet import pet_profiles, settings_slot_config
    from pet.config import Config
    from pet.modern_settings_dialog import ModernSettingsDialog

    app = QApplication([])
    result = {"mode": mode, "boxes": [], "clicked": [], "finished": None}

    # 初始盘面：主配置带一个非默认全局值 + 槽档案 scale=0.8（与进程内用例逐位一致）。
    main_config = Config(base=base)
    main_config.set("pet_opacity", 55)
    assert main_config.save() is True
    config_dir = Config(base=base).dir
    wr = pet_profiles.write_profile_settings(config_dir, slot, {"scale": 0.8}, user_customized=True)
    assert wr.status == "ok", wr
    result["main_path"] = str(main_config.path)

    if mode == "confirm-discard":
        dialog = ModernSettingsDialog(Config(base=base, instance_id=f"slot-{slot}"),
                                      include_ai=False, standalone=True)
    elif mode == "unverified-notice":
        shared = settings_slot_config.SlotRoutedConfig(
            Config(base=base, instance_id=f"slot-{slot}"), main_config, slot)
        dialog = ModernSettingsDialog(shared, include_ai=False, standalone=False)
    else:
        raise SystemExit(f"unknown mode: {mode}")
    dialog.finished.connect(lambda code: result.__setitem__("finished", int(code)))

    # 模态框观察器：事件同步轮询 activeModalWidget（10ms 一拍），确认框点「放弃」、
    # 通知框点掉；每个框只处理一次。注意必须持框的**强引用**做去重——不能记 id()，
    # 框销毁后新框可能复用同一地址（Windows CI 实测：通知框被误判为已处理而漏点）。
    handled: list = []

    def poll():
        box = app.activeModalWidget()
        if box is None or any(box is prev for prev in handled):
            return
        handled.append(box)
        buttons = [b.text() for b in box.buttons()] if hasattr(box, "buttons") else []
        result["boxes"].append({
            "title": box.windowTitle(),
            "text": box.text() if hasattr(box, "text") else "",
            "buttons": buttons,
        })
        if DISCARD_LABEL in buttons:
            for b in box.buttons():
                if b.text() == DISCARD_LABEL:
                    b.click()
                    result["clicked"].append(DISCARD_LABEL)
                    break
        else:
            box.accept()          # 通知框：点掉即可

    poller = QTimer(app)
    poller.setInterval(10)
    poller.timeout.connect(poll)
    poller.start()

    def watchdog():
        # 兜底：流程真挂住时收掉当前模态框让流程解开，结果字段缺失由父进程断言变红。
        result["watchdog_fired"] = True
        box = app.activeModalWidget()
        if box is not None:
            box.reject()

    QTimer.singleShot(15000, watchdog)

    if mode == "confirm-discard":
        dialog.scale_combo.setCurrentIndex(dialog.scale_combo.findData(1.0))
        gen = pet_profiles.begin_slot_generation(config_dir, slot)   # 宠物重建 = 代次 +1
        assert gen.ok, gen
    else:
        dialog.pet_opacity_spin.setValue(35)
        Path(main_config.path).write_text('{ "pet_opacity": 55, broken', encoding="utf-8")

    QTest.keyClick(dialog, Qt.Key.Key_Escape)   # 关窗 → 真确认框（→ 放弃 → 真通知框）
    app.processEvents()

    result["disk_scale"] = pet_profiles.read_slot_profile(config_dir, slot).value("scale")
    _emit(result)


if __name__ == "__main__":
    main()
