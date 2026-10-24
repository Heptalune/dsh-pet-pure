# -*- coding: utf-8 -*-
"""0 sprite 停表回归（DS 审查 M12c：节能三复合项之三）。

现状：``TickDriver.detach`` 只在「再无 overlay」时停表（V-12 关窗即停），
覆盖不了「overlay 还在、但一只 sprite 都没有」的空窗期（主宠退出/子宠全退/
角色素材重建中）——tick 仍在按档运行，仿真段与推进段对着空列表空转。

要求：sprite 数 0 → 停 tick 定时器；有 sprite 加入 → 恢复（并同步回 T0）；
多 overlay 时以**全部**成员的聚合数为准（别的 overlay 还有 sprite 就继续跑）；
档位机其余语义不变。

纪律：offscreen；同步直调，不启动真实 QTimer 等待、不 sleep。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pet.overlay_window import OverlayWindow
from pet.tick_driver import TickDriver
from tests.test_tick_driver import FakeSprite

app = QApplication.instance() or QApplication([])


def test_real_overlay_sprite_count_drives_timer():
    """真实 overlay：sprite 归零停表，重新加入恢复。"""
    driver = TickDriver()
    overlay = OverlayWindow(driver=driver)
    sprite = FakeSprite()
    overlay.add_sprite(sprite)
    overlay.start()
    assert driver.timer.isActive()

    overlay.remove_sprite(sprite)
    assert not driver.timer.isActive(), "0 sprite 必须停表"

    overlay.add_sprite(sprite)
    assert driver.timer.isActive(), "有 sprite 加入必须恢复"


def test_start_without_sprites_does_not_run():
    """空 overlay 起动不起表（有 sprite 加入时才跑）。"""
    driver = TickDriver()
    overlay = OverlayWindow(driver=driver)
    overlay.start()
    assert not driver.timer.isActive()

    overlay.add_sprite(FakeSprite())
    assert driver.timer.isActive()


def test_other_overlay_sprites_keep_timer_running():
    """聚合口径：只要任一 overlay 还有 sprite，就不能停表。"""
    driver = TickDriver()
    first = OverlayWindow(driver=driver)
    second = OverlayWindow(driver=driver)
    sprite = FakeSprite()
    second.add_sprite(sprite)
    driver.start()
    assert driver.timer.isActive()

    first.add_sprite(FakeSprite())
    first.remove_sprite(first.sprites[0])
    assert driver.timer.isActive(), "另一个 overlay 还有 sprite：继续跑"

    second.remove_sprite(sprite)
    assert not driver.timer.isActive(), "全部 overlay 都空了才停"


def test_resume_restores_active_tier_and_elapsed():
    """恢复时同步回 T0（start 语义）且不吃停表期的历史流逝。"""
    from pet.tick_governor import TIER_ACTIVE, TIER_IDLE_STILL

    driver = TickDriver()
    overlay = OverlayWindow(driver=driver)
    sprite = FakeSprite()
    overlay.add_sprite(sprite)
    overlay.start()
    driver._apply_tier(TIER_IDLE_STILL)
    assert driver.applied_tier == TIER_IDLE_STILL

    overlay.remove_sprite(sprite)
    assert not driver.timer.isActive()
    overlay.add_sprite(sprite)
    assert driver.timer.isActive()
    assert driver.applied_tier == TIER_ACTIVE


def test_detach_resets_empty_suspend_flag():
    """最后一个 overlay 摘除后清掉"空窗停表"标记（重挂不误判为恢复）。"""
    driver = TickDriver()
    overlay = OverlayWindow(driver=driver)
    sprite = FakeSprite()
    overlay.add_sprite(sprite)
    overlay.start()
    overlay.remove_sprite(sprite)
    assert not driver.timer.isActive()

    driver.detach(overlay)
    assert driver.overlays == []
    assert driver._suspended_for_empty is False
