# opus 1.5.2 for the sHades android APK (voice/p2p-opus).
#
# The pinned p4a revision 9562d1b4f306d0af1e76991c711114010dea4152 has no
# opus recipe of any name (receipt: the recipe tree has no opus/libopus
# entry and no recipe file mentions opus), so naming `opus` in
# buildozer.spec requirements put the name into p4a's pip group and pip
# died on "No matching distribution found for opus" (run 45381551657,
# job 138404504953). This recipe closes that gap: CI injects it into the
# p4a clone after the pin check-out (build.yml, "Prefetch p4a" step),
# where the requirement name `opus` finds it and keeps it out of the pip
# group.
#
# Autotools pattern copied from p4a's own liblzma recipe at the same
# revision (it is the closest native-lib example: shared-only build,
# install into a p4a_install prefix, built_libraries copy). The
# built_libraries copy lands libopus.so at ctx.get_libs_dir(arch.arch),
# which the APK packs as lib/arm64-v8a/libopus.so - exactly the name the
# voice_codec probe appends to applicationInfo.nativeLibraryDir
# (voice_codec.py: "libopus.so" under _android_native_lib_dir()).
#
# Tarball receipt: https://downloads.xiph.org/releases/opus/opus-1.5.2.tar.gz
# sha256 65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1
# (downloaded and hashed 2026-11-24; the tarball ships ./configure).
#
# APK-level gate: build.yml fails the job if the APK does not carry
# lib/arm64-v8a/libopus.so, so a silent recipe drop-out can never ship.

import sh

from multiprocessing import cpu_count
from os.path import join

from pythonforandroid.archs import Arch
from pythonforandroid.logger import shprint
from pythonforandroid.recipe import Recipe
from pythonforandroid.util import current_directory


class LibOpusRecipe(Recipe):
    version = "1.5.2"
    url = "https://downloads.xiph.org/releases/opus/opus-{version}.tar.gz"
    built_libraries = {"libopus.so": "p4a_install/lib"}

    def build_arch(self, arch: Arch) -> None:
        env = self.get_recipe_env(arch)
        install_dir = join(self.get_build_dir(arch.arch), "p4a_install")
        with current_directory(self.get_build_dir(arch.arch)):
            shprint(
                sh.Command("./configure"),
                "--host=" + arch.command_prefix,
                "--prefix=" + install_dir,
                "--enable-shared",
                "--disable-static",
                "--disable-doc",
                "--disable-extra-programs",
                _env=env,
            )
            shprint(sh.make, "-j", str(cpu_count()), _env=env)
            shprint(sh.make, "install", _env=env)


recipe = LibOpusRecipe()
