#!/usr/bin/env python3
"""Polish (c): drawer slide + popup entrance.

Asserts STATE, never wall-clock timing or frame counts. The 0.22s
drawer slide and the 0.15s popup fade are driven deterministically:
the running Kivy Animation is advanced by explicit _update(dt)
calls, so no real time has to elapse.

- opening a drawer slides its width 0 -> open width (mid-slide the
  width is strictly partial, at rest it is exactly the open width)
- closing slides back to 0; the boolean still drives the result
- opening one drawer closes the other (boolean semantics preserved)
- REDUCED_MOTION lands the width at its target at once, no animation
- a mid-slide toggle does not wedge: the width ends at a valid
  endpoint and the boolean matches it
- resizing with a drawer open re-lands its width at the new open
  width (the KV formula used to recompute from root.width)
- the swipe classifier still sees the animated width: an open drawer
  offers its inner-border close band, a closed drawer its edge band
- popup_entrance: opacity starts at 0, passes through a partial
  opacity mid-fade, lands at 1; REDUCED_MOTION opens at opacity 1
  at once; a failed open() still raises (and restores opacity 1)

Real MainScreen / Popup widgets under Xvfb; the screen is bare
(no KV ids), so swipe-band fakes stand in for the sidebars.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402
import ui_fx  # noqa: E402
from kivy.animation import Animation  # noqa: E402
from kivy.uix.label import Label  # noqa: E402
from kivy.uix.popup import Popup  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def _anim_for(widget, prop):
    for anim in list(Animation._instances):
        entry = getattr(anim, "_widgets", {}).get(widget.uid)
        if entry is not None and prop in entry.get("properties", {}):
            return anim
    return None


def _step(widget, prop, dt):
    """Advance the widget's running `prop` animation by dt seconds."""
    anim = _anim_for(widget, prop)
    if anim is None:
        return False
    anim._update(dt)
    return True


def _pump_to_end(widget, prop):
    for _ in range(10):
        if _anim_for(widget, prop) is None:
            break
        _step(widget, prop, 0.11)
    return getattr(widget, prop)


def make_main():
    main = kc.MainScreen()
    main.width = 800
    return main


def main():
    ui_fx.set_reduced_motion(False)

    # --- motion tokens ------------------------------------------------
    check("drawer slide spec is 0.22s",
          ui_fx.DRAWER_SLIDE_DURATION == 0.22,
          repr(ui_fx.DRAWER_SLIDE_DURATION))
    check("popup fade spec is 0.15s",
          ui_fx.POPUP_FADE_DURATION == 0.15,
          repr(ui_fx.POPUP_FADE_DURATION))
    check("left open width at 800 is 284",
          ui_fx.drawer_open_width(800, "left") == 284,
          repr(ui_fx.drawer_open_width(800, "left")))
    check("right open width at 800 is 200",
          ui_fx.drawer_open_width(800, "right") == 200,
          repr(ui_fx.drawer_open_width(800, "right")))
    check("left open width caps at 80% of a narrow screen",
          ui_fx.drawer_open_width(200, "left") == 160,
          repr(ui_fx.drawer_open_width(200, "left")))
    check("right open width caps at 72% of a narrow screen",
          ui_fx.drawer_open_width(200, "right") == 144,
          repr(ui_fx.drawer_open_width(200, "right")))

    main = make_main()
    check("drawers start closed at width 0",
          main.left_width == 0 and main.right_width == 0,
          repr((main.left_width, main.right_width)))

    # --- open slide: 0 -> 284, partial mid-slide ---------------------
    main.left_open = True
    check("opening sets the boolean at once", main.left_open is True)
    check("opening starts at width 0", main.left_width == 0,
          repr(main.left_width))
    check("opening starts a left_width animation",
          _anim_for(main, "left_width") is not None)
    check("opening starts no right_width animation",
          _anim_for(main, "right_width") is None)
    _step(main, "left_width", 0.11)  # first update arms the clock
    check("width still 0 after the arming update", main.left_width == 0,
          repr(main.left_width))
    _step(main, "left_width", 0.11)  # halfway through the 0.22s slide
    check("width is mid-slide (0 < width < 284)",
          0 < main.left_width < 284, repr(main.left_width))
    check("slide still running at mid-slide",
          _anim_for(main, "left_width") is not None)
    check("width lands at 284",
          _pump_to_end(main, "left_width") == 284,
          repr(main.left_width))
    check("slide finished after landing",
          _anim_for(main, "left_width") is None)

    # --- mutual exclusion: opening right closes left ------------------
    main.right_open = True
    check("opening right clears the left boolean",
          main.left_open is False and main.right_open is True)
    check("closing left starts a left_width animation",
          _anim_for(main, "left_width") is not None)
    check("opening right starts a right_width animation",
          _anim_for(main, "right_width") is not None)
    check("left width lands at 0",
          _pump_to_end(main, "left_width") == 0,
          repr(main.left_width))
    check("right width lands at 200",
          _pump_to_end(main, "right_width") == 200,
          repr(main.right_width))

    # --- close slide: 200 -> 0 ----------------------------------------
    main.right_open = False
    check("closing starts at width 200", main.right_width == 200,
          repr(main.right_width))
    _step(main, "right_width", 0.11)
    _step(main, "right_width", 0.11)
    check("closing width is mid-slide (0 < width < 200)",
          0 < main.right_width < 200, repr(main.right_width))
    check("closing width lands at 0",
          _pump_to_end(main, "right_width") == 0,
          repr(main.right_width))

    # --- reduced motion: instant landing, no animation -----------------
    ui_fx.set_reduced_motion(True)
    try:
        main.left_open = True
        check("reduced-motion open lands at 284 at once",
              main.left_width == 284, repr(main.left_width))
        check("reduced-motion open starts no animation",
              _anim_for(main, "left_width") is None)
        main.left_open = False
        check("reduced-motion close lands at 0 at once",
              main.left_width == 0, repr(main.left_width))
        check("reduced-motion close starts no animation",
              _anim_for(main, "left_width") is None)
    finally:
        ui_fx.set_reduced_motion(False)

    # --- mid-slide toggle does not wedge -------------------------------
    main.left_open = True
    _step(main, "left_width", 0.11)
    _step(main, "left_width", 0.11)
    mid = main.left_width
    check("mid-slide width is partial before the toggle",
          0 < mid < 284, repr(mid))
    main.left_open = False  # close mid-slide
    check("mid-slide toggle keeps the boolean False",
          main.left_open is False)
    check("mid-slide close lands at 0",
          _pump_to_end(main, "left_width") == 0,
          repr(main.left_width))
    main.left_open = True
    _step(main, "left_width", 0.11)
    main.left_open = False  # close again, one arming step in
    check("second mid-slide close lands at 0",
          _pump_to_end(main, "left_width") == 0,
          repr(main.left_width))
    main.left_open = True
    _step(main, "left_width", 0.11)
    _step(main, "left_width", 0.11)
    main.left_open = True  # no-op set while already open: no wedge
    check("re-setting the open flag mid-slide still lands at 284",
          _pump_to_end(main, "left_width") == 284,
          repr(main.left_width))
    main.left_open = False
    _pump_to_end(main, "left_width")

    # --- resize re-lands an open drawer at the new open width ---------
    main.width = 800
    main.left_open = True
    _pump_to_end(main, "left_width")
    check("open at 800 is 284", main.left_width == 284,
          repr(main.left_width))
    main.width = 200
    check("resize to 200 re-lands the open drawer at 160",
          main.left_width == 160, repr(main.left_width))
    main.width = 800
    check("resize back to 800 re-lands the open drawer at 284",
          main.left_width == 284, repr(main.left_width))
    main.left_open = False
    _pump_to_end(main, "left_width")
    main.width = 200
    check("resize while closed keeps the drawer at 0",
          main.left_width == 0, repr(main.left_width))
    main.width = 800

    # --- swipe classifier still reads the animated width --------------
    left_fake = types.SimpleNamespace(width=0, x=0)
    right_fake = types.SimpleNamespace(width=0, x=800)
    main.ids["left_sidebar"] = left_fake
    main.ids["right_sidebar"] = right_fake
    touch = types.SimpleNamespace(x=5, y=100)
    check("closed drawer: left edge band plans left_open",
          main._swipe_plan(touch) == "left_open",
          repr(main._swipe_plan(touch)))
    main.left_open = True
    _pump_to_end(main, "left_width")
    left_fake.width = main.left_width
    touch = types.SimpleNamespace(x=left_fake.width - 5, y=100)
    check("open drawer: inner-border band plans left_close",
          main._swipe_plan(touch) == "left_close",
          repr(main._swipe_plan(touch)))
    main.left_open = False
    _pump_to_end(main, "left_width")
    left_fake.width = 0

    # --- popup entrance: 0 -> 1, partial mid-fade ---------------------
    pop = Popup(title="probe", content=Label(text="hi"),
                size_hint=(0.5, 0.5))
    ui_fx.popup_entrance(pop)
    try:
        check("popup opens (in the window)", pop.parent is not None)
        check("popup starts at opacity 0", pop.opacity == 0,
              repr(pop.opacity))
        check("popup starts an opacity animation",
              _anim_for(pop, "opacity") is not None)
        _step(pop, "opacity", 0.075)  # arming update
        check("popup still 0 after the arming update", pop.opacity == 0,
              repr(pop.opacity))
        _step(pop, "opacity", 0.075)  # halfway through the 0.15s fade
        check("popup is mid-fade (0 < opacity < 1)",
              0 < pop.opacity < 1, repr(pop.opacity))
        check("popup lands at opacity 1",
              _pump_to_end(pop, "opacity") == 1, repr(pop.opacity))
        check("popup animation finished after landing",
              _anim_for(pop, "opacity") is None)
    finally:
        pop.dismiss()

    # --- popup entrance under reduced motion ---------------------------
    ui_fx.set_reduced_motion(True)
    try:
        pop2 = Popup(title="probe2", content=Label(text="hi"),
                     size_hint=(0.5, 0.5))
        try:
            ui_fx.popup_entrance(pop2)
            check("reduced-motion popup opens at opacity 1 at once",
                  pop2.opacity == 1, repr(pop2.opacity))
            check("reduced-motion popup starts no animation",
                  _anim_for(pop2, "opacity") is None)
        finally:
            pop2.dismiss()
    finally:
        ui_fx.set_reduced_motion(False)

    # --- a failed open() still raises (and restores opacity 1) --------
    def _boom():
        raise RuntimeError("no window")

    broken = types.SimpleNamespace(opacity=1, open=_boom)
    try:
        ui_fx.popup_entrance(broken)
        check("failed popup open raises", False, "no exception")
    except RuntimeError:
        check("failed popup open raises", True)
    check("failed popup open restores opacity 1",
          broken.opacity == 1, repr(broken.opacity))

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
