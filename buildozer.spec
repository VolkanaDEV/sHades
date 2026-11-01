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
android.permissions = INTERNET,ACCESS_NETWORK_STATE,READ_EXTERNAL_STATE,WRITE_EXTERNAL_STORAGE
android.api = 33
android.minapi = 24
android.ndk = 25b
android.sdk = 23
android.accept_sdk_license = True
android.entrypoint = kivy_client.py
android.manifest_template = AndroidManifest.tmpl
android.extra_android_arguments = -
android.copy_assets = sHades.py
android.services = sHades.py
p4a.branch = develop
p4a.local_endpoint = -

[buildozer]

log.level = 2
warn_on_root = 1
buildozer_version = 1.5.0
