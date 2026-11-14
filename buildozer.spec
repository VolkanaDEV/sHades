[app]

# sHades Android build spec
title = sHades
package.name = shades
package.domain = io.eldeucebag
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = .git, .github, tests, __pycache__, build, dist, .buildozer
version = 0.5
# Bump version on every android-facing fix so the installed build can be
# verified on-device (Settings > Apps > sHades) without adb.
# wasmtime runs the micron applet sandbox on-device (applet_bridge.py).
# It publishes an Android wheel tagged android_26_<abi>, so the NDK API
# must be 26+ for p4a to select it -- at android_24 pip silently falls
# back to the py3-none-any wheel, which carries only a Windows DLL and
# cannot be imported. There is no armeabi-v7a (32-bit ARM) wasmtime
# wheel, so that ABI is dropped; the applet feature is arm64-only.
# kivymd + pillow are REQUIRED on this lane: kivy_client.py defines
# sHadesClientApp(MDApp) and imports PIL unguarded at module top, so an APK
# built without them dies in main() at import (NameError: MDApp not defined /
# ImportError: PIL) before any widget exists -- the black screen on launch.
# rns/lxmf/wasmtime pinned to match requirements.txt so both lanes ship
# identical trees.
# Full dependency surface, named explicitly. Receipts from metadata:
#   - kivymd==2.0.0 declares kivy>=2.3.0, pillow, materialyoucolor>=3.0.3,
#     materialshapes>=0.3, asynckivy>=0.6,<0.7 (unpinned names resolve the
#     same versions pip would; named here so nothing arrives unnoticed).
#   - rns==1.1.3 declares cryptography + pyserial. Name cryptography so its
#     p4a recipe compiles the native OpenSSL bindings against the NDK instead
#     of relying on metadata-only resolution in the target env.
# Touchscreen: no opt-in flag exists or is needed -- the android build is
# SDL2-based (sdl2/sdl2_image/sdl2_ttf/sdl2_mixer + libmtdev recipes, which
# p4a ships by default), and touch input on arm64-v8a needs minapi >= 26 +
# ndk_api 26, exactly as pinned below. api 33 = modern Play/permission model.
requirements = python3,kivy,kivymd==2.0.0,pillow,materialyoucolor,materialshapes,asynckivy,rns==1.1.3,cryptography,pyserial,lxmf==0.9.4,wasmtime==50.0.1,libbz2,liblzma
# libbz2 + liblzma: p4a's python3 recipe marks _bz2/_lzma as OPTIONAL
# opt_depends (pythonforandroid/recipes/python3/__init__.py: opt_depends
# + the `if library_name in self.ctx.recipe_build_order` check) -- the
# extensions only get compiled when the recipes are named in the
# requirements. Phone receipt (v0.4 crash console): ModuleNotFoundError:
# No module named '_bz2' at kivy_client -> import RNS (RNS/Buffer.py ->
# bz2.py -> _bz2) -- every black screen was this death at import time.
# Naming libbz2/liblzma makes CPython compile _bz2.so/_lzma.so for the
# android target with the right flags.
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE
android.api = 33
# minapi must equal ndk_api: p4a (develop) refuses --minsdk != the API the
# dist was compiled against ("You must pass --allow-minsdk-ndkapi-mismatch",
# github run 42926045356). The wasmtime android wheel is android_26_<abi>
# anyway (see comment above), so 26 is the floor for both.
android.minapi = 26
android.ndk_api = 26
android.archs = arm64-v8a
android.accept_sdk_license = True
p4a.branch = develop
# Target python version: selected in .github/workflows/build.yml via
# VERSION_python3 / VERSION_hostpython3 env (p4a's Recipe.version reads
# VERSION_<recipe-name> from the environment, pythonforandroid/recipe.py,
# and applies version-gated recipe patches -- 3.11 gets the
# cpython-311-ctypes-find-library patch). Kivy 2.3.1 officially supports
# python 3.8-3.13; p4a develop's recipe default (3.14.2) is outside that
# range and fails silently on the device: black screen, no traceback. A
# p4a.commit pin to the last-3.11-default (2ebea90d9) was tried and is
# REJECTED: the stale Jul-2025 toolchain fails at -luuid (_uuid stdlib
# extension) with today's NDK, plus pip backend failures in the
# isolated env. Modern recipes + env-selected python version is the fix.

[buildozer]

log.level = 2
warn_on_root = 1
buildozer_version = 1.5.0