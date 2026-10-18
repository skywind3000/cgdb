# -*- coding: utf-8 -*-
"""run_tests.py - entry point for the cgdb Windows test suite.

Runs the real cgdb.exe under ConPTY sessions and covers: CLI paths,
startup display, focus switching, the status bar (including the ':'
idle crash regression), the debug loop (breakpoints/run/step/^C),
source navigation and search, GDB window line editing and history,
window splits and resizes, file variants (CRLF/CJK), ':' commands and
every quit path.

Usage (Git Bash, from the repository root):

    export PATH="/d/Dev/mingw32/bin:$PATH"   # for gcc (test inferiors)
    python testsuite/win32/run_tests.py

Exit code 0 = all checks passed.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wtlib
import cases_cli
import cases_session
import cases_debug
import cases_nav
import cases_windows
import cases_files
import cases_commands

MODULES = [cases_cli, cases_session, cases_debug, cases_nav,
           cases_windows, cases_files, cases_commands]


def main():
    print("cgdb Windows test suite")
    print("cgdb: %s" % wtlib.CGDB)
    print("gdb:  %s" % wtlib.GDB)
    print()

    if not wtlib.prerequisites():
        return 2

    t0 = time.time()
    exes = wtlib.compile_inferiors()
    if exes is None:
        print("ERROR: could not compile the test inferiors "
              "(is gcc from mingw32 on PATH?)")
        return 2

    for mod in MODULES:
        print("== %s ==" % mod.__name__.split(".")[-1])
        for case in mod.register():
            try:
                case(exes)
            except Exception as ex:
                import traceback
                wtlib.check("%s crashed the harness" %
                            case.__name__, False, repr(ex)[:120])
                traceback.print_exc()
        print()

    rc = wtlib.summary()
    print("total time: %.1fs" % (time.time() - t0))
    return rc


if __name__ == "__main__":
    sys.exit(main())
