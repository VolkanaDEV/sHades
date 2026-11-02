[app]

# sHades Android build spec
title = sHades
package.name = shades
package.domain = io.eldeucebag
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = .git, .github, tests, __pycache__, build, dist, .buildozer
version = 0.1
requirements = python3,kivy,rns,lxmf
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE
android.api = 33
android.minapi = 24
android.accept_sdk_license = True
p4a.branch = develop

[buildozer]

log.level = 2
warn_on_root = 1
buildozer_version = 1.5.0