#!/usr/bin/env python3
"""Micron script-block parser tests — imports micron only, no Kivy.

The `=sanguis fence opens a {"type": "script", "lang": "sanguis"}
block; the closer is still the plain "`=" line. An unterminated
script block is emitted as a code block (fail-closed: partial source
is never executed). Plain code blocks, directives, headings, and
paras must parse exactly as before.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import micron  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def main():
    # ---- basic script block ----
    b = micron.micron_parse(
        "before\n`=sanguis\ncell main() {\n  output 1.0;\n}\n`=\nafter")
    check("script block type + lang",
          b[1] == {"type": "script", "lang": "sanguis",
                   "text": "cell main() {\n  output 1.0;\n}"}, b[1])
    check("text before script block is a para",
          b[0]["type"] == "para", b[0])
    check("text after script block resumes parsing",
          b[2]["type"] == "para"
          and b[2]["runs"] == [{"text": "after"}], b[2])

    # ---- script source is preserved verbatim (incl. leading `c`) ----
    # The old parser mangled this: `cell main() {` lost its `c` to the
    # centered-line rule and the closer opened a runaway literal.
    src = ("cell main() {\n  let n = 6.0;\n  print(\"hello\");\n"
           "  output n * 7.0;\n}")
    b = micron.micron_parse("`=sanguis\n" + src + "\n`=\n")
    check("script text preserved verbatim (leading c kept)",
          len(b) == 1 and b[0]["type"] == "script"
          and b[0]["text"] == src, b)

    # ---- unterminated script block -> code block, never script ----
    b = micron.micron_parse("`=sanguis\ncell main() {\n  output 1.0;\n}")
    check("unterminated script block is a code block",
          b == [{"type": "code",
                 "text": "cell main() {\n  output 1.0;\n}"}], b)
    b = micron.micron_parse("head\n`=sanguis\npartial source")
    check("unterminated script after a para is code, page intact",
          len(b) == 2 and b[0]["type"] == "para"
          and b[1] == {"type": "code", "text": "partial source"}, b)

    # ---- plain code blocks unaffected ----
    b = micron.micron_parse("before\n`=\nraw `!x!` text\nline2\n`=\nafter")
    check("plain literal block still code",
          b[1] == {"type": "code", "text": "raw `!x!` text\nline2"}, b[1])
    b = micron.micron_parse("`=\nunterminated")
    check("unterminated literal still code",
          b == [{"type": "code", "text": "unterminated"}], b)

    # ---- opener/closer edge cases ----
    b = micron.micron_parse("`=sanguis\n`=\n")
    check("empty script block",
          b == [{"type": "script", "lang": "sanguis", "text": ""}], b)
    b = micron.micron_parse("`=sanguis  \ncell main() {\n  output 2.0;\n}\n`=\n")
    check("opener with trailing spaces still opens",
          len(b) == 1 and b[0]["type"] == "script"
          and b[0]["text"] == "cell main() {\n  output 2.0;\n}", b)
    b = micron.micron_parse("`=sanguisx\nnot a script\n")
    check("`=sanguisx is not an opener (exact match only)",
          b[0]["type"] == "para", b[0])
    b = micron.micron_parse("`=Sanguis\nnot a script\n")
    check("uppercase opener is not an opener",
          b[0]["type"] == "para", b[0])
    b = micron.micron_parse("x `=sanguis\nnot an opener mid-line\n")
    check("opener must be the whole line",
          b[0]["type"] == "para", b[0])

    # ---- two script blocks, and script + code mixed ----
    b = micron.micron_parse(
        "`=sanguis\ncell main() {\n  output 1.0;\n}\n`=\n"
        "`=\nplain code\n`=\n"
        "`=sanguis\ncell main() {\n  output 2.0;\n}\n`=\n")
    check("two script blocks with a code block between",
          len(b) == 3 and b[0]["type"] == "script"
          and b[1] == {"type": "code", "text": "plain code"}
          and b[2]["type"] == "script"
          and b[2]["text"] == "cell main() {\n  output 2.0;\n}", b)

    # ---- script body lines are not parsed as micron ----
    b = micron.micron_parse(
        "`=sanguis\n> not a heading\n- not a sep\n#!x=y\n`=\n")
    check("directive/heading/sep lines inside script stay source",
          len(b) == 1 and b[0]["type"] == "script"
          and b[0]["text"] == "> not a heading\n- not a sep\n#!x=y", b)

    # ---- directives / headings / paras unaffected ----
    b = micron.micron_parse("#!c=0\n#!bg=13a\n> Title\n-\ntext line")
    check("directives/headings/seps/paras unchanged",
          b[0] == {"type": "directive", "key": "c", "value": "0"}
          and b[2]["type"] == "heading" and b[3] == {"type": "sep"}
          and b[4]["type"] == "para", b)
    b = micron.micron_parse("c centered `!bold!` line")
    check("centered line with formatting unchanged",
          b[0]["type"] == "para" and b[0]["center"] is True
          and b[0]["runs"][1] == {"text": "bold", "bold": True}, b[0])
    b = micron.micron_parse("")
    check("empty page -> no blocks", b == [])

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
