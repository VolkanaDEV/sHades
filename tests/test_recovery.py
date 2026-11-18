"""Recovery kit confidentiality, integrity and overwrite protection."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import recovery  # noqa: E402


class RecoveryTests(unittest.TestCase):
    def test_round_trip_and_tamper(self):
        identity = os.urandom(64)
        kit = recovery.create_kit(identity, "twelve word secret", {"name": "Maya"})
        self.assertNotIn(identity, kit)
        self.assertNotIn(b"Maya", kit)
        self.assertEqual(recovery.open_kit(kit, "twelve word secret"),
                         (identity, {"name": "Maya"}))
        with self.assertRaisesRegex(ValueError, "wrong phrase"):
            recovery.open_kit(kit, "another wrong phrase")
        payload = json.loads(kit)
        payload["ciphertext"] = payload["ciphertext"][:-4] + "AAAA"
        with self.assertRaises(ValueError):
            recovery.open_kit(json.dumps(payload).encode(), "twelve word secret")

    def test_guards_and_private_write(self):
        with self.assertRaises(ValueError):
            recovery.create_kit(os.urandom(64), "short")
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "rns", "identity")
            recovery.write_private(target, b"new identity")
            if os.name != "nt":
                # os.chmod 0600 is a POSIX contract; Windows maps the
                # write_private hardening onto the read-only bit and
                # reports 0666 to stat regardless. The mechanism (the
                # chmod call) is unchanged; only the stat proof is
                # POSIX-checkable, so the mode assertion skips on nt.
                self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                recovery.write_private(target, b"other identity")
            with open(target, "rb") as stream:
                self.assertEqual(stream.read(), b"new identity")


if __name__ == "__main__":
    unittest.main(verbosity=2)
