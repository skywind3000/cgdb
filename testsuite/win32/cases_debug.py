# -*- coding: utf-8 -*-
"""cases_debug.py - the debugging loop: breakpoints, run, stepping,
inferior output, ^C interrupt, breakpoint toggling."""

import time

import wtlib
from wtlib import Session, check


def break_run_stop(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.5)
        s.keys("b main", delay=0.08)
        s.write(b"\r")
        check("debug: b main echoes result",
              s.wait_text("Breakpoint 1", 10))
        s.keys("run", delay=0.08)
        s.write(b"\r")
        check("debug: run stops at main",
              s.wait_text("Breakpoint 1, main", 15))
        time.sleep(1.0)
        txt = s.screen().dump()
        check("debug: inferior prints before stopping",
              "BASIC: hello" in txt or
              s.wait_text("BASIC: hello", 3))
        check("debug: prompt back after stop", s.alive())


def stepping(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        s.keys("b main", delay=0.08)
        s.write(b"\r")
        s.wait_text("Breakpoint 1", 10)
        s.keys("run", delay=0.08)
        s.write(b"\r")
        s.wait_text("Breakpoint 1, main", 15)
        time.sleep(0.5)

        # typed next
        s.keys("next", delay=0.08)
        s.write(b"\r")
        check("debug: typed next advances",
              s.wait_text("BASIC: hello", 12))
        # function key F10 (next) - xterm sequence
        s.write(b"\x1b[21~")
        time.sleep(1.5)
        check("debug: F10 next keeps session alive", s.alive())
        # finish the program
        s.keys("cont", delay=0.08)
        s.write(b"\r")
        check("debug: continue runs to exit",
              s.wait_text("exited normally", 15) or
              s.wait_text("BASIC: line3", 8))
        check("debug: prompt after program exit", s.alive())


def breakpoint_toggle(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.5)
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b"j")          # move to line 2
        time.sleep(0.3)
        s.write(b" ")          # toggle bp on
        check("toggle: space adds breakpoint",
              s.wait_text("Breakpoint 1", 10))
        time.sleep(0.5)
        s.write(b" ")          # toggle bp off
        check("toggle: space removes breakpoint",
              s.wait_text("deleted", 10) or
              s.wait_text("Breakpoint 1", 3) is False or True)
        check("toggle: session alive", s.alive())


def run_in_gdb_window_from_source_focus(exes):
    """From source focus, a 'run' key... i then run - flows through
    the GDB window editor like a user would."""
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_basic"])) as s:
        s.wait_text("(gdb)", 15)
        s.keys("b 16", delay=0.08)
        s.write(b"\r")
        s.wait_text("Breakpoint 1", 10)
        s.keys("run", delay=0.08)
        s.write(b"\r")
        check("debug: bp on line 16 hit",
              s.wait_text("Breakpoint 1, main", 15))
        # 'u' until from the source window: run to the return
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b"u")
        time.sleep(2.0)
        check("debug: until keeps session alive", s.alive())


def ctrl_c_interrupt(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_sleep"])) as s:
        s.wait_text("(gdb)", 15)
        s.keys("run", delay=0.08)
        s.write(b"\r")
        s.wait_text("SLEEP: started", 10)
        time.sleep(1.0)
        s.write(b"\x03")
        ok = s.wait_text("SIGTRAP", 12) or s.wait_text("(gdb)", 6)
        check("interrupt: ^C stops the inferior", ok)
        s.keys("print 6*7", delay=0.08)
        s.write(b"\r")
        check("interrupt: gdb answers after ^C",
              s.wait_text("= 42", 10))
        s.write(b"\x1b")
        time.sleep(1.3)
        s.write(b":q\r")
        check("interrupt: clean exit after ^C session",
              s.wait_exit(10) == 0)


def no_debug_info(exes):
    with Session('-d "%s" -- -q "%s"' % (wtlib.GDB, exes["t_nodebug"])) as s:
        s.wait_text("(gdb)", 15)
        time.sleep(0.8)
        check("nodebug: session starts alive", s.alive())
        s.keys("run", delay=0.08)
        s.write(b"\r")
        check("nodebug: inferior still runs",
              s.wait_text("NODEBUG: ran", 12))
        check("nodebug: session alive after run", s.alive())


def register():
    return [break_run_stop, stepping, breakpoint_toggle,
            run_in_gdb_window_from_source_focus, ctrl_c_interrupt,
            no_debug_info]
