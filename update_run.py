"""P11 chain 2: the update handover + rollback launcher.

update_run.py replaces the running sHades process (python sHades.py
<args>), but through this script — spawned by updater's execv — so a
bundle that cannot boot rolls itself back. The supervised tree is the
directory this script lives in (the server's tree), resolved from
__file__ — never from CWD, which under systemd is not the tree and
silently disarming the rollback was the failure mode.

  marker update_staging/pending_version present = an update was swapped
  update_backup/                                = the previous members

Loop: run the tree's sHades.py with the original argv. While the marker
file exists:
    exit 0   -> the update boots: clear marker, exit 0
    exit !=0 -> rollback (copy update_backup/* over the tree), clear
                marker, restart ONCE; a second failure exits 1 with the
                backup intact.
No marker (a normal boot): just exec the server; a crash exits with its
own code, no rollback magic.
"""
import os
import shutil
import sys

MARKER = os.path.join("update_staging", "pending_version")
BACKUP = "update_backup"


def _restore(tree):
    made = 0
    bdir = os.path.join(tree, BACKUP)
    for name in sorted(os.listdir(bdir)):
        src = os.path.join(bdir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(tree, name))
            made += 1
    return made


def main():
    tree = os.path.dirname(os.path.abspath(__file__))
    app_args = sys.argv[1:]
    marker = os.path.join(tree, MARKER)
    attempt = 0
    while True:
        proc = subprocess_run(tree, app_args)
        if not os.path.isfile(marker):
            return proc.returncode if proc else 1
        if attempt == 0 and proc and proc.returncode == 0:
            try:
                os.remove(marker)
            except OSError:
                pass
            return 0
        # boot failed under the marker: restore + clear + retry once
        attempt += 1
        if attempt > 2:
            sys.stderr.write(
                "update_run: rollback restore failed twice (%s updates "
                "reapplied); leaving backup intact at %s\n"
                % (attempt - 1, os.path.join(tree, BACKUP)))
            return 2
        try:
            n = _restore(tree)
        except OSError as e:
            sys.stderr.write("update_run: restore error: %s\n" % e)
            return 2
        try:
            os.remove(marker)
        except OSError:
            pass
        sys.stderr.write(
            "update_run: boot failed (exit=%s); restored %d files from "
            "%s, retrying\n" % (getattr(proc, "returncode", None), n, BACKUP))


def subprocess_run(tree, app_args):
    import subprocess
    env = dict(os.environ)
    env["SHADES_UPDATE_RUN"] = "1"  # P11f: marks the supervised child, so
    # the tree's own boot guard stays out of the launcher's rollback way
    script = os.path.join(tree, "sHades.py")
    argv = [sys.executable, script] + list(app_args)
    return subprocess.run(argv, cwd=tree, env=env)


if __name__ == "__main__":
    sys.exit(main())
