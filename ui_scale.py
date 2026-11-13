#!/usr/bin/env python3
"""Shared UI scale factors — NO Kivy, testable headless.

One place to scale UI lengths so the KV layout (kv_md.kv, via a KV
``#:import``) and the Python-built popups (kivy_client.py) stay identical
instead of drifting apart.

The factor used to be 2/3, which drew every button a third shorter than
its nominal height: a 40dp button became 26.67dp, and the smallest
reached 21px. Those are below the 48dp touch target, so the buttons were
hard to hit -- on a touch screen and with a mouse. The factor is now 1.0:
heights are written literally (dp(40) means 40px) and every button is at
least 40px tall. See DESIGN.md ("Buttons").

Only plain numbers here — no Kivy import — so the factor can be unit
tested without a display, like ui_geom.
"""

# 1.0 = no vertical shrink. Kept as a named constant so the scale stays
# adjustable in ONE place instead of being spread across every height.
BUTTON_V_SCALE = 1.0

# Minimum height for any button, in px. A touch target should be at
# least 48dp; 40px is the floor for the compact chrome in this client
# (sidebar toggles, popups) where a full 48px would crowd the layout.
BUTTON_MIN_HEIGHT = 40


def scaled(length):
    """Scale a length (px) by the shared button scale factor.

    Used for button heights: ``scaled(dp(40))`` -> 40px. The factor is
    currently 1.0, so this is a no-op that documents intent and keeps one
    place to change if the scale ever moves again.
    """
    return length * BUTTON_V_SCALE
