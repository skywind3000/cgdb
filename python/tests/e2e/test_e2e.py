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
if sys.platform != "win32":
    # conpty.py touches ctypes.windll at import time; skip before that
    # so a full `pytest tests/` collects cleanly on POSIX too
    pytest.skip("ConPTY end-to-end tests are Windows only",
                allow_module_level=True)
from conpty import KEYS, ConPtySession          # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="ConPTY end-to-end tests are Windows only")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
CGDB_PY = os.path.join(ROOT, "python", "cgdb.py")
PY = sys.executable

ROWS, COLS = 32, 100

# the executing line's long arrow: `│` bar (same glyph as every other
# row's bar, just colored) + ASCII `---->` marker -- see app._source_text
EXEC_ARROW_RE = re.compile(r"│-+>")

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


BUSY_SRC = """#include <stdio.h>

int main(void) {
    volatile long n = 0;
    for (;;) {
        n++;
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def busy():
    """An inferior that never exits, for the ^C interrupt test."""
    if CC is None:
        pytest.skip("no C compiler")
    d = tempfile.mkdtemp(prefix="cgdbpy-e2e-busy-")
    src = os.path.join(d, "busy.c")
    exe = os.path.join(d, "busy.exe")
    with open(src, "w", encoding="utf-8") as f:
        f.write(BUSY_SRC)
    r = subprocess.run([CC, "-g", "-O0", src, "-o", exe],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip("could not compile busy: %s" % r.stderr[:300])
    yield {"dir": d, "src": src, "exe": exe}
    shutil.rmtree(d, ignore_errors=True)


class Cgdb:
    """One cgdb.py session in a ConPTY."""

    def __init__(self, target, extra=(), env=None, rows=ROWS, cols=COLS,
                 source=True):
        self.target = target
        self.extra = list(extra)
        self.env = env
        self.rows, self.cols = rows, cols
        self.source = source
        self.session = None

    def argv(self):
        argv = [PY, CGDB_PY, "-d", GDB]
        if self.source:
            argv += ["--source", self.target["src"]]
        return argv + ["--"] + self.extra + [self.target["exe"]]

    def __enter__(self):
        env = dict(self.env) if self.env else dict(os.environ)
        # isolate from the user's ~/.cgdb/cgdbrc: a machine with
        # `set wso=vertical` in it would silently flip every layout
        # assertion in this suite
        env.setdefault("CGDB_DIR",
                       tempfile.mkdtemp(prefix="cgdbpy-e2e-home-"))
        self.session = ConPtySession(self.argv(), rows=self.rows,
                                     cols=self.cols,
                                     cwd=ROOT, env=env).__enter__()
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
        """The source pane rows: (row, line) for numbered gutter rows.

        The gutter is ` lineno │ text` (lwidth grows with the file, the
        breakpoint marker is the line number's *colour*, invisible in a
        text dump); EOF rows show `~│`.
        """
        out = []
        for i in range(self.screen.rows):
            line = self.screen.line(i)
            if re.match(r"\s*\d+│", line):
                out.append((i, line))
        return out

    # the cursor bar's background (#2e4a62), and the executing-line
    # highlight's (#50fa7b) for when the selected line IS the stopped line
    BAR_BG = (46, 74, 98)
    EXEC_BG = (80, 250, 123)

    def cursor_line(self):
        """The selected source line, located by the cursor bar's colour.

        The hardware cursor is hidden while the source window has focus
        (and its position is not relayed anyway), so the bar painted in
        the selected line's own background is the locator: the first
        gutter row above the status bar carrying it. Falls back to the
        executing-line highlight when the selected line is also the
        stopped line -- the exec style wins the row there.
        """
        status = self.status_row()
        top = status if status is not None else self.screen.rows
        for bg in (self.BAR_BG, self.EXEC_BG):
            for r in range(top):
                if any(c == bg for c in self.screen.bg_grid[r]):
                    m = re.match(r"\s*(\d+)│", self.screen.line(r))
                    if m:
                        return int(m.group(1))
        return None

    def status_row(self):
        """Row index of the status bar -- the row between the two panes.

        cgdb draws the `:` / `/` command line into this same row
        (interface.cpp:459-472), so a test that cares *where* the user is
        typing needs the index, not just the text. C's status bar holds
        the current file's full path and nothing else (the `*` for GDB
        focus is dead code upstream, erased by if_display_message's
        repaint), so the locator is "the row that names the source file
        without a source-window gutter".
        """
        name = os.path.basename(self.target["src"])
        for i in range(self.screen.rows):
            line = self.screen.line(i)
            if name in line and "│" not in line:
                return i
        return None

    def status_line(self):
        i = self.status_row()
        return self.screen.line(i).strip() if i is not None else ""

    def to_gdb(self):
        """Ensure the GDB window has focus, whatever the current state.

        Idempotent on purpose: cgdb starts in the GDB window, so a test that
        blindly sent `i` would type the letter into the edit line.
        """
        if self.focus() != "GDB":
            self.send("i")
            assert self.wait_focus("GDB", timeout=10), self.status_line()
        self.settle(0.3)

    def to_source(self):
        """Ensure the source window has focus (what ESC gets a user)."""
        if self.focus() != "CGDB":
            self.key("esc")
            assert self.wait_focus("CGDB", timeout=10), self.status_line()
        self.settle(0.3)

    def focus(self):
        """Which window has focus: CGDB / GDB / CMD or ''.

        Two signals, in priority order:

        * The source pane's vertical bar is drawn BOLD only while that
          pane has focus (class:vline-focus). Every repaint carries it,
          resizes included, and it is the only reliable signal -- the
          hardware cursor is hidden in the source window (the cursor bar
          is the indicator) and ConPTY's relay of the hide is flaky.
        * Otherwise the visible cursor's position: on the edit prompt row
          the GDB window, on the status row the `:` command line.
        """
        status = self.status_row()
        if status is None:
            return ""
        # locate the bar column from any numbered gutter row
        vcol = None
        for r in range(status):
            m = re.match(r"\s*\d+│", self.screen.line(r))
            if m:
                vcol = m.end() - 1
                break
        if vcol is not None:
            # The vline-focus style bolds the WHOLE bar column; the
            # executing line's arrow style is bold too, and since the
            # arrow's bar is the same `│` glyph as the plain bar, a single
            # bold `│` row no longer proves source focus (that exact
            # confusion made to_source() skip its ESC and swallow F8/F6).
            # Require several bold bars: the executing line can only ever
            # contribute one.
            bold_bars = 0
            for r in range(status):
                if self.screen.grid[r][vcol] == "│" \
                        and self.screen.bold_grid[r][vcol]:
                    bold_bars += 1
                    if bold_bars >= 2:
                        return "CGDB"
        if not self.screen.cursor_visible:
            return ""          # hidden with no bold bar: intermediate
        r = self.screen.r
        line = self.screen.line(r)
        prompt = line.find("(gdb)")
        if prompt >= 0 and self.screen.c >= prompt:
            return "GDB"
        if r == status:
            return "CMD"
        # anything else -- the console in scroll mode -- reports unknown
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
    # an unknown flag is not cgdb's: getopt semantics hand it to gdb
    # (cgdb.cpp's parse_long_options stops at the first unknown). What
    # the CLI guarantees is a nonzero exit with an actionable error
    # rather than a raw traceback -- here the no-console guard fires in
    # a piped subprocess.
    r = subprocess.run([PY, CGDB_PY, "--nosuchflag"], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0
    assert "cgdb:" in (r.stderr + r.stdout)
    assert "Traceback" not in (r.stderr + r.stdout)


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
        # C's status bar carries the current file's full path and nothing
        # else; cgdb starts in the GDB window (interface.cpp:139) so
        # commands can be typed straight away, and the view sits at
        # main's line: gdb's answer to -file-list-exec-source-file
        # positioned it, as C does at startup.
        assert st.split("|")[0].endswith("target.c"), st
        assert "int main" in t, t
        assert c.focus() == "GDB", c.focus()
        assert c.session.parse_errors == []


@needs_gdb
def test_line_numbers_are_rendered(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.8)
        rows = c.gutter_lines()
        assert rows, "no gutter rows found:\n%s" % c.text()
        nums = [line.strip().split("│")[0].strip() for _, line in rows]
        assert nums[:4] == ["1", "2", "3", "4"], nums


@needs_gdb
def test_startup_opens_the_debuggees_main_source(target):
    """The debuggee's main file lands in the source window before anything
    has run: gdb's answer to -file-list-exec-source-file, which is what
    tgdb_request_current_location asks for in C.

    No --source flag here -- the point is that cgdb finds the file itself.
    """
    with Cgdb(target, ["-q"], source=False) as c:
        assert c.wait("(gdb)", timeout=30)
        assert c.wait("int compute", timeout=15), c.text()
        assert "target.c" in c.status_line(), c.status_line()
        # positioned at main, not merely opened
        assert "int main" in c.text(), c.text()
        # and a stop still works on top of it
        c.to_gdb()
        c.type_cmd("break main")
        assert c.wait("Breakpoint 1", timeout=15), c.text()
        c.type_cmd("run")
        c.settle(2.5)
        # the inferior's output goes to its own console window; what proves
        # the run completed is gdb answering commands again
        c.type_cmd("echo after-run\\n")
        assert c.wait("after-run", timeout=20), c.text()


@needs_gdb
def test_pythonhome_in_our_env_does_not_break_gdb(target):
    """A PYTHONHOME we inherit must not reach gdb.

    Vendor gdb builds embed their own CPython and resolve its stdlib from
    PYTHONHOME, so an unrelated value kills gdb during startup with
    "Python initialization failed: Failed to import encodings module" and every
    command after it. cgdb.py is itself a Python program, so its users are
    exactly the people who have PYTHONHOME set.
    """
    env = dict(os.environ)
    # the interpreter running cgdb.py accepts its own home here; gdb's
    # embedded CPython does not, and dies during startup -- exactly the
    # combination _child_env() strips. (Pointing one level higher would
    # kill cgdb.py's own interpreter before it could start.)
    env["PYTHONHOME"] = os.path.dirname(PY)
    with Cgdb(target, ["-q"], env=env) as c:
        assert c.wait("(gdb)", timeout=30), c.text()
        c.settle(0.8)
        t = c.text()
        assert "encodings module" not in t, t
        assert "Python not initialized" not in t, t
        # absence of the message is not enough -- prove gdb's embedded Python
        # actually runs
        c.to_gdb()
        c.type_cmd("python print(6 * 7)")
        assert c.wait("42", timeout=20), c.text()


# ---------------------------------------------------------------------------
# focus
# ---------------------------------------------------------------------------
@needs_gdb
def test_focus_switch_i_and_esc(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        assert c.focus() == "GDB", c.status_line()

        c.key("esc")
        # ESC needs ttimeoutlen (100 ms) to be released by prompt_toolkit's
        # VT parser; allow generously but assert it is not the old 500 ms bug
        assert c.wait_focus("CGDB", timeout=5), c.status_line()

        c.send("i")
        assert c.wait_focus("GDB", timeout=10), c.status_line()


# ---------------------------------------------------------------------------
# driving gdb
# ---------------------------------------------------------------------------
@needs_gdb
def test_gdb_command_is_echoed_and_answered(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_gdb()
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
        c.to_gdb()
        c.type_cmd("break main")
        assert c.wait("Breakpoint 1", timeout=20), c.text()

        c.type_cmd("run")
        assert c.wait("hit Breakpoint 1", timeout=40), c.text()
        c.settle(1.0)

        t = c.text()
        # the executing line is the long arrow (`│---->` -- see
        # _source_text); the breakpoint itself is the line number's
        # colour, which a text dump cannot see -- the marker itself is
        # asserted by the toggle test below
        assert EXEC_ARROW_RE.search(t), t


@needs_gdb
def test_next_advances_the_executing_line(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_gdb()
        c.type_cmd("break compute")
        assert c.wait("Breakpoint 1", timeout=20)
        c.type_cmd("run")
        assert c.wait("hit Breakpoint 1", timeout=40)
        c.settle(0.8)

        def arrow_row():
            for i in range(c.screen.rows):
                if EXEC_ARROW_RE.search(c.screen.line(i)):
                    return i, c.screen.line(i)
            return -1, ""

        r0, l0 = arrow_row()
        assert r0 >= 0, c.text()
        c.to_source()                    # F-keys only fire from source focus
        c.key("f8")                      # next
        c.settle(1.5)
        r1, l1 = arrow_row()
        assert r1 >= 0, c.text()
        assert l1 != l0, "F8 (next) did not move the executing line: %r" % l0


@needs_gdb
def test_inferior_runs_in_its_own_console(target):
    """run: the inferior's I/O belongs to its own console window (gdb
    "new-console on", the Visual Studio convention). Its output must not
    land on cgdb's screen, and the session must answer commands again
    after the program exits (MI reports the exit, not gdb's console
    channel -- there is no "[Inferior ... exited]" text to wait for)."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_gdb()
        c.type_cmd("run")
        c.settle(2.5)
        # needle is the computed value, not "MARKER result=" -- the source
        # window legitimately shows the printf("MARKER result=%d") literal
        assert "MARKER result=42" not in c.text(), (
            "inferior output leaked onto cgdb's screen (it belongs to the "
            "new-console popup): %r" % c.text()[:300])
        # gdb has the prompt back: a queued echo answers once the inferior
        # (which finished in its own window) is gone
        c.type_cmd("echo alive-after-run\\n")
        assert c.wait("alive-after-run", timeout=20), c.text()


@needs_gdb
def test_continue_after_break_runs_to_exit(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_gdb()
        c.type_cmd("break main")
        assert c.wait("Breakpoint 1", timeout=20)
        c.type_cmd("run")
        assert c.wait("hit Breakpoint 1", timeout=40)
        c.to_source()                    # F-keys only fire from source focus
        c.key("f6")                      # continue
        c.settle(2.5)
        # the inferior finishes in its own console window; cgdb never sees
        # the program's stdout, only gdb's own reports
        assert "MARKER result=42" not in c.text(), c.text()[:300]
        c.to_gdb()
        c.type_cmd("echo after-continue\\n")
        assert c.wait("after-continue", timeout=20), c.text()


# ---------------------------------------------------------------------------
# source window interaction
# ---------------------------------------------------------------------------
@needs_gdb
def test_space_toggles_a_breakpoint_marker(target):
    """space toggles the breakpoint at the cursor line.

    C's breakpoint marker is the line number's *colour* (red enabled,
    yellow disabled) -- there is no character, so a text dump cannot see
    it. The toggle itself is asserted through gdb's breakpoint table,
    and the cursor position through the gutter row under the cursor.
    """
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.8)
        c.to_source()
        assert c.cursor_line() is not None, c.text()
        start = c.cursor_line()
        c.send("jj")                     # move down two lines
        c.settle(0.6)
        here = c.cursor_line()
        assert here == start + 2, "jj moved %d -> %d" % (start, here)

        c.send(" ")                      # toggle breakpoint
        c.settle(2.5)
        c.to_gdb()
        c.type_cmd("info break")
        c.settle(1.2)
        t = c.text()
        # [\s\S] because the console wraps gdb's table across rows
        m = re.search(r"1\s+breakpoint\s+keep\s+y[\s\S]*?\.c:(\d+)", t)
        assert m, "no breakpoint in gdb's table:\n%s" % t
        marked = int(m.group(1))

        # walk the cursor onto the line gdb chose and toggle again: delete
        c.to_source()
        while c.cursor_line() is not None and c.cursor_line() < marked:
            c.send("j")
            c.settle(0.2)
        c.send(" ")
        c.settle(2.5)
        c.to_gdb()
        c.type_cmd("info break")
        assert c.wait("No breakpoints", timeout=15), c.text()


@needs_gdb
def test_slash_search_moves_the_cursor(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.8)
        c.to_source()
        # startup positioned the view at main (gdb's reported line)
        assert c.cursor_line() == 10, c.cursor_line()
        c.send("/printf")
        c.settle(0.4)
        c.key("enter")
        assert c.wait("printf", timeout=15)
        c.settle(1.0)
        # the cursor must have moved to the printf line, off main
        assert c.cursor_line() == 12, c.cursor_line()


@needs_gdb
def test_G_and_gg_navigate(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.8)
        c.to_source()
        c.send("G")
        c.settle(0.8)
        last = c.cursor_line()
        c.send("gg")
        c.settle(0.8)
        first = c.cursor_line()
        assert first == 1, first
        assert last == 14, last


# ---------------------------------------------------------------------------
# console scroll mode
# ---------------------------------------------------------------------------
@needs_gdb
def test_pageup_enters_console_scroll_mode(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_gdb()
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
        c.to_source()
        c.send(":set tabstop=4")
        c.settle(0.4)
        c.key("enter")
        c.settle(1.0)
        # the set reports the new value (a message replaces the whole
        # status row until the next key, so the path locator cannot find
        # the row -- assert on the full screen text instead); no error
        t = c.text()
        assert "unknown option" not in t, t
        assert ":set" not in t or "target.c" in t, t


@needs_gdb
def test_unknown_colon_command_reports_an_error(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        # `:` is cgdb's only in the source window; in the GDB window it is just
        # another character for gdb (interface.cpp:996-1000)
        c.to_source()
        c.send(":nosuchcommand")
        c.settle(0.4)
        c.key("enter")
        assert c.wait("Unknown command", timeout=15), c.text()


@needs_gdb
def test_command_line_lives_on_the_status_row(target):
    """`:` is typed on the middle status bar, and the bar is restored after.

    cgdb draws cur_sbc into status_win (interface.cpp:459-472) -- the row
    between the two panes, not the bottom edit line. Two separate things go
    wrong if this is missed: the prompt appears at the foot of the screen, and
    stale status text survives past the end of the shorter command (erasing it
    needs ECH, `CSI n X`, which the harness in conpty.py must implement).
    """
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        row = c.status_row()
        assert row is not None, c.text()
        bottom = c.screen.rows - 1

        c.to_source()
        c.send(":set tabstop=4")
        c.settle(0.6)
        assert c.screen.line(row).startswith(":set tabstop=4"), \
            repr(c.screen.line(row))
        assert "(gdb)" in c.screen.line(bottom), \
            "command line landed on the bottom row: %r" % c.screen.line(bottom)

        c.key("enter")
        c.settle(1.0)
        line = c.screen.line(row)
        assert not line.lstrip().startswith(":"), \
            "command text survived on the status bar: %r" % line
        # either the file path returned or the set's value message is up
        assert "target.c" in line or "4" in line, repr(line)


@needs_gdb
def test_ctrl_w_switches_to_vertical_split(target):
    """Ctrl-W (source focus) swaps the split to vertical.

    interface.cpp:17-31: source left, a one-column '|' separator, gdb right,
    and the status bar becomes the bottom row of the LEFT column. The `:`
    command line replaces that same row, so it moves with it.
    """
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.to_source()
        mid = c.screen.cols // 2          # left column is half the width

        c.send("\x17")                    # Ctrl-W
        c.settle(1.2)
        bars = sum(1 for r in range(c.screen.rows)
                   if c.screen.grid[r][mid] == "|")
        assert bars >= c.screen.rows - 4, \
            "no full-height separator column at %d:\n%s" % (mid, c.text())
        # source pane is truncated at the separator, gdb pane keeps its edit
        assert "int compute" in c.text()
        bottom = c.screen.rows - 1
        assert "target.c" in c.screen.line(bottom), c.screen.line(bottom)
        assert "(gdb)" in c.screen.line(bottom)[mid:], \
            c.screen.line(bottom)

        # the : command line replaces the status row, now bottom-left
        c.send(":")
        c.settle(0.6)
        line = c.screen.line(bottom)
        assert line.lstrip().startswith(":"), repr(line)
        assert "(gdb)" in line[mid:], repr(line)

        c.key("esc")
        c.settle(0.5)
        # `=` widens the left column by one; the separator moves with it
        c.send("=")
        c.settle(1.0)
        moved = sum(1 for r in range(c.screen.rows)
                    if c.screen.grid[r][mid + 1] == "|")
        assert moved >= c.screen.rows - 4, \
            "separator did not follow `=:\n%s" % c.text()

        # back to horizontal: separator gone, status bar back in the middle
        c.send("\x17")
        c.settle(1.2)
        bars = sum(1 for r in range(c.screen.rows)
                   if c.screen.grid[r][mid] == "|" or
                   c.screen.grid[r][mid + 1] == "|")
        assert bars < 5, c.text()
        assert c.status_row() is not None, c.text()
        assert "target.c" in c.status_line()


@needs_gdb
def test_terminal_resize_keeps_session_and_layout(target):
    """Resize the terminal mid-session, as the C win32 suite does
    (cases_windows: grow to 50x130, shrink back).

    The pane geometry is derived from the live terminal size, so the
    session must redraw and still accept commands after each resize.
    """
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)

        c.session.resize(50, 130)
        assert c.wait("int compute", timeout=15), c.text()
        c.to_gdb()
        c.type_cmd("echo after-grow\\n")
        assert c.wait("after-grow", timeout=15), c.text()

        c.session.resize(24, 80)
        assert c.wait("int compute", timeout=15), c.text()
        c.to_gdb()
        c.type_cmd("echo after-shrink\\n")
        assert c.wait("after-shrink", timeout=15), c.text()
        assert not c.exited(), "session died on resize:\n%s" % c.text()


@needs_gdb
def test_vertical_split_follows_resize(target):
    """The vertical separator is placed from the live width: after a
    resize it must move to the new half, and the session must survive."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.to_source()
        c.send("\x17")                    # Ctrl-W
        c.settle(1.2)
        mid = c.screen.cols // 2
        bars = sum(1 for r in range(c.screen.rows)
                   if c.screen.grid[r][mid] == "|")
        assert bars >= c.screen.rows - 4, c.text()

        c.session.resize(30, 60)
        mid2 = 60 // 2
        bars = sum(1 for r in range(c.screen.rows)
                   if c.screen.grid[r][mid2] == "|")
        assert bars >= c.screen.rows - 4, \
            "separator did not follow the resize:\n%s" % c.text()
        c.to_gdb()
        c.type_cmd("echo resized\\n")
        assert c.wait("resized", timeout=15), c.text()


# ---------------------------------------------------------------------------
# the `o` file dialog
# ---------------------------------------------------------------------------
@needs_gdb
def test_o_file_dialog_browses_and_opens(target):
    """`o` lists gdb's source files full-screen, /-search narrows to the
    target, Enter opens it back into the source pane."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.to_source()
        c.send("o")
        assert c.wait("Select a file or press q to cancel", timeout=15), \
            c.text()
        # gdb's list includes the CRT headers too; search for ours
        c.send("/")
        c.settle(0.3)
        c.send("target.c")
        c.settle(0.3)
        c.key("enter")
        c.settle(0.5)
        assert c.wait("target.c", timeout=10), c.text()
        c.key("enter")             # open the selection
        c.settle(1.2)
        assert "Select a file" not in c.text(), c.text()
        assert c.wait("int compute", timeout=15), c.text()


@needs_gdb
def test_o_file_dialog_esc_cancels(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.to_source()
        c.send("o")
        assert c.wait("Select a file or press q to cancel", timeout=15)
        c.key("esc")
        c.settle(1.0)
        assert "Select a file" not in c.text(), c.text()
        assert c.wait("int compute", timeout=15), c.text()   # pane intact


# ---------------------------------------------------------------------------
# file variants and robustness, ported from the C win32 matrix
# ---------------------------------------------------------------------------
@needs_gdb
def test_crlf_source_file_displays(target, tmp_path):
    src = tmp_path / "crlf.c"
    src.write_bytes(b"int crlf_fn(void) {\r\n    return 7;\r\n}\r\n")
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_source()
        c.send(":open " + str(src).replace("\\", "/"))
        c.settle(0.4)
        c.key("enter")
        assert c.wait("crlf_fn", timeout=15), c.text()
        assert c.session.parse_errors == []


@needs_gdb
def test_cjk_source_file_displays(target, tmp_path):
    src = tmp_path / "cjk.c"
    src.write_text("int cjk_fn(void) { return 1; }  /* 中文注释 */\n",
                   encoding="utf-8")
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_source()
        c.send(":open " + str(src).replace("\\", "/"))
        c.settle(0.4)
        c.key("enter")
        assert c.wait("中文注释", timeout=15), c.text()
        assert c.session.parse_errors == []


@needs_gdb
def test_ctrl_c_interrupts_the_inferior(busy):
    """run + ^C: gdb takes the break, the prompt answers again."""
    with Cgdb(busy, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_gdb()
        c.type_cmd("run")
        c.settle(1.0)
        c.key("ctrl-c")
        # the stop reason is not on the status bar any more (C never showed
        # it). gdb's own console report is the visible signal -- on Windows
        # the break lands as a SIGTRAP in the break thread, whose frame has
        # no source, so no executing-line arrow follows
        assert c.wait("received signal", timeout=15), c.text()
        c.settle(0.5)
        c.type_cmd("echo after-cc\\n")
        assert c.wait("after-cc", timeout=15), c.text()


@needs_gdb
def test_tiny_terminal_sizes_stay_alive(target):
    """12x40 and 6x20, the C matrix's floor sizes.

    `echo` with no trailing newline: the console pane may be a single row
    at these sizes, and anything that scrolls (the echoed command line, a
    trailing blank line) scrolls the assertion needle right off it.
    """
    for rows, cols in ((12, 40), (6, 20)):
        with Cgdb(target, ["-q"], rows=rows, cols=cols) as c:
            assert c.wait("(gdb)", timeout=30), (rows, cols)
            c.settle(0.8)
            c.to_gdb()
            c.type_cmd("echo tiny%d" % rows)
            assert c.wait("tiny%d" % rows, timeout=15), (rows, cols, c.text())


@needs_gdb
def test_rapid_key_bursts_keep_session_alive(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.to_source()
        c.send("jjjjkkkkGgg")
        c.key("esc")
        c.settle(0.4)
        c.send("i")
        c.settle(0.3)
        c.type_cmd("echo burst-ok\\n")
        assert c.wait("burst-ok", timeout=15), c.text()


@needs_gdb
def test_shell_command_round_trip(target):
    """:sh suspends the UI, runs the shell, and restores the session."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_source()
        c.send(":sh")
        c.settle(0.3)
        c.key("enter")
        # the subshell takes over the console; wait for cmd's banner, not
        # a bare '>' (that matches cgdb's own screen before cmd starts)
        assert c.wait("Microsoft", timeout=20), c.text()
        c.settle(1.0)
        c.send("echo from-shell\r")
        assert c.wait("from-shell", timeout=15), c.text()
        c.send("exit\r")
        # run_shell_command waits for one ENTER before handing the console
        # back (cgdb.cpp:185-219), so the last screen is not erased at once
        assert c.wait("Hit ENTER", timeout=15), c.text()
        c.key("enter")
        c.settle(2.0)
        assert c.wait("(gdb)", timeout=15), c.text()
        c.to_gdb()
        c.type_cmd("echo back-in-cgdb\\n")
        assert c.wait("back-in-cgdb", timeout=15), c.text()


@needs_gdb
def test_quit_exits_cleanly(target):
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("target.c", timeout=30)
        c.settle(0.8)
        c.to_source()
        c.send(":q")
        c.settle(0.3)
        c.key("enter")
        end = time.time() + 20
        while time.time() < end and not c.exited():
            time.sleep(0.2)
        assert c.exited(), "cgdb.py did not exit on :q"


@needs_gdb
def test_gdb_exit_takes_cgdb_down_with_it(target):
    """`exit` typed at gdb must end cgdb too.

    EOF on the MI channel makes tgdb_process() fail, which ends main_loop() and
    the program (cgdb.cpp:760-772). A cgdb that outlives its gdb leaves the
    user staring at a UI that can no longer do anything.
    """
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30), c.text()
        c.settle(0.5)
        c.to_gdb()
        c.type_cmd("exit")
        end = time.time() + 20
        while time.time() < end and not c.exited():
            time.sleep(0.2)
        assert c.exited(), "cgdb.py outlived gdb:\n%s" % c.text()


@needs_gdb
def test_quit_after_a_debug_session(target):
    """:q must work with a live inferior and a breakpoint set."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("(gdb)", timeout=30)
        c.settle(0.5)
        c.to_gdb()
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
        c.to_gdb()
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
        c.to_gdb()
        t0 = time.time()
        c.key("esc")
        ok = c.wait_focus("CGDB", timeout=10)
        dt = time.time() - t0
        assert ok, "ESC never switched focus back"
        assert dt < 0.45, (
            "ESC took %.3f s; prompt_toolkit's default ttimeoutlen (0.5 s) is "
            "probably not being overridden by cgdb's 100 ms setting" % dt)


@needs_gdb
def test_help_shows_the_builtin_cheatsheet(target):
    """:help swaps in a built-in key reference -- C opens the tgdb help
    file, which this port does not ship, so the reference is compiled in:
    modes and window switching, the F-keys, the common motions, and the
    file/breakpoint keys. q (or ESC) closes it."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.5)
        c.to_source()
        c.send(":help")
        c.settle(0.4)
        c.key("enter")
        assert c.wait("key reference", timeout=10), c.text()
        t = c.text()
        for needle in ("ESC", "F5 run", "F10 step", "h j k l",
                       "Ctrl-U/D", "gg / G", "space",
                       "open the file dialog"):
            assert needle in t, needle
        # the page replaces the whole screen while open
        assert "int compute" not in t
        c.send("q")
        c.settle(0.5)
        assert "key reference" not in c.text()
        assert c.wait("int compute", timeout=10), c.text()


@needs_gdb
def test_map_f4_runs_a_gdb_command_and_returns(target):
    """The classic mapping: `:map <f4> i<cmd><cr><esc>` must type the
    command into the GDB window, run it, and come back to the source
    window. The rhs has to survive its own first key ('i' switches the
    focus, which swaps the active map set), the text keys have to land
    in the edit line, and <cr>/<esc> close the loop."""
    with Cgdb(target, ["-q"]) as c:
        assert c.wait("int compute", timeout=30)
        c.settle(0.5)
        c.to_source()
        c.send(":map <f4> ibreak main<cr><esc>")
        c.settle(0.4)
        c.key("enter")
        c.settle(0.5)
        c.key("f4")
        assert c.wait("Breakpoint 1", timeout=15), c.text()
        c.settle(0.6)
        assert c.focus() == "CGDB", c.focus()
