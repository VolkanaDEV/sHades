"""The real client restores its RNS identity without replacing an old key."""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import kivy_client as kc  # noqa: E402
import recovery  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as folder:
        with patch.object(kc, "_app_data_dir", lambda: folder):
            app = kc.sHadesClientApp()
            key_path = app._ensure_identity_file()
            with open(key_path, "rb") as stream:
                original = stream.read()
            app.username = "Maya"
            kit = recovery.create_kit(original, "a long private phrase",
                                      {"username": "Maya", "friends": [], "servers": []})
            kit_path = os.path.join(folder, "kit.shades")
            recovery.write_private(kit_path, kit)
            with patch.object(app, "_ensure_identity_file", side_effect=AssertionError):
                # An existing identity is not silently overwritten.
                try:
                    app._restore_recovery_file(kit_path, "a long private phrase")
                except FileExistsError:
                    pass
                else:
                    raise AssertionError("restore replaced an existing key")
            os.unlink(key_path)
            assert app._restore_recovery_file(kit_path, "a long private phrase")
            with open(key_path, "rb") as stream:
                assert stream.read() == original
            assert os.path.isfile(app._onboarding_path())
            os.unlink(key_path)
            with patch.object(kc.dms, "save_friends", side_effect=OSError("full disk")):
                assert not app._restore_recovery_file(kit_path, "a long private phrase")
            assert os.path.isfile(key_path), "key survives optional metadata failure"
    print("PASS  RNS recovery preserves the identity and blocks overwrite")


if __name__ == "__main__":
    main()
