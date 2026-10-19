#!/usr/bin/env python3
"""cgdb.py -- Python implementation of CGDB, the console front end to GDB.

Usage matches the C version:

    cgdb.py [cgdb options] [gdb options]

CGDB options:
    --version        print version information and exit
    --help           print this message and exit
    -d DEBUGGER      use DEBUGGER instead of `gdb` from PATH
    -w               wait for a debugger to attach before starting
    --no-color       force monochrome output
    --source FILE    open FILE in the source pane at startup

cgdb's options are parsed from the front only: the first argument that
is not a cgdb option ends the scan, and everything from there on is
passed straight to gdb. So `cgdb.py my_program`, `cgdb.py -q
my_program` and `cgdb.py -- -q my_program` all work, exactly like the
original cgdb.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VERSION = "0.1.0"

USAGE = """CGDB (Python) Usage:
   cgdb.py [cgdb options] [gdb options]

CGDB Options:
   --version      Print version information and then exit.
   --help         Print help (this message) and then exit.
   -d DEBUGGER    Set debugger to use.
   -w             Wait for debugger to attach before starting.
   --no-color     Disable syntax highlighting and colours.
   --source FILE  Open FILE in the source pane at startup.
   --             Marks the end of CGDB's options.

Everything from the first argument that is not a CGDB option onwards is
passed to gdb verbatim, so `cgdb.py my_program` and `cgdb.py -q
my_program` work like in cgdb itself.

Keys:
   ESC            switch to the source window
   i              switch to the GDB window
   F5 / F6        run / continue
   F8 / F10       next / step
   F7             finish
   space          toggle breakpoint at the cursor line
   t              temporary breakpoint
   /  ?  n  N     search forward / backward / repeat
   :              command line (:set, :open, :map, :q)
   +  -           grow / shrink the source pane
   Ctrl-L         redraw
   Ctrl-C         interrupt the inferior
"""


def parse_args(argv):
    """Parse cgdb's own options, mirroring parse_long_options (cgdb.cpp).

    getopt-style semantics: cgdb's options are parsed from the front and
    the FIRST argument that is not one of them ends the scan -- from
    there on everything belongs to gdb verbatim (an unknown `-x` option
    included, which is why `cgdb -q my_program` works in the original).
    A leading `--` in that remainder is stripped, as the C version does.
    The one deviation: GNU getopt reorders a trailing `prog -w` to pick
    up -w after a non-option; this scan does not permute.
    """
    opts = {"gdb": None, "wait": False, "color": None, "source": None,
            "gdb_args": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            opts["gdb_args"] = list(argv[i + 1:])
            break
        if a == "--version":
            print("CGDB (Python) %s" % VERSION)
            raise SystemExit(0)
        if a == "--help":
            print(USAGE)
            raise SystemExit(0)
        if a == "-d":
            i += 1
            if i >= len(argv):
                sys.stderr.write("cgdb: -d requires an argument\n")
                raise SystemExit(1)
            opts["gdb"] = argv[i]
        elif a.startswith("-d") and len(a) > 2:
            opts["gdb"] = a[2:]
        elif a == "-w":
            opts["wait"] = True
        elif a == "--no-color":
            opts["color"] = False
        elif a == "--source":
            i += 1
            if i >= len(argv):
                sys.stderr.write("cgdb: --source requires an argument\n")
                raise SystemExit(1)
            opts["source"] = argv[i]
        elif a.startswith("--source="):
            opts["source"] = a.split("=", 1)[1]
        else:
            # not one of cgdb's options: the original hands the rest to
            # gdb (getopt stops at the first non-option / unknown option)
            opts["gdb_args"] = list(argv[i:])
            break
        i += 1
    return opts


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    opts = parse_args(argv)

    if opts["gdb"] and not _executable_exists(opts["gdb"]):
        # Fail fast and visibly: the C port had a bug where a missing gdb
        # produced a silent crash (commit "Fix silent crash when gdb is not
        # on PATH; surface spawn errors"). Do not repeat it.
        sys.stderr.write(
            "cgdb: cannot find debugger %r\n"
            "Is it installed and on PATH? Use -d <path> to point at it.\n"
            % opts["gdb"])
        return 1

    try:
        from cgdbpy.app import CgdbApp
        from cgdbpy.cgdbrc import Config
    except ImportError as e:
        sys.stderr.write("cgdb: missing dependency: %s\n" % e)
        sys.stderr.write("Install with: pip install prompt_toolkit pygments\n")
        return 1

    config = Config()
    if opts["color"] is False:
        config.set("color", 0)
    _load_rc(config)

    app = CgdbApp(gdb_path=opts["gdb"], gdb_args=opts["gdb_args"],
                  config=config, source_path=opts["source"])
    if opts["wait"]:
        _wait_for_debugger()
    return app.run()


def _executable_exists(path):
    # dirname() rather than `os.path.sep in path`: on Windows a debugger may be
    # given as `D:\Dev\mingw32\bin\gdb.exe`, `D:/Dev/mingw32/bin/gdb.exe` or
    # the drive-relative `C:gdb.exe`, and only the first contains os.path.sep.
    # Any of them is a path, not a bare name to look up on PATH.
    if os.path.dirname(path):
        return os.path.exists(path)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, path)
        if os.path.exists(cand):
            return True
        if os.name == "nt":
            for ext in os.environ.get("PATHEXT", ".EXE").split(os.pathsep):
                if os.path.exists(cand + ext):
                    return True
    return False


def _load_rc(config):
    """Load ~/.cgdbrc then ./.cgdbrc, mirroring cgdb's rc lookup."""
    candidates = []
    home = os.path.expanduser("~")
    if home:
        candidates.append(os.path.join(home, ".cgdbrc"))
    candidates.append(os.path.join(os.getcwd(), ".cgdbrc"))
    for path in candidates:
        if os.path.exists(path):
            try:
                errors = config.load_file(path)
            except OSError:
                continue
            for e in errors:
                sys.stderr.write("cgdb: %s\n" % e)


def _wait_for_debugger():
    """`-w`: block until a debugger attaches.

    The C version checks TracerPid in /proc/self/status. There is no
    equivalent on Windows, so poll for a debugger via the OS API and fall back
    to a prompt on platforms where that is unavailable.
    """
    if sys.platform == "win32":
        import ctypes
        import time
        sys.stderr.write("cgdb: waiting for a debugger to attach (pid %d)...\n"
                         % os.getpid())
        while not ctypes.windll.kernel32.IsDebuggerPresent():
            time.sleep(0.2)
        return
    status = "/proc/self/status"
    if not os.path.exists(status):
        return
    import time
    sys.stderr.write("cgdb: waiting for a debugger to attach (pid %d)...\n"
                     % os.getpid())
    while True:
        try:
            with open(status) as f:
                for line in f:
                    if line.startswith("TracerPid:"):
                        if line.split(":")[1].strip() != "0":
                            return
                        break
        except OSError:
            return
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())
