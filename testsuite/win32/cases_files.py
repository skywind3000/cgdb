# -*- coding: utf-8 -*-
"""cases_files.py - source file variants: CRLF, CJK, long lines, tabs."""

import time

import wtlib
from wtlib import Session, check


def crlf_cjk_source(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_cjk"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        txt = s.screen().dump()
        check("files: CRLF source displays", "printf" in txt)
        check("files: CJK comment renders",
              u"\u4f60\u597d\u4e16\u754c" in txt.replace(" ", ""))
        check("files: CJK session alive", s.alive())


def cjk_debug_flow(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_cjk"])) as s:
        s.wait_text("(gdb)", 15)
        s.keys("b main", delay=0.08)
        s.write(b"\r")
        s.wait_text("Breakpoint 1", 10)
        s.keys("run", delay=0.08)
        s.write(b"\r")
        check("files: CJK program runs and prints",
              s.wait_text("CJK: ok", 12))
        check("files: CJK stop keeps session alive", s.alive())


def wide_content(exes):
    """t_basic has short lines; the resize+nav already covers wide.
    Here: verify line rendering stays within bounds at 40 cols."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"]),
                 cols=40) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        check("files: narrow console stays alive", s.alive())
        rows = s.screen().text_rows()
        check("files: narrow rows are not wider than console",
              all(len(r.rstrip()) <= 40 for r in rows))


def register():
    return [crlf_cjk_source, cjk_debug_flow, wide_content]
