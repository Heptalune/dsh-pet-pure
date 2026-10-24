"""Live bubble styles must follow the owning profile through every refresh."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest

from pet import pet_profiles
from pet.speech_bubble import BUBBLE_STYLE_PRESETS, PetSpeechBubble
from tests.test_sprite_menu_facade import _make_shell, app


@pytest.mark.parametrize("style", list(BUBBLE_STYLE_PRESETS))
def test_global_refresh_keeps_the_saved_profile_style_and_scale(tmp_path, style):
    shell, _ = _make_shell(tmp_path, {"self_talk_bubble_style": "classic_top"})
    bubble = PetSpeechBubble()
    shell._bubble_follower.bubble = bubble
    try:
        shell.overlay.show()
        app.processEvents()
        pet_profiles.write_profile_settings(tmp_path, 0,
            {"self_talk_bubble_style": style, "bubble_text_scale": 150})
        for _ in range(3):
            shell.refresh_settings()
            app.processEvents()
            assert bubble.style_id == style
            assert bubble.text_scale == pytest.approx(1.5)
        assert shell._sprite_config(shell.sprite).get("self_talk_bubble_style") == style
    finally:
        shell._bubble_follower.close()
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_child_bubble_does_not_inherit_global_style_when_slot0_is_absent(tmp_path):
    shell, _ = _make_shell(tmp_path, {"self_talk_bubble_style": "classic_top"})
    try:
        shell.overlay.show()
        shell.spawn_pet()
        child = shell._spawned[0]
        bubble = PetSpeechBubble()
        shell._bubble_followers[child].bubble = bubble
        slot = shell._spawned_slots[child]
        pet_profiles.write_profile_settings(tmp_path, slot,
            {"self_talk_bubble_style": "glass_right", "bubble_text_scale": 175})
        shell.exit_pet(shell.sprite)
        shell.refresh_settings()
        app.processEvents()
        assert bubble.style_id == "glass_right"
        assert bubble.text_scale == pytest.approx(1.75)
    finally:
        shell.clear_spawned_pets()
        shell.overlay.close()
        shell._delete_runtime_marker()


@pytest.mark.parametrize("style", ["glass_right", "paper_left", "soft_blue_top", "breath_bubble"])
def test_visible_style_switch_rebuilds_geometry_without_restarting_lifetime(style):
    anchor = QRect(700, 600, 100, 130)
    bubble = PetSpeechBubble()
    reference = PetSpeechBubble(style_id=style)
    text = "Style changes keep the current message."
    try:
        bubble.show_text(text, anchor, 20_000, pet_scale=.8)
        reference.show_text(text, anchor, 20_000, pet_scale=.8)
        app.processEvents()
        bubble._pos_anim.stop() if bubble._pos_anim else None
        reference._pos_anim.stop() if reference._pos_anim else None
        reference.reflow(anchor, pet_scale=.8)
        remaining = bubble._hide_timer.remainingTime()
        bubble.set_style(style)
        app.processEvents()
        assert bubble.geometry() == reference.geometry()
        assert bubble._surface_geometry_key == reference._surface_geometry_key
        assert bubble._raw_text == text
        assert 0 < bubble._hide_timer.remainingTime() <= remaining
        bubble.set_style("classic_top")
        classic = PetSpeechBubble()
        try:
            classic.show_text(text, anchor, 20_000, pet_scale=.8)
            classic.reflow(anchor, pet_scale=.8)
            assert bubble.size() == classic.size()
        finally:
            classic.close()
    finally:
        bubble.close()
        reference.close()


def test_style_change_keeps_interactive_buttons_and_does_not_wake_hidden_bubble():
    bubble = PetSpeechBubble()
    clicked = []
    try:
        bubble.show_text("Approve this?", QRect(700, 600, 100, 130),
                         buttons=[("Approve", lambda: clicked.append(True))])
        button = bubble._interactive_buttons[0]
        bubble.set_style("glass_right")
        assert bubble._interactive_buttons[0] is button
        assert not bubble._hide_timer.isActive()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert clicked == [True]
        bubble.hide()
        bubble.set_style("paper_left")
        assert not bubble.isVisible()
    finally:
        bubble.close()
