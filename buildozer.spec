[app]

# sHades Android build spec
title = sHades
package.name = shades
package.domain = io.eldeucebag
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 0.1
requirements = python3,kivy,rns,lxmf
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE
android.api = 33
android.minapi = 24
android.accept_sdk_license = True
android.entrypoint = kivy_client.py
p4a.branch = develop

[buildozer]

log.level = 2
warn_on_root = 1
buildozer_version = 1.5.0
