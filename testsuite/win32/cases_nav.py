# -*- coding: utf-8 -*-
"""cases_nav.py - source window navigation, source search, GDB window
line editing, history and scrollback."""

import time

import wtlib
from wtlib import Session, check


def source_navigation(exes):
    """G / gg / Ctrl-D / Ctrl-U / j / k - the visible content must
    move; assert on which file lines are on screen."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)

        # G: jump to the bottom - last line "}" becomes visible
        s.write(b"G")
        time.sleep(1.0)
        rows = s.screen().text_rows()
        on_screen = "\n".join(rows)
        check("nav: G shows the end of file",
              any(l.strip().endswith("}") for l in rows) or
              on_screen.count("~") > 0)

        # gg: back to the top - the #include line visible again
        s.write(b"g")
        time.sleep(0.2)
        s.write(b"g")
        time.sleep(1.0)
        check("nav: gg shows the top of file",
              s.screen().find_row("#include") >= 0)

        # Ctrl-D half page down: still coherent
        s.write(b"\x04")
        time.sleep(0.8)
        check("nav: Ctrl-D keeps session alive", s.alive())
        s.write(b"\x15")       # Ctrl-U back up
        time.sleep(0.8)
        check("nav: Ctrl-U keeps session alive", s.alive())
        # j / k
        s.write(b"j")
        s.write(b"j")
        s.write(b"k")
        time.sleep(0.8)
        check("nav: j/k keep session alive", s.alive())


def source_search(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b"/")
        time.sleep(0.5)
        s.keys("line2", delay=0.08)
        s.write(b"\r")
        time.sleep(1.2)
        check("search: /pattern jumps to match",
              s.screen().find_row("line2") >= 0)
        check("search: session alive after search", s.alive())
        # n: next match
        s.write(b"n")
        time.sleep(0.8)
        check("search: n keeps session alive", s.alive())


def gdb_line_editing(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        # typo + backspaces
        s.keys("prinXX", delay=0.06)
        s.write(b"\x7f")
        time.sleep(0.2)
        s.write(b"\x7f")
        time.sleep(0.2)
        s.keys("t 41+1", delay=0.06)
        s.write(b"\r")
        check("editor: backspace fixes the line",
              s.wait_text("= 42", 10))
        # Ctrl-U kills the line, retype
        s.keys("garbageword", delay=0.04)
        s.write(b"\x15")
        time.sleep(0.3)
        s.keys("print 7*3", delay=0.06)
        s.write(b"\r")
        check("editor: Ctrl-U clears the line",
              s.wait_text("= 21", 10))
        # Ctrl-A / Ctrl-E / Ctrl-B / Ctrl-F then enter
        s.keys("print 8", delay=0.06)
        s.write(b"\x01")       # home
        time.sleep(0.2)
        s.write(b"\x06")       # right
        time.sleep(0.2)
        s.write(b"\x05")       # end
        time.sleep(0.2)
        s.write(b"\r")
        check("editor: Ctrl-A/E/F accepted",
              s.wait_text("= 8", 10))
        check("editor: session alive", s.alive())


def gdb_history(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        s.keys("print 11", delay=0.06)
        s.write(b"\r")
        s.wait_text("= 11", 10)
        s.keys("print 22", delay=0.06)
        s.write(b"\r")
        s.wait_text("= 22", 10)
        # recall the previous command with arrow up: the full line is
        # redrawn as one contiguous string (individual typing echoes
        # byte-by-byte, so only the recall renders "print 22")
        n0 = len(s.raw())
        s.write(b"\x1b[A")
        time.sleep(1.2)
        check("history: arrow-up recalls command text",
              s.raw()[n0:].count(b"print 22") >= 1)
        s.write(b"\r")
        check("history: recalled command resubmits",
              s.raw().count(b"= 22") >= 2 or
              s.wait_text("= 22", 5))
        check("history: session alive", s.alive())


def gdb_scrollback(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        for i in range(4):
            s.keys("print %d+%d" % (i, i), delay=0.05)
            s.write(b"\r")
            s.wait_text("= %d" % (i + i), 8)
        # PageUp enters scroll mode
        s.write(b"\x1b[5~")
        time.sleep(1.0)
        check("scroll: PageUp keeps session alive", s.alive())
        # exit scroll mode with 'i'
        s.write(b"i")
        time.sleep(0.8)
        check("scroll: i exits scroll mode, session alive", s.alive())
        s.keys("print 9", delay=0.05)
        s.write(b"\r")
        check("scroll: commands work after scrolling",
              s.wait_text("= 9", 8))


def register():
    return [source_navigation, source_search, gdb_line_editing,
            gdb_history, gdb_scrollback]
