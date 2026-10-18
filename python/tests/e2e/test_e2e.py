"""End-to-end tests: real cgdb.py + real gdb inside a real ConPTY.

These are the tests that caught every serious defect during development
(`event.key_press` not existing, the console pane padding its content out of
view, the command line swallowing ordinary characters, ESC taking 500 ms).
None of those are reachable from the unit tests, which drive CgdbApp directly
with a fake gdb.

Mirrors the scenario matrix the C port uses (doc/win_specs.md §10.2). Windows
only, and slow: each scenario spawns a fresh cgdb + gdb pair.

Run with:  pytest tests/e2e -q
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conpty import KEYS, ConPtySession          # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="ConPTY end-to-end tests are Windows only")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
CGDB_PY = os.path.join(ROOT, "python", "cgdb.py")
PY = sys.executable

ROWS, COLS = 32, 100

TARGET_SRC = """#include <stdio.h>

int compute(int n) {
    int s = 0;
    s += n;
    s *= 2;
    return s;
}

int main(void) {
    int r = compute(21);
    printf("MARKER result=%d\\n", r);
    return 0;
}
"""


def find_gdb():
    env = os.environ.get("CGDB_GDB")
    if env and os.path.exists(env):
        return env
    for cand in (shutil.which("gdb"), r"D:\Dev\mingw32\bin\gdb.exe"):
        if cand and os.path.exists(cand):
            return cand
    return None


def find_cc():
    for cand in (shutil.which("gcc"), r"D:\Dev\mingw32\bin\gcc.exe"):
        if cand and os.path.exists(cand):
            return cand
    return None


GDB = find_gdb()
CC = find_cc()

needs_gdb = pytest.mark.skipif(GDB is None, reason="gdb not available")
needs_cc = pytest.mark.skipif(CC is None, reason="gcc not available")


@pytest.fixture(scope="module")
def target():
    if CC is None:
        pytest.skip("no C compiler")
    d = tempfile.mkdtemp(prefix="cgdbpy-e2e-")
    src = os.path.join(d, "target.c")
    exe = os.path.join(d, "target.exe")
    with open(src, "w", encoding="utf-8") as f:
        f.write(TARGET_SRC)
    r = subprocess.run([CC, "-g", "-O0", src, "-o", exe],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip("could not compile target: %s" % r.stderr[:300])
    yield {"dir": d, "src": src, "exe": exe}
    shutil.rmtree(d, ignore_errors=True)


class Cgdb:
    """One cgdb.py session in a ConPTY."""

    def __init__(self, target, extra=()):
        self.target = target
        self.extra = list(extra)
        self.session = None

    def argv(self):
        return [PY, CGDB_PY, "-d", GDB, "--source", self.target["src"],
                "--"] + self.extra + [self.target["exe"]]

    def __enter__(self):
        self.session = ConPtySession(self.argv(), rows=ROWS, cols=COLS,
                                     cwd=ROOT).__enter__()
        return self

    def __exit__(self, *a):
        try:
            return self.session.__exit__(*a)
        finally:
            self.session = None

    # --- helpers ----------------------------------------------------------
    @property
    def screen(self):
        return self.session.screen

    def text(self):
        return self.session.snapshot()

    def send(self, s):
        self.session.send(s)

    def key(self, name):
        self.session.send(KEYS[name])

    def type_cmd(self, text):
        """Type into the GDB window and press Enter."""
        self.send(text)
        self.key("enter")

    def wait(self, needle, timeout=25.0):
        return self.session.wait_for(needle, timeout=timeout)

    def settle(self, s=0.8):
        self.session.settle(s)

    def exited(self):
        return self.session.exited()

    def gutter_lines(self):
        """The source pane rows, as (marker, lineno, exec, text)."""
        out = []
        for i in range(self.screen.rows):
            line = self.screen.line(i)
            if not line.strip():
                continue
            if len(line) > 2 and line[0] in "B " and line[1:5].strip().isdigit():
                out.append((i, line))
        return out

    def status_line(self):
        for i in range(self.screen.rows):
            line = self.screen.line(i)
            if "target.c" in line and ("CGDB" in line or "GDB" in line):
                return line.strip()
        return ""

    def focus(self):
        """The focus token from the status line: CGDB / GDB / CMD or ''.

        Parsed rather than substring-matched, because "CGDB  target.c"
        contains "GDB  target.c" -- a naive wait_for() on the latter passes
        before the focus has actually changed.
        """
        st = self.status_line()
        for name in ("CGDB", "GDB", "CMD"):
            if st.startswith(name + " "):
                return name
        return ""

    def wait_focus(self, name, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.focus() == name:
                return True
            time.sleep(0.05)
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_version():
    r = subprocess.run([PY, CGDB_PY, "--version"], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0
    assert "CGDB (Python)" in r.stdout


def test_cli_help():
    r = subprocess.run([PY, CGDB_PY, "--help"], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0
    for needle in ("--version", "-d", "ESC", "F5"):
        assert needle in r.stdout, needle


def test_cli_unknown_option():
    r = subprocess.run([PY, CGDB_PY, "--nosuchflag"], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0
    assert "unknown option" in (r.stderr + r.stdout)


def test_cli_missing_debugger_is_visible_not_silent(tmp_path):
    """The C port had a silent crash here (commit "Fix silent crash when gdb
    is not on PATH; surface spawn errors"). The Python port must fail loudly."""
    bogus = str(tmp_path / "no-such-gdb.exe")
    r = subprocess.run([PY, CGDB_PY, "-d", bogus], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0
    assert "cannot find debugger" in r.stderr
    # the message formats the path with %r, so backslashes are doubled;
    # assert on the file name rather than the exact path
    assert "no-such-gdb.exe" in r.stderr


# ---------------------------------------------------------------------------
# startup and layout
# ---------------------------------------------------------------------------
@needs_gdb
def test_startup_shows_source_status_and_prompt(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30), c.text()
        c.settle(1.0)
        t = c.text()
        assert "#include <stdio.h>" in t
        assert "int compute(int n)" in t
        assert "(gdb)" in t
        st = c.status_line()
        assert st.startswith("CGDB"), st
        assert "1/" in st
        assert c.session.parse_errors == []


@needs_gdb
def test_line_numbers_are_rendered(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.8)
        rows = c.gutter_lines()
        assert rows, "no gutter rows found:\n%s" % c.text()
        nums = [line[1:5].strip() for _, line in rows]
        assert nums[:4] == ["1", "2", "3", "4"], nums


# ---------------------------------------------------------------------------
# focus
# ---------------------------------------------------------------------------
@needs_gdb
def test_focus_switch_i_and_esc(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        assert c.focus() == "CGDB", c.status_line()

        c.send("i")
        assert c.wait_focus("GDB", timeout=10), c.status_line()

        c.key("esc")
        # ESC needs ttimeoutlen (100 ms) to be released by prompt_toolkit's
        # VT parser; allow generously but assert it is not the old 500 ms bug
        assert c.wait_focus("CGDB", timeout=5), c.status_line()


# ---------------------------------------------------------------------------
# driving gdb
# ---------------------------------------------------------------------------
@needs_gdb
def test_gdb_command_is_echoed_and_answered(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.send("i")
        c.settle(0.4)
        c.type_cmd("show language")
        assert c.wait("show language", timeout=15), c.text()
        # gdb 17.1 wording: `The current source language is "auto; currently c".`
        # Older gdb said "Currently the language is", so accept either.
        assert (c.wait("current source language", timeout=15)
                or c.wait("Currently the language", timeout=5)), c.text()


@needs_gdb
def test_break_run_stop_marks_the_source(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.send("i")
        c.settle(0.4)
        c.type_cmd("break main")
        assert c.wait("Breakpoint 1", timeout=20), c.text()

        c.type_cmd("run")
        assert c.wait("hit Breakpoint 1", timeout=40), c.text()
        c.settle(1.0)

        t = c.text()
        # the gutter must show both the breakpoint and the executing line
        assert "=>" in t, t
        assert "B" in t, t
        assert "breakpoint-hit" in c.status_line()


@needs_gdb
def test_next_advances_the_executing_line(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.send("i")
        c.settle(0.4)
        c.type_cmd("break compute")
        assert c.wait("Breakpoint 1", timeout=20)
        c.type_cmd("run")
        assert c.wait("hit Breakpoint 1", timeout=40)
        c.settle(0.8)

        def arrow_row():
            for i in range(c.screen.rows):
                if "=>" in c.screen.line(i):
                    return i, c.screen.line(i)
            return -1, ""

        r0, l0 = arrow_row()
        assert r0 >= 0, c.text()
        c.key("f8")                      # next
        c.settle(1.5)
        r1, l1 = arrow_row()
        assert r1 >= 0, c.text()
        assert l1 != l0, "F8 (next) did not move the executing line: %r" % l0


@needs_gdb
def test_inferior_output_is_shown(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.send("i")
        c.settle(0.4)
        c.type_cmd("run")
        assert c.wait("MARKER result=42", timeout=40), c.text()


@needs_gdb
def test_continue_after_break_runs_to_exit(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.send("i")
        c.settle(0.4)
        c.type_cmd("break main")
        assert c.wait("Breakpoint 1", timeout=20)
        c.type_cmd("run")
        assert c.wait("hit Breakpoint 1", timeout=40)
        c.key("f6")                      # continue
        assert c.wait("MARKER result=42", timeout=40), c.text()


# ---------------------------------------------------------------------------
# source window interaction
# ---------------------------------------------------------------------------
@needs_gdb
def test_space_toggles_a_breakpoint_marker(target):
    def marked_rows(c):
        """Source-pane rows whose gutter column 0 carries a 'B'."""
        out = []
        for i in range(c.screen.rows):
            line = c.screen.line(i)
            if line[:1] == "B" and line[1:6].strip().isdigit():
                out.append((i, line))
        return out

    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.8)
        assert marked_rows(c) == [], "breakpoints present before any were set"

        def cursor_line():
            m = re.search(r"\s(\d+)/(\d+)\s", c.status_line())
            assert m, "cannot read the cursor from %r" % c.status_line()
            return int(m.group(1))

        start = cursor_line()
        c.send("jj")                     # move down two lines
        c.settle(0.6)
        # read the cursor back off the status line rather than assuming a
        # starting position: the property under test is "the marker lands where
        # the cursor is"
        here = cursor_line()
        assert here == start + 2, "jj moved %d -> %d, expected +2" % (start,
                                                                      here)

        c.send(" ")                      # toggle breakpoint
        c.settle(2.0)
        rows = marked_rows(c)
        assert rows, "space did not put a B in the gutter:\n%s" % c.text()
        # The marker shows the line gdb actually used, which is not necessarily
        # the cursor line: gdb slides a breakpoint set on a function's opening
        # brace down to the first executable statement. Asserting equality with
        # the cursor line would be asserting a bug.
        marked = int(rows[0][1][1:6].strip())
        assert marked >= here, (
            "gdb moved the breakpoint backwards: cursor %d, marker %d"
            % (here, marked))
        assert marked <= here + 3, (
            "marker too far from the cursor: %d vs %d" % (marked, here))

        # toggling again from the marked line removes it
        for _ in range(marked - here):
            c.send("j")
            c.settle(0.15)
        c.settle(0.4)
        assert cursor_line() == marked, (
            "could not move the cursor onto the marked line %d" % marked)
        c.send(" ")
        c.settle(2.0)
        assert marked_rows(c) == [], (
            "second space did not clear the marker:\n%s" % c.text())


@needs_gdb
def test_slash_search_moves_the_cursor(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.8)
        assert "1/" in c.status_line()
        c.send("/main")
        c.settle(0.4)
        c.key("enter")
        assert c.wait("int main(void)", timeout=15)
        c.settle(1.0)
        st = c.status_line()
        # the cursor must have moved off line 1
        assert " 1/" not in st, st


@needs_gdb
def test_G_and_gg_navigate(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.8)
        c.send("G")
        c.settle(0.8)
        st_last = c.status_line()
        c.send("gg")
        c.settle(0.8)
        st_first = c.status_line()
        assert " 1/" in st_first, st_first
        assert st_last != st_first


# ---------------------------------------------------------------------------
# console scroll mode
# ---------------------------------------------------------------------------
@needs_gdb
def test_pageup_enters_console_scroll_mode(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.send("i")
        c.settle(0.4)
        # make some scrollback
        for _ in range(6):
            c.type_cmd("echo line\\n")
            c.settle(0.3)
        c.key("pageup")
        c.settle(1.0)
        t = c.text()
        assert "[" in t and "/" in t, "no scroll indicator: %r" % t


# ---------------------------------------------------------------------------
# :set and :q
# ---------------------------------------------------------------------------
@needs_gdb
def test_set_command_reports_on_the_status_line(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.send(":set tabstop=4")
        c.settle(0.4)
        c.key("enter")
        c.settle(1.0)
        # back in source focus, and no error message on the status line
        st = c.status_line()
        assert "unknown option" not in st, st
        assert st.startswith("CGDB"), st


@needs_gdb
def test_unknown_colon_command_reports_an_error(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.send(":nosuchcommand")
        c.settle(0.4)
        c.key("enter")
        assert c.wait("unknown command", timeout=15), c.text()


@needs_gdb
def test_quit_exits_cleanly(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.send(":q")
        c.settle(0.3)
        c.key("enter")
        end = time.time() + 20
        while time.time() < end and not c.exited():
            time.sleep(0.2)
        assert c.exited(), "cgdb.py did not exit on :q"


@needs_gdb
def test_quit_after_a_debug_session(target):
    """:q must work with a live inferior and a breakpoint set."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.send("i")
        c.settle(0.4)
        c.type_cmd("break main")
        assert c.wait("Breakpoint 1", timeout=20)
        c.type_cmd("run")
        assert c.wait("hit Breakpoint 1", timeout=40)
        c.key("esc")
        c.settle(0.6)
        c.send(":q")
        c.settle(0.3)
        c.key("enter")
        end = time.time() + 25
        while time.time() < end and not c.exited():
            time.sleep(0.2)
        assert c.exited(), "cgdb.py hung on :q during a live session"


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------
@needs_gdb
def test_no_vt_parse_errors_during_a_session(target):
    """The screen model must never see a sequence it cannot handle, or the
    assertions above would be reading a corrupted screen."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.send("i")
        c.settle(0.3)
        c.type_cmd("break main")
        c.wait("Breakpoint 1", timeout=20)
        c.type_cmd("run")
        c.wait("hit Breakpoint 1", timeout=40)
        c.key("esc")
        c.settle(0.5)
        c.send("jjkkGGgg")
        c.settle(1.0)
        assert c.session.parse_errors == [], c.session.parse_errors[:5]


@needs_gdb
def test_key_latency_is_not_the_500ms_escape_bug(target):
    """Regression guard for the ttimeoutlen wiring.

    prompt_toolkit's VT parser holds a lone ESC until Application.ttimeoutlen
    expires. Its default of 0.5 s made ESC -- cgdb's focus-switch key -- feel
    broken. cgdb's own ttimeoutlen default is 100 ms.
    """
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(1.0)
        c.send("i")
        assert c.wait_focus("GDB", timeout=10)
        t0 = time.time()
        c.key("esc")
        ok = c.wait_focus("CGDB", timeout=10)
        dt = time.time() - t0
        assert ok, "ESC never switched focus back"
        assert dt < 0.45, (
            "ESC took %.3f s; prompt_toolkit's default ttimeoutlen (0.5 s) is "
            "probably not being overridden by cgdb's 100 ms setting" % dt)
