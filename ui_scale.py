#!/usr/bin/env python3
"""Shared UI scale factors — NO Kivy, testable headless.

Every button in the client is drawn a third shorter than the height it
would otherwise get: a 40dp button becomes 40 * 2/3 = 26.67dp, and so on.
The factor lives here so the KV layout (kv_md.kv, via a KV ``#:import``)
and the Python-built popups (kivy_client.py) scale identically instead of
drifting apart.

Only plain numbers here — no Kivy import — so the factor can be unit
tested without a display, like ui_geom.
"""

# Buttons are scaled down vertically by one third.
BUTTON_V_SCALE = 2.0 / 3.0


def scaled(length):
    """Scale a length (px) down by one third.

    Used for button heights: ``scaled(dp(40))`` -> 26.67px.
    """
    return length * BUTTON_V_SCALE
