"""P11 (chain 1): the version-bridge pairing lane.

The one contract every updater depends on: buildozer.spec's version,
the client's APP_VERSION and the server's SERVER_VERSION are the same
string. Drift here means a client reports one version while the
release channel publishes another, so this lane runs on every CI
pair. Also proves the jnius leg of _client_version() (the device
half returns the APK's own versionName; the fallback returns the
constant).
"""
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, extra=None):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)


def spec_version():
    with open(os.path.join(ROOT, "buildozer.spec"), encoding="utf-8") as f:
        m = re.search(r"^version = (\S+)", f.read(), re.M)
        return m.group(1) if m else None


def client_version():
    src = open(os.path.join(ROOT, "kivy_client.py"), encoding="utf-8").read()
    m = re.search(r'^APP_VERSION = "([^"]+)"', src, re.M)
    return m.group(1) if m else None


def server_version():
    src = open(os.path.join(ROOT, "sHades.py"), encoding="utf-8").read()
    m = re.search(r'^SERVER_VERSION = "([^"]+)"', src, re.M)
    return m.group(1) if m else None


S = spec_version()
C = client_version()
V = server_version()
check("buildozer.spec carries a version", S is not None)
check("client APP_VERSION constant present", C is not None)
check("server SERVER_VERSION constant present", V is not None)
check("client APP_VERSION == buildozer.spec", C == S, C)
check("server SERVER_VERSION == buildozer.spec", V == S, V)

# The fallback half: kivy_client without jnius answers the constant.
os.environ["KIVY_GL_BACKEND"] = "mock"
os.environ["KIVY_NO_ARGS"] = "1"
import kivy_client as kc  # noqa: E402

v = kc._client_version()
check("no jnius -> _client_version() returns APP_VERSION",
      v == kc.APP_VERSION, v)

# The device half: a jnius shim carries the APK's versionName.
class _FakeInfo:
    versionName = "0.36.99-fakeapk"


def _fake_autoclass(name):
    info = _FakeInfo()
    holder = types.SimpleNamespace(getPackageInfo=lambda n, f: info)
    return types.SimpleNamespace(mActivity=types.SimpleNamespace(
        getPackageManager=lambda: holder,
        getPackageName=lambda: "com.shades.test"))


saved = {k: v for k, v in sys.modules.items() if k == "jnius"}
fake_jnius = types.ModuleType("jnius")
fake_jnius.autoclass = _fake_autoclass
sys.modules["jnius"] = fake_jnius
try:
    v2 = kc._client_version()
finally:
    if saved:
        sys.modules["jnius"] = saved["jnius"]
    else:
        sys.modules.pop("jnius", None)
check("device half: the APK's versionName wins", v2 == "0.36.99-fakeapk", v2)

# The server constant is importable and matches the spec (drift guard).
import sHades  # noqa: E402

check("imported SERVER_VERSION == buildozer.spec",
      sHades.SERVER_VERSION == S, sHades.SERVER_VERSION)

print()
print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
