#!/usr/bin/env python3
"""GPU queue drainer tests -- imports gpu_drain only, no Kivy.

Covers: a recording fake backend proves commands arrive in order
with the right args; the default CPU backend shades a real,
non-blank image; unhandled commands are reported, never dropped;
empty queue yields image None and no notes; garbage input never
raises; the module stays Kivy-free so the tests run headless.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpu_drain  # noqa: E402
from gpu_drain import CpuPixelBackend, drain_gpu_commands, mandel_pixel  # noqa: E402
from PIL import Image  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def _distinct_colours(img):
    return len(set(img.getdata()))


def _mean_brightness(img):
    pixels = list(img.getdata())
    total = sum(r + g + b for r, g, b in pixels)
    return total / (3.0 * len(pixels)) if pixels else 0.0


class RecordingBackend:
    """Fake backend: records every call, returns canned readbacks."""

    def __init__(self, image_to_return=None):
        self.calls = []
        self.image_to_return = image_to_return

    def create_surface(self, handle, width, height):
        self.calls.append(("create_surface", handle, width, height))

    def upload(self, handle, values):
        self.calls.append(("upload", handle, list(values)))

    def dispatch(self, handle, frame):
        self.calls.append(("dispatch", handle, frame))

    def read_floats(self, handle, offset, count):
        self.calls.append(("read_floats", handle, offset, count))
        return [1.0, 2.0][:count]

    def read_plane(self, handle, plane):
        self.calls.append(("read_plane", handle, plane))
        return {"plane": plane, "count": 4, "min": 0, "max": 255,
                "mean": 10.0}

    def read_window(self, handle, x, y, w, h):
        self.calls.append(("read_window", handle, x, y, w, h))
        return {"x": x, "y": y, "w": w, "h": h, "count": 4,
                "mean_r": 1.0, "mean_g": 2.0, "mean_b": 3.0}

    def zero(self, handle):
        self.calls.append(("zero", handle))

    def close(self, handle):
        self.calls.append(("close", handle))

    def image(self, handle):
        self.calls.append(("image", handle))
        return self.image_to_return


class ThrowingBackend(RecordingBackend):
    """Fake backend whose dispatch always faults: drain must survive it."""

    def dispatch(self, handle, frame):
        self.calls.append(("dispatch", handle, frame))
        raise RuntimeError("boom")


def main():
    # ---- module is Kivy-free ----
    check("no kivy imported by gpu_drain",
          "kivy" not in sys.modules, [m for m in sys.modules if "kivy" in m])
    check("all 21 names known",
          len(gpu_drain.ALL_GPU_COMMANDS) == 21
          and set(gpu_drain.ALL_GPU_COMMANDS) == set([
              "gpu_info", "gpu_open_cap", "gpu_dispatch", "gpu_open",
              "gpu_upload_f32", "gpu_read_f32", "gpu_read_plane",
              "gpu_read_planes", "gpu_zero", "gpu_read_window",
              "gpu_save_buffer", "gpu_load_buffer", "gpu_load_file",
              "gpu_edge_beat", "gpu_edge_mark", "gpu_edge_decay",
              "gpu_edge_walk", "gpu_matvec", "gpu_close",
              "gpu_hybrid_stats", "gpu_program"]),
          gpu_drain.ALL_GPU_COMMANDS)

    # ---- fake backend: order + args ----
    red = Image.new("RGB", (2, 2), (255, 0, 0))
    fake = RecordingBackend(image_to_return=red)
    queue = [["gpu_open", 1.0],
             ["gpu_upload_f32", 1.0, [1.0, 2.0]],
             ["gpu_dispatch", 1.0, 2.0],
             ["gpu_read_f32", 1.0, 0.0, 2.0],
             ["gpu_close", 1.0]]
    res = drain_gpu_commands(queue, width=80, height=60, backend=fake)
    check("fake backend sees commands in order with right args",
          fake.calls == [
              ("create_surface", 1.0, 80, 60),
              ("upload", 1.0, [1.0, 2.0]),
              ("dispatch", 1.0, 2),
              ("image", 1.0),
              ("read_floats", 1.0, 0, 2),
              ("close", 1.0)], fake.calls)
    check("fake backend image is the result image",
          res["image"] is red, res["image"])
    check("handled lists implemented names in order",
          res["handled"] == ["gpu_open", "gpu_upload_f32", "gpu_dispatch",
                             "gpu_read_f32", "gpu_close"], res["handled"])
    check("nothing unhandled for the live subset",
          res["unhandled"] == [], res["unhandled"])

    # ---- fake backend: plane + window readbacks recorded ----
    fake2 = RecordingBackend()
    res2 = drain_gpu_commands(
        [["gpu_open", 1.0], ["gpu_dispatch", 1.0, 0.0],
         ["gpu_read_plane", 1.0, 0.0], ["gpu_read_window", 1.0, 0.0, 0.0,
                                        2.0, 2.0], ["gpu_zero", 1.0]],
        width=80, height=60, backend=fake2)
    check("plane/window/zero reach the backend in order",
          fake2.calls == [
              ("create_surface", 1.0, 80, 60),
              ("dispatch", 1.0, 0),
              ("image", 1.0),
              ("read_plane", 1.0, 0),
              ("read_window", 1.0, 0, 0, 2, 2),
              ("zero", 1.0)], fake2.calls)
    check("plane stats land in notes",
          any("gpu_read_plane" in n and "mean 10.0" in n
              for n in res2["notes"]), res2["notes"])
    check("window stats land in notes",
          any("gpu_read_window" in n and "mean" in n
              for n in res2["notes"]), res2["notes"])

    # ---- CPU backend: a real, non-blank image ----
    cpu = CpuPixelBackend()
    res3 = drain_gpu_commands(
        [["gpu_open", 1.0],
         ["gpu_upload_f32", 1.0, [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]],
         ["gpu_dispatch", 1.0, 2.0],
         ["gpu_read_f32", 1.0, 0.0, 4.0]],
        width=80, height=60, backend=cpu)
    img = res3["image"]
    check("cpu backend produces an image",
          img is not None, res3)
    check("cpu image size and mode",
          img is not None and img.size == (80, 60) and img.mode == "RGB",
          None if img is None else (img.size, img.mode))
    check("cpu image is not solid black",
          img is not None and _mean_brightness(img) > 5.0,
          None if img is None else _mean_brightness(img))
    check("cpu image has varied colours",
          img is not None and _distinct_colours(img) > 20,
          None if img is None else _distinct_colours(img))
    check("readback values land in notes",
          any("gpu_read_f32" in n and "-0.75" in n for n in res3["notes"]),
          res3["notes"])

    # ---- default size is 160x120 ----
    res4 = drain_gpu_commands([["gpu_open", 1.0], ["gpu_dispatch", 1.0, 0.0]])
    check("default drain size is 160x120",
          res4["image"] is not None and res4["image"].size == (160, 120),
          None if res4["image"] is None else res4["image"].size)
    check("default image is not solid black",
          res4["image"] is not None
          and _mean_brightness(res4["image"]) > 5.0,
          None if res4["image"] is None
          else _mean_brightness(res4["image"]))

    # ---- pixel function: defined, deterministic, frame zooms ----
    params = list(gpu_drain.DEFAULT_PARAMS)
    check("mandel centre is inside the set (dark blue)",
          mandel_pixel(40, 30, 80, 60, params, 0) == (0, 0, 4),
          mandel_pixel(40, 30, 80, 60, params, 0))
    check("mandel corner escapes (not inside colour)",
          mandel_pixel(0, 0, 80, 60, params, 0) != (0, 0, 4),
          mandel_pixel(0, 0, 80, 60, params, 0))
    check("mandel pixel is deterministic",
          mandel_pixel(10, 20, 80, 60, params, 3)
          == mandel_pixel(10, 20, 80, 60, params, 3))
    img0 = drain_gpu_commands([["gpu_open", 1.0], ["gpu_dispatch", 1.0, 0.0]],
                              width=80, height=60)["image"]
    img8 = drain_gpu_commands([["gpu_open", 1.0], ["gpu_dispatch", 1.0, 8.0]],
                              width=80, height=60)["image"]
    check("later frame zooms (image differs)",
          img0 is not None and img8 is not None
          and list(img0.getdata()) != list(img8.getdata()))

    # ---- upload param mapping: partial upload pads with defaults ----
    cpu2 = CpuPixelBackend()
    drain_gpu_commands([["gpu_open", 1.0], ["gpu_upload_f32", 1.0, [2.0]]],
                       width=40, height=30, backend=cpu2)
    got_params = cpu2.surfaces[1]["params"]
    check("partial upload keeps defaults for the rest",
          got_params[0] == 2.0 and got_params[1] == 0.0
          and got_params[2] == 3.0 and got_params[3] == 48.0,
          got_params)
    check("upload stores the raw data buffer",
          cpu2.surfaces[1]["data"] == [2.0], cpu2.surfaces[1]["data"])

    # ---- unhandled: defined-but-not-implemented + unknown ----
    res5 = drain_gpu_commands(
        [["gpu_open", 1.0], ["gpu_dispatch", 1.0, 0.0],
         ["gpu_matvec", 1.0, 1.0], ["gpu_edge_beat", 1.0],
         ["gpu_hybrid_stats"], ["gpu_frobnicate", 1.0],
         ["gpu_save_buffer", 1.0, "x"]],
        width=40, height=30)
    check("unhandled lists defined + unknown names in order",
          res5["unhandled"] == ["gpu_edge_beat",
                                "gpu_hybrid_stats", "gpu_frobnicate",
                                "gpu_save_buffer"], res5["unhandled"])
    check("every unhandled command gets a visible note",
          all(any(name in n for n in res5["notes"])
              for name in res5["unhandled"]), res5["notes"])
    check("unhandled commands do not kill the image",
          res5["image"] is not None and res5["image"].size == (40, 30))
    check("gpu_info in a queue is noted, never executed",
          drain_gpu_commands([["gpu_info"]])["unhandled"] == ["gpu_info"])

    # ---- empty queue ----
    res6 = drain_gpu_commands([])
    check("empty queue: image None",
          res6["image"] is None, res6)
    check("empty queue: only a backend note",
          len(res6["notes"]) == 1
          and (res6["notes"][0] == "GPU backend: OpenGL ES"
               or res6["notes"][0] == "GPU backend: CPU"
               or res6["notes"][0].startswith(
                   "GPU backend: CPU (OpenGL ES unavailable:")),
          res6["notes"])
    check("empty queue: handled/unhandled empty",
          res6["handled"] == [] and res6["unhandled"] == [], res6)

    # ---- garbage input never raises ----
    garbage = [
        None, [], ["gpu_open"], [123, 1.0], ["gpu_open", "x"],
        ["gpu_open", 1.0], ["gpu_dispatch"], ["gpu_dispatch", 1.0, "bad"],
        ["gpu_dispatch", 99.0, 0.0], ["gpu_dispatch", 1.0, -3.0],
        ["gpu_upload_f32", 1.0, "nope"],
        ["gpu_upload_f32", 1.0, [1.0, "x", None]],
        ["gpu_upload_f32", 1.0, []],
        ["gpu_read_f32", 1.0, "a", "b"],
        ["gpu_read_plane", 1.0, 7.0], ["gpu_read_plane", 1.0],
        ["gpu_read_window", 1.0, 0.0],
        ["gpu_close", 1.0], ["gpu_close", 1.0],
        ["gpu_zero", 5.0], ("gpu_open", 2.0),
        "not a list", 42, 3.5,
    ]
    try:
        res7 = drain_gpu_commands(garbage, width=40, height=30)
        raised = False
    except Exception as exc:  # noqa: BLE001 - the test must see any raise
        res7 = None
        raised = exc
    check("garbage queue never raises",
          not raised, raised)
    check("garbage queue still drains the valid commands",
          res7 is not None and res7["image"] is not None
          and "gpu_open" in res7["handled"]
          and "gpu_dispatch" in res7["handled"], res7)
    check("garbage queue notes explain the skips",
          res7 is not None and len(res7["notes"]) >= 10, res7)
    for bad_width, bad_height in [("x", 30), (0, 30), (5000, 30), (40, -2)]:
        try:
            res8 = drain_gpu_commands([["gpu_open", 1.0],
                                       ["gpu_dispatch", 1.0, 0.0]],
                                      width=bad_width, height=bad_height)
            raised = False
        except Exception as exc:  # noqa: BLE001 - see above
            res8 = None
            raised = exc
        check("bad size %r x %r never raises" % (bad_width, bad_height),
              not raised and res8 is not None and res8["image"] is not None,
              raised)
    check("oversize width is clamped to 1024",
          drain_gpu_commands([["gpu_open", 1.0], ["gpu_dispatch", 1.0, 0.0]],
                             width=5000, height=30)["image"].size == (1024, 30))

    # ---- a throwing backend never escapes the drainer ----
    boom = ThrowingBackend()
    res9 = drain_gpu_commands([["gpu_open", 1.0], ["gpu_dispatch", 1.0, 0.0]],
                              width=40, height=30, backend=boom)
    check("throwing backend: no raise, image None",
          res9["image"] is None, res9)
    check("throwing backend: error is noted",
          any("backend error on gpu_dispatch" in n for n in res9["notes"]),
          res9["notes"])

    # ---- demo-shaped queue end to end ----
    demo = [["gpu_open", 1.0],
            ["gpu_upload_f32", 1.0, [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]],
            ["gpu_dispatch", 1.0, 2.0],
            ["gpu_read_f32", 1.0, 0.0, 4.0],
            ["gpu_edge_beat", 1.0],
            ["gpu_close", 1.0]]
    res10 = drain_gpu_commands(demo)
    check("demo queue: image survives the trailing close",
          res10["image"] is not None and res10["image"].size == (160, 120),
          res10)
    check("demo queue: edge_beat is the only unhandled name",
          res10["unhandled"] == ["gpu_edge_beat"], res10["unhandled"])
    check("demo queue: handled lists the live commands",
          res10["handled"] == ["gpu_open", "gpu_upload_f32", "gpu_dispatch",
                               "gpu_read_f32", "gpu_close"], res10["handled"])
    check("demo image is not solid black",
          res10["image"] is not None
          and _mean_brightness(res10["image"]) > 5.0)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
