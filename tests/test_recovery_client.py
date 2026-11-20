"""The real client restores its RNS identity without replacing an old key."""
import json
import os
import sys
import tempfile
import types
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


def main_transfer():
    """tay's device report (0.36.24, restored kit + DIRECT LINK guild):
    the restore left no session, no portable profile, and a manual
    ip:port reconnect wiped the kit's guild hashes - the account never
    transferred alive and the relaunch misrouted to staging. The lane
    receipts the transfer contract: restore writes the session and the
    portable profile, the enter helper dials the kit's guild with its
    own hashes, and a hashless reconnect inherits instead of wiping."""
    with tempfile.TemporaryDirectory() as folder:
        with patch.object(kc, "_app_data_dir", lambda: folder):
            app = kc.sHadesClientApp()
            # The kit the source device wrote: identity + a complete row
            # for the guild it was last active on.
            row = {"hub_address": "10.0.0.4:29221", "host": "10.0.0.4",
                   "port": 29221,
                   "node_hash": "a" * 32, "delivery_hash": "b" * 32,
                   "username": "Maya"}
            key_path = app._ensure_identity_file()
            with open(key_path, "rb") as stream:
                original = stream.read()
            kit = recovery.create_kit(
                original, "a long private phrase",
                {"username": "Maya", "friends": [], "servers": [row]})
            kit_path = os.path.join(folder, "trans_kit.shades")
            recovery.write_private(kit_path, kit)
            os.unlink(key_path)
            assert app._restore_recovery_file(kit_path, "a long private phrase")
            # the boot gate routes the restored key as a completed profile
            assert os.path.isfile(app._portable_profile_path()), \
                "portable profile missing after restore"
            sess = app._load_session()
            assert sess, "session missing after restore"
            assert sess.get("hub_address") == "10.0.0.4:29221", sess
            assert sess.get("hub_host") == "10.0.0.4" and sess.get("hub_port") == 29221
            assert sess.get("node_hash") == "a" * 32, sess
            assert sess.get("delivery_hash") == "b" * 32, sess
            assert sess.get("username") == "Maya", sess
            assert app._servers and app._servers[0]["node_hash"] == "a" * 32
            # the enter helper transfers the account alive
            stub = types.SimpleNamespace(
                current="x",
                get_screen=lambda name: _connect_stub())
            app.root = stub
            assert app._enter_restored_account() is True
            assert stub.current == "connect"
            assert app.guild_node_hash == "a" * 32, app.guild_node_hash
            assert app.delivery_hash == "b" * 32, app.delivery_hash
            assert app.hub_address == "10.0.0.4:29221" and app.hub_port == 29221
            assert app.reticulum is None, "no device RNS ran in-lane"
            # a manual reconnect that types only ip:port inherits the hashes
            app.connect_to_guild("10.0.0.4:29221", "Maya")
            assert app.guild_node_hash == "a" * 32, app.guild_node_hash
            assert app.delivery_hash == "b" * 32, app.delivery_hash
            assert app._servers[0]["node_hash"] == "a" * 32, \
                "row lost its guild hash on a hashless reconnect"
            _connect_stub().text = ""
            with open(app._portable_profile_path()) as source:
                assert json.load(source)["name"] == "Maya"
    print("PASS  restored account transfers alive (session, portable, hashes)")


def _connect_stub():
    return types.SimpleNamespace(text="")


if __name__ == "__main__":
    main_transfer()
    main()
