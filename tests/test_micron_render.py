#!/usr/bin/env python3
"""Micron parser unit tests (site channels) — imports micron only, no Kivy.

The parser turns NomadNet Micron pages into structured blocks. These tests pin
the grammar the guild emits (md_to_micron output + page renderers) and the
tolerance rules for malformed input.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import micron  # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

def blocks(text):
    return micron.micron_parse(text)

def main():
    # ---- directives + blocks ----
    b = blocks("#!c=0\n#!bg=13a\n> Title\n-\ntext line")
    check("directive parsed", b[0] == {"type": "directive", "key": "c", "value": "0"}
          and b[1]["key"] == "bg" and b[1]["value"] == "13a", b[:2])
    check("heading block", b[2]["type"] == "heading"
          and b[2]["runs"] == [{"text": "Title"}], b[2])
    check("separator block", b[3] == {"type": "sep"})
    check("plain para", b[4]["type"] == "para" and b[4]["center"] is False
          and b[4]["runs"] == [{"text": "text line"}], b[4])

    # ---- background resolution: page directive beats manifest theme ----
    check("page bg wins over theme",
          micron.micron_page_bg(blocks("#!bg=13a\nx"), {"bg": "223"}) == "#1133aa",
          micron.micron_page_bg(blocks("#!bg=13a\nx"), {"bg": "223"}))
    check("theme bg used when the page has none",
          micron.micron_page_bg(blocks("x"), {"bg": "223"}) == "#222233")
    check("no bg anywhere -> None", micron.micron_page_bg(blocks("x")) is None)
    check("invalid bg -> None", micron.micron_page_bg(blocks("#!bg=zz\nx")) is None)

    # ---- formatting runs ----
    r = blocks("a `!bold!` b")[0]["runs"]
    check("bold run", r == [{"text": "a "}, {"text": "bold", "bold": True},
                            {"text": " b"}], r)
    r = blocks("`*it*`")[0]["runs"]
    check("italic run", r == [{"text": "it", "italic": True}], r)
    r = blocks("`_under_`")[0]["runs"]
    check("underline run", r == [{"text": "under", "under": True}], r)
    r = blocks("`F8a8cyan`f tail")[0]["runs"]
    check("fg color run carries its text", r == [{"text": "cyan", "fg": "#88aa88"},
                                                 {"text": " tail"}], r)
    r = blocks("`B333inverse`b")[0]["runs"]
    check("bg color run", r == [{"text": "inverse", "bg": "#333333"}], r)
    r = blocks("x`F8a8no-close")[0]["runs"]
    check("unclosed color run degrades to literal",
          r == [{"text": "x`F8a8no-close"}], r)

    # ---- links ----
    r = blocks("`[label`:/page/index.mu]")[0]["runs"]
    check("colon-prefixed target stripped", r == [{"text": "label",
                                                   "link": "/page/index.mu"}], r)
    r = blocks("`[Send`:/page/sHades/update.mu`msg|channel=abc]")[0]["runs"]
    check("link vars ignored", r[0]["link"] == "/page/sHades/update.mu", r)
    r = blocks("`[profile`1a2b3c4d]")[0]["runs"]
    check("bare node-hash target kept", r[0]["link"] == "1a2b3c4d", r)
    r = blocks("`[pics`:/file/sites/%s/img/pic.png]" % ("a" * 64))[0]["runs"]
    check("site media link target", r[0]["link"] == "/file/sites/" + "a" * 64 + "/img/pic.png", r)

    # ---- media detection ----
    check("image ext detected",
          micron.micron_media_target("/file/sites/x/img/pic.png") == ".png")
    check("video ext detected", micron.micron_media_target("/file/a/b.webm") == ".webm")
    check("non-media ext rejected", micron.micron_media_target("/file/a/page.mu") is None)
    check("non-file target rejected", micron.micron_media_target("/page/index.mu") is None)
    check("empty target rejected", micron.micron_media_target("") is None)

    # ---- centered lines + escaping ----
    b = blocks("cNo channels yet.")
    check("centered line", b[0]["center"] is True and b[0]["runs"] == [{"text": "No channels yet."}], b)
    b = blocks("\\> not a heading")
    check("line-start escape -> literal para", b[0]["type"] == "para"
          and b[0]["runs"] == [{"text": "> not a heading"}], b)
    r = blocks("pay \\$5 and a\\`b")[0]["runs"]
    check("inline escapes", r == [{"text": "pay $5 and a`b"}], r)

    # ---- literal blocks ----
    b = blocks("before\n`=\nraw `!x!` text\nline2\n`=\nafter")
    check("literal block keeps backticks inert",
          b[1] == {"type": "code", "text": "raw `!x!` text\nline2"}, b[1])
    check("text after literal block resumes parsing", b[2]["runs"] == [{"text": "after"}], b[2])
    b = blocks("`=\nunterminated")
    check("unterminated literal block still emitted", b[0]["type"] == "code"
          and b[0]["text"] == "unterminated", b)

    # ---- partials ----
    r = blocks("`{:/page/sHades/messages.mu`8`channel=abc}")[0]["runs"]
    check("partial becomes a link", r == [{"text": "/page/sHades/messages.mu",
                                           "link": "/page/sHades/messages.mu"}], r)

    # ---- tolerance ----
    b = blocks("")
    check("empty page -> no blocks", b == [])
    r = blocks("stray ` backtick")[0]["runs"]
    check("stray backtick literal", r == [{"text": "stray ` backtick"}], r)
    b = blocks("-\n-\n-")
    check("separator lines", b == [{"type": "sep"}] * 3, b)
    r = blocks("`F8a8red`f`B333blue`b`!b!`")[0]["runs"]
    check("adjacent runs sequence correctly",
          r == [{"text": "red", "fg": "#88aa88"}, {"text": "blue", "bg": "#333333"},
                {"text": "b", "bold": True}], r)

    # ---- markup assembly (uses the kivy_client escaper shape) ----
    def esc(s):
        s = s.replace("&", "&" + "amp;")
        return s.replace("[", "&bl;").replace("]", "&br;")
    m = micron.runs_to_markup([{"text": "a[b"}, {"text": "go", "bold": True},
                               {"text": "x", "link": "/p/1"}], esc)
    check("markup escapes brackets", "&bl;b" in m
          and m.count("[") == 4, m)   # 2 bold tags + 2 ref tags only
    check("markup wraps bold + ref",
          "[b]go[/b]" in m and "[ref=/p/1]x[/ref]" in m, m)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
