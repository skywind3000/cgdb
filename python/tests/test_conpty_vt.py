"""Unit tests for the e2e ConPTY screen model (tests/e2e/conpty.py).

The model grew two channels beyond plain text, both load-bearing for the
suite: the hardware cursor's *visibility* (cgdb hides it in the source
window, so visibility is the focus signal) and the per-cell *background*
(the cursor bar's colour locates the selected line).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "e2e"))

if sys.platform != "win32":
    # conpty.py touches ctypes.windll at import time; skip before that
    # so a full `pytest tests/` collects cleanly on POSIX too
    import pytest
    pytest.skip("ConPTY screen model is Windows only",
                allow_module_level=True)

from conpty import VTScreen


def test_cursor_visibility_sequences():
    s = VTScreen(10, 40)
    assert s.cursor_visible
    s.feed(b"\x1b[?25h")
    assert s.cursor_visible
    s.feed(b"\x1b[25l")          # ConPTY relays the hide without the '?'
    assert not s.cursor_visible
    s.feed(b"\x1b[?25l")
    assert not s.cursor_visible
    s.feed(b"\x1b[?25h")
    assert s.cursor_visible


def test_blink_stop_sequence_is_ignored():
    # ptk's show is "\x1b[?12l\x1b[?25h": the 12 must not clobber anything
    s = VTScreen(10, 40)
    s.feed(b"\x1b[25l")
    s.feed(b"\x1b[?12l\x1b[?25h")
    assert s.cursor_visible


def test_truecolor_background_tracking():
    s = VTScreen(10, 40)
    s.feed(b"\x1b[48;2;46;74;98mab\x1b[mcd")
    assert s.bg_grid[0][0] == (46, 74, 98)
    assert s.bg_grid[0][1] == (46, 74, 98)
    assert s.bg_grid[0][2] is None
    assert s.bg_grid[0][3] is None


def test_sgr_reset_and_default_clear_the_background():
    s = VTScreen(10, 40)
    s.feed(b"\x1b[48;2;1;2;3mA\x1b[mB\x1b[48;2;9;9;9mC\x1b[49mD")
    assert s.bg_grid[0][0] == (1, 2, 3)
    assert s.bg_grid[0][1] is None
    assert s.bg_grid[0][2] == (9, 9, 9)
    assert s.bg_grid[0][3] is None


def test_el_fills_with_the_current_background():
    # ptk paints the cursor bar's tail via EL in the bar colour
    s = VTScreen(10, 40)
    s.feed(b"\x1b[48;2;46;74;98mX\x1b[K")
    for c in range(40):
        assert s.bg_grid[0][c] == (46, 74, 98), c


def test_bold_tracking():
    s = VTScreen(10, 40)
    s.feed(b"[1mAB[mC[22mD[1m[22mE")
    assert s.bold_grid[0][0] and s.bold_grid[0][1]
    assert not s.bold_grid[0][2]
    assert not s.bold_grid[0][3]
    assert not s.bold_grid[0][4]


def test_indexed_background_recorded_as_is():
    s = VTScreen(10, 40)
    s.feed(b"\x1b[48;5;208mA")
    assert s.bg_grid[0][0] == ("idx", 208)


def test_scroll_keeps_grids_in_step():
    s = VTScreen(3, 10)
    # reset the SGR after "top": a persistent bg would keep colouring the
    # lines that scroll in, which is correct VT behaviour but noise here
    s.feed(b"\x1b[48;2;1;2;3mtop\x1b[m\r\n")
    for _ in range(4):
        s.feed(b"line\r\n")
    # the highlighted row scrolled off the top
    assert all(c != (1, 2, 3) for row in s.bg_grid for c in row)
    assert s.grid[0][0] == "l"
