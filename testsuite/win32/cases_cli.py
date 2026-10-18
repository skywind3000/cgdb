# -*- coding: utf-8 -*-
"""cases_cli.py - command-line/startup-failure paths (no debug session)."""

import os
import subprocess
import time

import wtlib
from wtlib import Session, check


def version(exes=None):
    r = subprocess.run([wtlib.CGDB, "--version"], capture_output=True,
                       timeout=20)
    out = r.stdout.decode("utf-8", "replace")
    check("cli: --version prints banner", r.returncode == 0 and
          out.startswith("CGDB "))
    check("cli: --version exit code 0", r.returncode == 0)


def help_opt(exes=None):
    r = subprocess.run([wtlib.CGDB, "--help"], capture_output=True,
                       timeout=20)
    out = r.stdout.decode("utf-8", "replace")
    check("cli: --help prints usage", r.returncode == 0 and
          "Usage" in out)


def bogus_debugger_path(exes=None):
    """-d pointing nowhere: must exit quickly with the reason on
    screen (regression for the silent crash, commit 2c8c8ca)."""
    with Session('-d "C:\\no\\such\\gdb.exe"') as s:
        ec = s.wait_exit(15)
        check("cli: bogus -d exits nonzero", ec not in (None, 0))
        check("cli: bogus -d no crash (not 0xC0000005)",
              ec != 3221225477, "exit=%s" % ec)
        # the message is printed during shutdown; the capture thread
        # may lag a moment behind the exit
        ok = False
        for _ in range(10):
            if b"not found" in s.raw() or b"failed" in s.raw():
                ok = True
                break
            time.sleep(0.3)
        check("cli: bogus -d explains itself", ok)


def gdb_not_in_path(exes=None):
    """Default 'gdb' with a PATH stripped of gdb: same contract."""
    saved = os.environ.get("PATH", "")
    stripped = ";".join(p for p in saved.split(";")
                        if "mingw32" not in p.lower())
    os.environ["PATH"] = stripped
    try:
        with Session("-- -q foo.exe") as s:
            ec = s.wait_exit(15)
            check("cli: no gdb on PATH exits nonzero", ec not in (None, 0))
            check("cli: no gdb on PATH no crash", ec != 3221225477,
                  "exit=%s" % ec)
            check("cli: no gdb on PATH shows hint",
                  b"not found" in s.raw())
    finally:
        os.environ["PATH"] = saved


def register():
    return [version, help_opt, bogus_debugger_path, gdb_not_in_path]
