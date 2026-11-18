"""Guild isolation, bounds, metadata-only storage and edits while offline."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import offline_store  # noqa: E402


class OfflineStoreTests(unittest.TestCase):
    def test_isolation_bound_and_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cache.sqlite3")
            rows = [{"msg_id": str(i), "ts": i, "content": "message %d" % i,
                     "author_hash": "user", "attachments": [
                         {"name": "photo", "size": 900, "blob": b"secret"}]}
                    for i in range(8)]
            offline_store.put(path, "guild-a", "general", rows, cap=5)
            offline_store.put(path, "guild-b", "general", [rows[0]])
            self.assertEqual([m["msg_id"] for m in offline_store.recent(
                path, "guild-a", "general")], ["3", "4", "5", "6", "7"])
            self.assertEqual(len(offline_store.recent(path, "guild-b", "general")), 1)
            offline_store.put(path, "guild-b", "general", [
                {"msg_id": "0", "deleted_at": 123}])
            self.assertEqual(offline_store.recent(path, "guild-b", "general"), [])
            offline_store.put(path, "guild-b", "general", [rows[0]])
            self.assertEqual(offline_store.recent(path, "guild-a", "private"), [])
            with open(path, "rb") as stream:
                self.assertNotIn(b"secret", stream.read())
            offline_store.apply_update(path, "guild-a", "general", "7", "edited")
            self.assertEqual(offline_store.recent(path, "guild-a", "general")[-1]
                             ["content"], "edited")
            offline_store.apply_update(path, "guild-a", "general", "7", deleted=True)
            self.assertEqual(len(offline_store.recent(path, "guild-a", "general")), 4)
            offline_store.forget_author(path, "guild-a", "user")
            self.assertEqual(offline_store.all_messages(path, "guild-a"), [])
            offline_store.forget_guild(path, "guild-a")
            self.assertEqual(offline_store.all_messages(path, "guild-a"), [])
            self.assertEqual(len(offline_store.all_messages(path, "guild-b")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
