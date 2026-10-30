"""End-to-end tests on a real pty: the inferior's embedded terminal.

The acceptance bar for doc/python_specs.md §21 is "the /usr/bin/cgdb 0.8.0
probe's assertions pass on the Python port too": the inferior's colour,
clear and absolute positioning render inside the GDB window, typed input
travels byte-wise with exactly the line discipline's echo, a terminal
resize reaches the inferior as SIGWINCH, ^C stops it, and its screen
content lands in the console history ABOVE gdb's stop report -- the same
visual order C's one-vterm scroller gives.

POSIX only (the Windows counterpart is the ConPTY suite). Slow: each test
spawns a fresh cgdb.py + gdb + inferior inside its own pty.

Run with:  pytest tests/e2e/test_e2e_posix.py -q
"""

import os
import sys
import time

import pytest

pytest.importorskip("pyte")
if sys.platform == "win32":
    # posix_harness imports fcntl at module time; skip before that so a
    # full `pytest tests/` collects cleanly on Windows too
    pytest.skip("pty end-to-end tests are POSIX only",
                allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from posix_harness import PtyDriver, compile_probe   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
CGDB_PY = os.path.join(ROOT, "python", "cgdb.py")
PY = sys.executable

ROWS, COLS = 40, 100


@pytest.fixture(scope="module")
def probe_exe():
    return compile_probe()


@pytest.fixture
def app_pty(probe_exe):
    """A real cgdb.py session in its own pty, torn down hard on exit."""
    d = PtyDriver([PY, CGDB_PY, "--", "-q", probe_exe],
                  rows=ROWS, cols=COLS)
    try:
        d.pump(3.0)               # boot: MI mode has no prompt to wait for
        yield d
    finally:
        d.terminate()


def test_inferior_terminal_renders_inside_the_gdb_window(app_pty):
    d = app_pty
    d.write("run\r")
    assert d.wait_for("TYPE>", 20)
    d.pump(0.5)

    # \033[2J cleared the GDB pane: the pre-clear markers are gone from
    # the screen, exactly as they were under /usr/bin/cgdb
    screen = d.screen_text()
    assert "normal" not in screen
    assert screen.count("SCREEN-MODE") == 1
    # and it sits where the inferior put it: column 10 (1-based)
    hit = d.find("SCREEN-MODE")
    assert hit is not None
    y, x = hit
    assert x == 9
    # the colour survives the app's rendering into the real terminal
    ch = d.char_at(y, x)
    assert ch.fg in ("red", "800000")

    # the hardware cursor rides the inferior's terminal cursor: it lands
    # in the GDB pane at the VT's cursor -- never stranded at the screen
    # corner (ptk maps cursors through rendered cells only, so the
    # cursor's row is padded for it). Exactly where depends on whether
    # gdb's thread-db records raced ahead of the inferior's output (two
    # channels; the C version has one), so accept the VT's own position
    # and require the ESC -> i roundtrip to return to it.
    deadline = time.time() + 5
    cur = (0, 0)
    while time.time() < deadline:
        d.pump(0.1)
        hit = d.find("TYPE>")
        cur = (d._screen.cursor.y, d._screen.cursor.x)
        if hit and cur[0] >= 20 and cur != (0, 0):
            break
    assert hit is not None
    assert cur[0] >= 20 and cur != (0, 0)
    d.write("\x1b")
    d.pump(0.5)
    d.write("i")
    d.pump(0.5)
    assert (d._screen.cursor.y, d._screen.cursor.x) == cur

    # typed input: byte-wise passthrough, echoed exactly once by the pty
    # line discipline (never by hand -- that was the old double-echo bug)
    d.write("hello\r")
    assert d.wait_for("GOT:hello", 10)
    d.pump(0.5)
    screen = d.screen_text()
    assert screen.count("hello") == 2       # echo + the program's own line


def test_resize_interrupt_and_flatten_order(app_pty):
    d = app_pty
    d.write("run\r")
    assert d.wait_for("TYPE>", 20)
    d.write("hello\r")
    assert d.wait_for("GOT:hello", 10)

    # the resize chain: harness pty -> cgdbpy layout -> TIOCSWINSZ on the
    # inferior's pty -> SIGWINCH to the foreground process group
    d.set_size(30, 90)
    assert d.wait_for("WINCH-SEEN", 5)

    # ^C: on_key intercepts it -> interrupt() -> the inferior stops
    d.write("\x03")
    assert d.wait_for("SIGINT", 10)
    d.pump(1.5)

    # the inferior's screen was flattened into the console history and
    # sits ABOVE gdb's stop report -- C's one-vterm visual order (this is
    # what the two-channel drain gate in _on_stopped buys)
    screen = d.screen_text()
    assert "PAUSING" in screen
    assert "Program received signal" in screen
    assert screen.find("PAUSING") < screen.find("Program received signal")

    # and the session still responds: :q exits cleanly
    d.write("\x1b")
    d.pump(0.4)
    d.write(":q\r")
    end = time.time() + 15
    while time.time() < end and not d.exited():
        d.pump(0.2)
    assert d.exited(), "cgdb.py did not exit on :q"
    assert os.WIFEXITED(d.exit_status())
    assert os.WEXITSTATUS(d.exit_status()) == 0
