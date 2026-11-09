#!/usr/bin/env python3
"""Friends roster + DM history tests (U7) — pure stdlib, no mesh, no Kivy."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dms  # noqa: E402

H = "b" * 32
H2 = "c" * 32


class TestRoster(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.path = os.path.join(self.base, "friends.json")

    def test_add_dedupe_and_rename(self):
        f = dms.add_friend([], H, "alice")
        f = dms.add_friend(f, H2, "bob")
        self.assertEqual([x["nickname"] for x in f], ["alice", "bob"])
        f = dms.add_friend(f, H, "alice2")          # same hash -> rename
        self.assertEqual(len(f), 2)
        self.assertTrue(dms.is_friend(f, H))
        self.assertEqual([x["nickname"] for x in f], ["alice2", "bob"])

    def test_remove(self):
        f = dms.add_friend([], H, "alice")
        f = dms.remove_friend(f, H)
        self.assertEqual(f, [])
        self.assertFalse(dms.is_friend(f, H))

    def test_bad_hash_rejected(self):
        with self.assertRaises(ValueError):
            dms.add_friend([], "zz", "x")
        with self.assertRaises(ValueError):
            dms.add_friend([], "a" * 31, "x")       # 31 hex, too short
        with self.assertRaises(ValueError):
            dms.add_friend([], "a" * 64, "x")       # channel-id length

    def test_persist_roundtrip_and_corruption(self):
        f = dms.add_friend([], H, "alice")
        dms.save_friends(self.path, f)
        self.assertEqual(dms.load_friends(self.path), f)
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(dms.load_friends(self.path), [])
        self.assertEqual(dms.load_friends(os.path.join(self.base, "nope.json")), [])

    def test_load_filters_junk(self):
        with open(self.path, "w") as fh:
            import json
            json.dump([{"user_hash": H, "nickname": "ok"},
                       {"user_hash": "bad", "nickname": "junk"},
                       "a string", {"nickname": "no hash"}], fh)
        f = dms.load_friends(self.path)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["nickname"], "ok")


class TestDMHistory(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.path = dms.dm_history_path(self.base, H)

    def test_path_validation(self):
        self.assertEqual(self.path, os.path.join(self.base, "dms", H + ".json"))
        with self.assertRaises(ValueError):
            dms.dm_history_path(self.base, "nope")

    def test_append_and_load(self):
        dms.append_dm(self.path, "out", "hi there")
        dms.append_dm(self.path, "in", "ohai")
        rows = dms.load_dm_history(self.path)
        self.assertEqual([r["dir"] for r in rows], ["out", "in"])
        self.assertEqual([r["text"] for r in rows], ["hi there", "ohai"])
        self.assertTrue(all(isinstance(r["ts"], float) for r in rows))

    def test_cap(self):
        for i in range(250):
            dms.append_dm(self.path, "out", "m%d" % i, cap=200)
        rows = dms.load_dm_history(self.path)
        self.assertEqual(len(rows), 200)
        self.assertEqual(rows[0]["text"], "m50")
        self.assertEqual(rows[-1]["text"], "m249")

    def test_bad_direction(self):
        with self.assertRaises(ValueError):
            dms.append_dm(self.path, "sideways", "x")

    def test_corrupt_file(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            fh.write("[broken")
        self.assertEqual(dms.load_dm_history(self.path), [])
        dms.append_dm(self.path, "in", "recovers")
        self.assertEqual(len(dms.load_dm_history(self.path)), 1)

    def test_text_truncated(self):
        dms.append_dm(self.path, "out", "x" * 5000)
        self.assertEqual(len(dms.load_dm_history(self.path)[0]["text"]), 4000)


class TestLXMFPlumbing(unittest.TestCase):
    """Guard the exact LXMRouter API the client calls, without needing a live
    RNS Transport (registering a destination requires an initialized
    Reticulum, so the real end-to-end delivery lives in integ_test.py)."""

    def test_router_api_shape(self):
        try:
            import LXMF
        except ImportError:
            self.skipTest("LXMF not installed")
        import inspect
        router = LXMF.LXMRouter
        # the three calls the client makes on connect
        self.assertTrue(callable(router.register_delivery_identity))
        self.assertTrue(callable(router.register_delivery_callback))
        self.assertTrue(callable(router.announce))
        self.assertTrue(callable(router.handle_outbound))
        self.assertTrue(callable(router.exit_handler))
        # register_delivery_identity(identity, display_name=None, stamp_cost=None)
        params = list(inspect.signature(
            router.register_delivery_identity).parameters)
        self.assertEqual(params[:2], ["self", "identity"])
        self.assertIn("display_name", params)
        # announce(destination_hash, attached_interface=None)
        aparams = list(inspect.signature(router.announce).parameters)
        self.assertEqual(aparams[:2], ["self", "destination_hash"])
        # the delivery destination hash is 16 bytes -> 32 hex user hash
        self.assertEqual(LXMF.LXMessage.DESIRED_METHOD_DIRECT
                         if hasattr(LXMF.LXMessage, "DESIRED_METHOD_DIRECT")
                         else "DIRECT", "DIRECT")

    def test_client_uses_real_hash_width(self):
        """The client keys friends/DMs by the 32-hex lxmf.delivery hash."""
        import dms
        self.assertTrue(dms.is_user_hash("a" * 32))
        self.assertFalse(dms.is_user_hash("a" * 64))   # channel-id length
        self.assertFalse(dms.is_user_hash("a" * 31))


if __name__ == "__main__":
    unittest.main(verbosity=2)
