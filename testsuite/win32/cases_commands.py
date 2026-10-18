# -*- coding: utf-8 -*-
"""cases_commands.py - ':' commands (set/help/unknown) and quit paths."""

import time

import wtlib
from wtlib import Session, check


def set_options(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)

        s.write(b":")
        time.sleep(0.5)
        s.keys("set syntax=off", delay=0.08)
        s.write(b"\r")
        time.sleep(0.8)
        check("cmd: :set syntax=off accepted",
              s.alive() and b"Unknown command" not in s.raw()[-400:])

        s.write(b":")
        time.sleep(0.5)
        s.keys("set syntax=on", delay=0.08)
        s.write(b"\r")
        time.sleep(0.8)
        check("cmd: :set syntax=on accepted", s.alive())

        s.write(b":")
        time.sleep(0.5)
        s.keys("set winminheight=4", delay=0.08)
        s.write(b"\r")
        time.sleep(0.8)
        check("cmd: :set winminheight accepted", s.alive())


def set_show(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":")
        time.sleep(0.5)
        s.keys("set", delay=0.08)
        s.write(b"\r")
        check("cmd: :set without value is handled", s.alive(1.0))


def unknown_command(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":")
        time.sleep(0.5)
        s.keys("nosuchcmd", delay=0.08)
        s.write(b"\r")
        time.sleep(1.0)
        # upstream replaces the message with if_draw() before it ever
        # reaches the physical screen (same on Linux), so assert the
        # no-damage contract instead:
        check("cmd: unknown :command handled", s.alive())
        s.write(b"i")      # focus back to the GDB window
        time.sleep(0.6)
        s.keys("print 2*3", delay=0.06)
        s.write(b"\r")
        check("cmd: session usable after unknown command",
              s.wait_text("= 6", 10))


def help_command(exes):
    """':help' opens the cgdb manual if cgdb.txt exists; either way
    it must not crash."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":")
        time.sleep(0.5)
        s.keys("help", delay=0.08)
        s.write(b"\r")
        time.sleep(1.5)
        check("cmd: :help does not crash", s.alive())
        check("cmd: session usable after :help",
              s.alive(1.0))


def quit_paths(exes):
    """Main quit path: ESC + :q (status bar), exit code 0."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.5)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":q\r")
        ec = s.wait_exit(12)
        check("quit: ESC+:q exits 0", ec == 0, "exit=%s" % ec)


def quit_after_debug(exes):
    """Quit after a full debug session (teardown with live gdb)."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        s.keys("b main", delay=0.08)
        s.write(b"\r")
        s.wait_text("Breakpoint 1", 10)
        s.keys("run", delay=0.08)
        s.write(b"\r")
        s.wait_text("Breakpoint 1, main", 15)
        time.sleep(0.5)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":q\r")
        ec = s.wait_exit(12)
        check("quit: after a debug session exits 0", ec == 0,
              "exit=%s" % ec)


def quit_during_run(exes):
    """:q while the inferior is running (gdb busy)."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_sleep"])) as s:
        s.wait_text("(gdb)", 15)
        s.keys("run", delay=0.08)
        s.write(b"\r")
        s.wait_text("SLEEP: started", 10)
        time.sleep(0.5)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":q\r")
        ec = s.wait_exit(15)
        check("quit: during inferior run exits 0", ec == 0,
              "exit=%s" % ec)


def register():
    return [set_options, set_show, unknown_command, help_command,
            quit_paths, quit_after_debug, quit_during_run]
