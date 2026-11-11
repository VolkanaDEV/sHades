[app]

# sHades Android build spec
title = sHades
package.name = shades
package.domain = io.eldeucebag
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = .git, .github, tests, __pycache__, build, dist, .buildozer
version = 0.1
# wasmtime runs the micron applet sandbox on-device (applet_bridge.py).
# It publishes an Android wheel tagged android_26_<abi>, so the NDK API
# must be 26+ for p4a to select it -- at android_24 pip silently falls
# back to the py3-none-any wheel, which carries only a Windows DLL and
# cannot be imported. There is no armeabi-v7a (32-bit ARM) wasmtime
# wheel, so that ABI is dropped; the applet feature is arm64-only.
requirements = python3,kivy,rns,lxmf,wasmtime
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

[buildozer]

log.level = 2
warn_on_root = 1
buildozer_version = 1.5.0