# -*- coding: utf-8 -*-
"""cases_session.py - startup display, focus switching, status bar.

The status bar cases reproduce the ':' crash reported against the
first release (key-up records waking the kui with no data).
"""

import time

import wtlib
from wtlib import Session, check

INF = "t_basic"


def _spawn(exes, args=None):
    return Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes[INF]) if
                   args is None else args)


def startup_display(exes):
    with _spawn(exes) as s:
        ok = s.wait_text("(gdb)", 15)
        check("session: gdb prompt appears", ok)
        time.sleep(0.8)
        txt = s.screen().dump()
        check("session: source window shows code",
              "printf" in txt and "return" in txt)
        check("session: line numbers rendered",
              any(l.strip().startswith("9") or
                  l.strip().startswith("10") or
                  l.strip().startswith(" 9") or
                  l.strip().startswith(" 10")
                  for l in txt.splitlines()))
        check("session: status bar with file path",
              s.screen().find_row("t_basic.c") >= 0)
        check("session: process alive at prompt", s.alive())


def focus_switch(exes):
    """ESC -> source focus, i -> GDB focus, verified by behavior:
    from source focus, space toggles a breakpoint; from GDB focus,
    typing runs in the GDB window."""
    with _spawn(exes) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        # source focus, then space -> breakpoint request
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b" ")
        check("focus: space in source window sets bp",
              s.wait_text("Breakpoint 1", 10))
        # back to GDB focus, type a command
        s.write(b"i")
        time.sleep(0.6)
        s.keys("print 5+5", delay=0.08)
        s.write(b"\r")
        check("focus: i returns to GDB window",
              s.wait_text("= 10", 10))
        check("focus: session still alive", s.alive())


def status_bar_typing(exes):
    """The reported crash: ':' then idle at the prompt."""
    with _spawn(exes) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":")
        time.sleep(2.0)      # idle at the status bar - used to die here
        check("statusbar: alive after ':' + idle", s.alive())
        check("statusbar: ':' prompt visible",
              s.screen().find_row(":") >= 0)
        s.keys("q", delay=0.3)
        s.write(b"\r")
        ec = s.wait_exit(10)
        check("statusbar: :q quits cleanly", ec == 0)


def status_bar_edit(exes):
    """Type an unknown command with backspaces, then run it."""
    with _spawn(exes) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":")
        time.sleep(0.5)
        s.keys("setxx", delay=0.1)
        for _ in range(2):
            s.write(b"\x7f")
            time.sleep(0.2)
        s.keys(" foo", delay=0.1)   # line now "set foo"
        s.write(b"\r")
        time.sleep(1.0)
        # Note: upstream overwrites the "Unknown command" status with
        # if_draw() before doupdate (same on Linux - verified in a pty),
        # so the assertion is "handled without damage":
        check("statusbar: bad command handled without crash",
              s.alive())
        check("statusbar: typing still works after bad command",
              s.alive(1.0))
        s.write(b"i")      # focus back to the GDB window
        time.sleep(0.6)
        s.keys("print 3+3", delay=0.06)
        s.write(b"\r")
        check("statusbar: session usable after bad command",
              s.wait_text("= 6", 10))


def status_bar_cancel(exes):
    """:' then ESC cancels back to the previous focus."""
    with _spawn(exes) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":")
        time.sleep(0.6)
        s.write(b"\x1b")       # cancel the status bar
        time.sleep(1.3)
        check("statusbar: ESC cancel keeps session alive", s.alive())
        # ':' again still works afterwards
        s.write(b":")
        time.sleep(0.6)
        check("statusbar: ':' usable after cancel",
              s.alive(1.0) and s.screen().find_row(":") >= 0)


def rapid_typing(exes):
    """A burst of keys with release timing - the kui must survive."""
    with _spawn(exes) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.keys("print 1+2", delay=0.03)
        s.write(b"\r")
        check("robust: rapid burst command works",
              s.wait_text("= 3", 10))
        s.write(b"\x1b")
        time.sleep(0.05)
        s.write(b":")
        time.sleep(0.05)
        s.write(b"\x1b")
        time.sleep(0.05)
        s.write(b"i")
        time.sleep(0.05)
        s.keys("print 4*4", delay=0.02)
        s.write(b"\r")
        check("robust: ESC/:/i bursts keep session alive and working",
              s.wait_text("= 16", 10))


def register():
    return [startup_display, focus_switch, status_bar_typing,
            status_bar_edit, status_bar_cancel, rapid_typing]
