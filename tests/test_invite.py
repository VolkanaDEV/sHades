#!/usr/bin/env python3
"""Invite share-link parser tests (U6a) — pure stdlib, no mesh, no Kivy."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import invite  # noqa: E402

H = "a" * 32


class TestInvite(unittest.TestCase):
    @staticmethod
    def d(r):
        assert r is not None, "parse_invite returned None"
        return r

    def test_full_invite(self):
        r = self.d(invite.parse_invite("shades://192.168.1.50:29221/" + H))
        self.assertEqual(r, {"host": "192.168.1.50", "port": 29221,
                             "node_hash": H})

    def test_roundtrip_build_parse(self):
        s = invite.build_invite("10.0.0.7", 42421, H)
        self.assertEqual(s, "shades://10.0.0.7:42421/" + H)
        self.assertEqual(self.d(invite.parse_invite(s)),
                         {"host": "10.0.0.7", "port": 42421, "node_hash": H})

    def test_address_only(self):
        self.assertEqual(self.d(invite.parse_invite("shades://10.0.0.7:42421")),
                         {"host": "10.0.0.7", "port": 42421, "node_hash": None})

    def test_host_only(self):
        self.assertEqual(self.d(invite.parse_invite("shades://guild.example.org")),
                         {"host": "guild.example.org", "port": None,
                          "node_hash": None})

    def test_whitespace_and_case(self):
        r = self.d(invite.parse_invite("  \nSHADES://10.0.0.7:42421/%s \n" % H.upper()))
        self.assertEqual(r["host"], "10.0.0.7")
        self.assertEqual(r["port"], 42421)
        self.assertEqual(r["node_hash"], H.lower())

    def test_bracketed_ipv6(self):
        r = self.d(invite.parse_invite("shades://[::1]:42421/" + H))
        self.assertEqual(r["host"], "::1")
        self.assertEqual(r["port"], 42421)
        self.assertEqual(r["node_hash"], H)

    def test_trailing_slash(self):
        r = self.d(invite.parse_invite("shades://10.0.0.7:42421/%s/" % H))
        self.assertEqual(r["node_hash"], H)

    def test_invalid_hash_ignored(self):
        r = self.d(invite.parse_invite("shades://10.0.0.7:42421/nothex"))
        self.assertIsNone(r["node_hash"])
        r = self.d(invite.parse_invite("shades://10.0.0.7:42421/" + "a" * 31))
        self.assertIsNone(r["node_hash"])

    def test_invalid_port(self):
        r = self.d(invite.parse_invite("shades://10.0.0.7:99999/" + H))
        self.assertIsNone(r["port"])
        self.assertEqual(r["host"], "10.0.0.7")
        r = self.d(invite.parse_invite("shades://10.0.0.7:abc/" + H))
        self.assertIsNone(r["port"])

    def test_not_an_invite(self):
        for t in ("", None, "192.168.1.50:42421", "http://10.0.0.7:80/x",
                  "shades:10.0.0.7", "shades//10.0.0.7"):
            self.assertIsNone(invite.parse_invite(t), t)

    def test_connect_style_rebuild(self):
        """The client rebuilds 'host:port' from the parsed invite for the
        existing ip:port connect path."""
        inv = invite.parse_invite("shades://192.168.1.50:29221/" + H)
        hub = "%s:%s" % (inv["host"], inv["port"])
        self.assertEqual(hub, "192.168.1.50:29221")


if __name__ == "__main__":
    unittest.main(verbosity=2)
