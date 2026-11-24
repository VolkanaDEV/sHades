#!/usr/bin/env python3
"""Polish (a): screen switches use one short fade, instant under reduced motion.

Boots the real app under Xvfb and asserts STATE, never wall-clock timing
or frame counts:

- the manager's transition is a FadeTransition at 0.18s (motion on)
- ui_fx.set_reduced_motion(True) retimes the live manager to 0, and
  set_reduced_motion(False) restores 0.18
- switching connect -> main -> connect changes `current` and completes
- switching still completes with reduced motion on (instant landing)
- a manager registered while reduced motion is on starts instant
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.uix.screenmanager import FadeTransition, ScreenManager  # noqa: E402
import ui_fx  # noqa: E402
from kivy_client import sHadesClientApp  # noqa: E402


class TransitionProbe(sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.checks = 0
        Clock.schedule_once(self.step_transition_type, 1.5)

    def check(self, condition, message):
        self.checks += 1
        print(("PASS " if condition else "FAIL ") + message, flush=True)
        if not condition:
            self.failures.append(message)

    def step_transition_type(self, dt):
        sm = self.root
        self.check(isinstance(sm, ScreenManager),
                   "root is a ScreenManager")
        self.check(isinstance(sm.transition, FadeTransition),
                   "manager transition is a FadeTransition (got %r)"
                   % type(sm.transition).__name__)
        self.check(ui_fx.SCREEN_TRANSITION_DURATION == 0.18,
                   "screen fade spec is 0.18s (got %r)"
                   % ui_fx.SCREEN_TRANSITION_DURATION)
        self.check(sm.transition.duration == 0.18,
                   "transition duration is 0.18 with motion on (got %r)"
                   % sm.transition.duration)
        self.check(sm.current == "connect",
                   "app boots on the connect screen (got %r)" % sm.current)
        Clock.schedule_once(self.step_reduced_on, 0.3)

    def step_reduced_on(self, dt):
        ui_fx.set_reduced_motion(True)
        try:
            self.check(self.root.transition.duration == 0,
                       "reduced motion retimes live transition to 0 (got %r)"
                       % self.root.transition.duration)
            # A manager registered while reduced motion is on starts instant.
            sm2 = ScreenManager()
            sm2.transition = FadeTransition(
                duration=ui_fx.SCREEN_TRANSITION_DURATION)
            ui_fx.register_screen_manager(sm2)
            self.check(sm2.transition.duration == 0,
                       "manager registered under reduced motion starts at 0"
                       " (got %r)" % sm2.transition.duration)
            # Switching still completes with reduced motion on.
            self.root.current = "main"
        finally:
            pass
        Clock.schedule_once(self.step_reduced_switch_landed, 0.5)

    def step_reduced_switch_landed(self, dt):
        self.check(self.root.current == "main",
                   "switch to main completes with reduced motion on "
                   "(got %r)" % self.root.current)
        self.check(self.root.current_screen is not None
                   and self.root.current_screen.name == "main",
                   "current_screen is main after the reduced-motion switch")
        ui_fx.set_reduced_motion(False)
        self.check(self.root.transition.duration == 0.18,
                   "re-enabling motion restores 0.18 (got %r)"
                   % self.root.transition.duration)
        self.root.current = "connect"
        Clock.schedule_once(self.step_back_on_connect, 0.5)

    def step_back_on_connect(self, dt):
        self.check(self.root.current == "connect",
                   "switch back to connect completes (got %r)"
                   % self.root.current)
        # And one more forward switch with the fade live.
        self.root.current = "main"
        Clock.schedule_once(self.step_main_again, 0.5)

    def step_main_again(self, dt):
        self.check(self.root.current == "main",
                   "switch to main completes with the fade live (got %r)"
                   % self.root.current)
        ui_fx.set_reduced_motion(False)
        print("PASS polish (a) screen transition: %d checks, %d failed"
              % (self.checks, len(self.failures)), flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    probe = TransitionProbe()
    probe.run()
    sys.exit(bool(probe.failures))
