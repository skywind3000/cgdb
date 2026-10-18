# -*- coding: utf-8 -*-
"""cases_windows.py - split-window management and terminal resize."""

import time

import wtlib
from wtlib import Session, check


def split_grow_shrink(exes):
    """+/- change the split: the status bar row (file path) moves."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)
        base = s.screen().find_row("t_basic.c")
        check("split: status row located", base >= 0)

        s.write(b"+")
        time.sleep(0.8)
        grown = s.screen().find_row("t_basic.c")
        check("split: + grows source window", grown > base,
              "base=%s grown=%s" % (base, grown))
        check("split: session alive after +", s.alive())

        s.write(b"-")
        time.sleep(0.8)
        s.write(b"-")
        time.sleep(0.8)
        shrunk = s.screen().find_row("t_basic.c")
        # a smaller source window moves the status row UP
        check("split: - shrinks source window", shrunk < grown,
              "grown=%s shrunk=%s" % (grown, shrunk))
        check("split: session alive after -", s.alive())

        # keep shrinking must stop at winminheight, not crash
        for _ in range(8):
            s.write(b"-")
            time.sleep(0.15)
        check("split: extreme shrink does not crash", s.alive())


def conpty_resize(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.5)
        hr = s.resize(50, 130)
        time.sleep(1.2)
        check("resize: grow to 50x130 ok", hr == 0)
        check("resize: session alive after grow", s.alive(1.0))
        txt = s.raw().decode("utf-8", "replace")
        check("resize: content redrawn after grow",
              "printf" in txt and "(gdb)" in txt)

        hr = s.resize(24, 80)
        time.sleep(1.2)
        check("resize: shrink to 24x80 ok", hr == 0)
        check("resize: session alive after shrink", s.alive(1.0))

        # typing still works after both resizes
        s.keys("print 1+1", delay=0.06)
        s.write(b"\r")
        check("resize: commands work after resizes",
              s.wait_text("= 2", 10))


def tiny_terminal(exes):
    """A very small console must not crash the layout math."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"]),
                 rows=12, cols=40) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        check("resize: 12x40 session stays alive", s.alive(1.0))
        s.resize(6, 20)
        time.sleep(1.0)
        check("resize: 6x20 does not crash", s.alive(1.0))


def register():
    return [split_grow_shrink, conpty_resize, tiny_terminal]
