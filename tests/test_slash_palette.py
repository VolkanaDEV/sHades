#!/usr/bin/env python3
"""Command-palette slash parsing tests (tier-2).

parse_slash_help must track the hub dispatcher's /help output (the
palette is the discovery surface for slash commands / bot & script
integration). These are pure-string tests: sHades.py's literal help
table is pasted here and asserted end to end so a dispatcher verb
addition that forgets the format fails here, in CI, before a device
ever shows an empty palette.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kivy_client import parse_slash_help  # noqa: E402

# sHades.py _dispatch_command help table (verbatim, sHades.py:2733-2751)
HELP_TEXT = "\r\n".join([
    "commands:",
    "  help                this list",
    "  who                 who is online (and where they are)",
    "  channels            channel index",
    "  msg <channel> <text>   post <text> to an existing channel",
    "  info <channel>      channel details",
    "  presence            the active user list",
    "  uptime              guild uptime",
    "  create <name> [kind]   new channel (site/term: moderator)",
    "  delete <channel>    delete a channel (moderator)",
    "  rename <channel> <name>   rename (moderator)",
    "  topic <channel> <text>    set the channel topic (moderator)",
    "  ban <user> / unban <user>        moderator",
    "  mute <user> / unmute <user>      moderator",
    "  clear               clear the terminal",
    "  quit                close this session",
])


class TestParseSlashHelp(unittest.TestCase):
    def test_full_table(self):
        rows = parse_slash_help(HELP_TEXT)
        cmds = [c for c, _ in rows]
        self.assertEqual(cmds, ["/help", "/who", "/channels", "/msg",
                                "/info", "/presence", "/uptime", "/create",
                                "/delete", "/rename", "/topic", "/ban",
                                "/mute", "/clear", "/quit"])

    def test_usage_collapses_whitespace(self):
        rows = dict(parse_slash_help(HELP_TEXT))
        self.assertEqual(rows["/msg"], "<channel> <text> post <text> to "
                         "an existing channel")
        self.assertEqual(rows["/help"], "this list")
        self.assertEqual(rows["/who"], "who is online (and where they are)")

    def test_garbage_and_empty(self):
        self.assertEqual(parse_slash_help(""), [])
        self.assertEqual(parse_slash_help(None), [])
        self.assertEqual(parse_slash_help("freeform text no indent"), [])
        self.assertEqual(parse_slash_help("commands:"), [])

    def test_new_verbs_surface(self):
        # a future bot-registered verb in the same format must appear
        rows = parse_slash_help(HELP_TEXT + "\r\n  roll <expr>   dice bot")
        self.assertIn(("/roll", "<expr> dice bot"), rows)


if __name__ == "__main__":
    unittest.main()
