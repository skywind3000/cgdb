"""Unit tests for cgdbpy/vt.py -- the inferior's embedded terminal.

Everything here is platform-independent (pyte is pure Python); the POSIX
and e2e suites cover the pty wiring around it. Byte feeds use \r\n the
way the pty delivers them (its ONLCR has already turned the inferior's
\n into \r\n by the time the master reads it) -- feeding a bare \n keeps
the column, which is correct VT102 behaviour and was a trap during the
capability probing (see the LF test at the bottom).
"""

import re

import pytest

pyte = pytest.importorskip("pyte")

from cgdbpy.vt import InferiorScreen


def frags_text(frags):
    return "".join(t for _, t in frags)


def all_styles(frags):
    return [s for s, _ in frags]


def test_16_256_and_truecolor_styles():
    vt = InferiorScreen(4, 20)
    vt.feed(b"\x1b[31mRED\x1b[0m\r\n")
    vt.feed(b"\x1b[38;5;208mC256\x1b[0m\r\n")
    vt.feed(b"\x1b[38;2;10;20;30mTRUE\x1b[0m\r\n")
    assert "RED" in vt.row_text(0)
    assert any("fg:ansired" in s for s in all_styles(vt.row_fragments(0)))
    assert any("fg:#ff8700" in s for s in all_styles(vt.row_fragments(1)))
    assert any("fg:#0a141e" in s for s in all_styles(vt.row_fragments(2)))


def test_bold_reverse_underline():
    vt = InferiorScreen(4, 20)
    vt.feed(b"\x1b[1mB\x1b[0m\x1b[7mR\x1b[0m\x1b[4mU\x1b[0m\r\n")
    styles = " ".join(all_styles(vt.row_fragments(0)))
    assert "bold" in styles and "reverse" in styles and "underline" in styles


def test_clear_and_absolute_position():
    vt = InferiorScreen(6, 24)
    vt.feed(b"START\r\n")
    vt.feed(b"\x1b[2J\x1b[H\x1b[3;10HPOS\r\n")
    assert "START" not in vt.row_text(0)
    # 1-based row 3 col 10 -> 0-based (2, 9)
    assert vt.row_text(2)[9] == "P"


def test_alt_screen_restores_on_exit():
    vt = InferiorScreen(3, 20)
    vt.feed(b"BASE\r\n")
    vt.feed(b"\x1b[?1049h\x1b[2J\x1b[HALT\r\n")
    assert "ALT" in vt.row_text(0) or "HALT" in vt.row_text(0)
    vt.feed(b"\x1b[?1049l")
    assert vt.row_text(0).rstrip() == "BASE"


def test_alt_screen_exit_drops_scrolled_content():
    # a full-screen app scrolling inside the alt screen leaves nothing
    # behind on exit -- what vterm (and any real terminal) does
    vt = InferiorScreen(3, 20)
    vt.feed(b"BASE\r\n")
    vt.feed(b"\x1b[?1049h\x1b[2J\x1b[H")
    for i in range(6):
        vt.feed(b"v%d\r\n" % i)
    vt.feed(b"\x1b[?1049l")
    assert vt.row_text(0).rstrip() == "BASE"


def test_alt_screen_modes_47_and_1047_too():
    for mode in (47, 1047):
        vt = InferiorScreen(3, 20)
        vt.feed(b"BASE\r\n")
        vt.feed(b"\x1b[?%dh\x1b[HALT" % mode)
        vt.feed(b"\x1b[?%dl" % mode)
        assert vt.row_text(0).rstrip() == "BASE"


def test_wide_chars_advance_two_columns():
    vt = InferiorScreen(3, 20)
    vt.feed("AB你好\r\n".encode())
    # the cursor moved 6 columns for 4 characters
    x, y = vt.cursor_pos()
    assert (x, y) == (0, 1)         # wrapped after the newline
    vt2 = InferiorScreen(3, 20)
    vt2.feed("AB你".encode())
    assert vt2.cursor_pos()[0] == 4


def test_autowrap_and_its_off_state():
    vt = InferiorScreen(4, 10)
    vt.feed(b"x" * 11 + b"\r\n")
    assert vt.row_text(0) == "x" * 10
    assert vt.row_text(1) == "x"
    vt.feed(b"\x1b[?7l\x1b[H" + b"y" * 11)
    assert vt.row_text(0) == "y" * 10   # clamped at the last column


def test_scroll_region_confined():
    vt = InferiorScreen(6, 20)
    for i in range(6):
        vt.feed(b"r%d\r\n" % i)
    # the 7th linefeed scrolled r0 into history already
    vt.feed(b"\x1b[3;4r")             # DECSTBM rows 2..3 (1-based)
    vt.feed(b"\x1b[3;1HIN\r\nOVER\r\n")
    # rows outside the region never move: 0/1 hold r1/r2, row 4 holds r5
    screen = [vt.row_text(i) for i in range(vt.row_count())][-6:]
    assert screen[0].rstrip() == "r1"
    assert screen[1].rstrip() == "r2"
    assert screen[4].rstrip() == "r5"
    # inside the region the last write scrolled: OVER sits at region top
    assert screen[2].rstrip() == "OVER"


def test_scrollback_history_and_row_count():
    vt = InferiorScreen(4, 20)
    for i in range(10):
        vt.feed(b"line%d\r\n" % i)
    # 11 line advances -> 7 rows scrolled into history, nothing lost
    assert vt.row_count() == 4 + 7
    assert vt.row_text(0) == "line0"      # oldest, fully retained
    assert vt.row_text(6) == "line6"      # last history row
    assert vt.row_text(7) == "line7"      # first live screen row
    assert vt.row_text(10).rstrip() == "" # cursor row on the screen


def test_visible_row_range_and_max_delta():
    vt = InferiorScreen(4, 20)
    for i in range(10):
        vt.feed(b"line%d\r\n" % i)
    total = vt.row_count()
    assert list(vt.visible_row_range(4, 0)) == list(range(total - 4, total))
    assert list(vt.visible_row_range(4, 3)) == list(range(total - 7, total - 3))
    assert vt.max_delta(4) == total - 4


def test_extract_lines_drops_trailing_blanks_and_resets():
    vt = InferiorScreen(4, 20)
    vt.feed(b"one\r\ntwo\r\n")        # the rest of the screen is blank
    lines = vt.flatten()
    assert lines == ["one", "two"]
    assert vt.extract_lines() == []
    assert vt.row_count() == 4        # the screen itself is still there
    assert vt.cursor_pos() == (0, 0)


def test_flatten_is_idempotent():
    vt = InferiorScreen(4, 20)
    vt.feed(b"X\r\n")
    first = vt.flatten()
    assert first == ["X"]
    assert vt.flatten() == []


def test_reset_content_clears_scroll_region_and_alt_state():
    vt = InferiorScreen(6, 20)
    vt.feed(b"\x1b[?1049h\x1b[2J\x1b[3;4r\x1b[3;10HPOS")
    vt.reset_content()
    assert vt.extract_lines() == []
    # a fresh frame positions absolutely again: margins were reset
    vt.feed(b"\x1b[5;5HZ")
    assert vt.row_text(4)[4] == "Z"
    # and leaving the (forgotten) alt screen restores nothing
    vt.feed(b"\x1b[?1049l")
    assert vt.row_text(4)[4] == "Z"


def test_resize_keeps_bottom_content():
    vt = InferiorScreen(6, 20)
    for i in range(5):
        vt.feed(b"r%d\r\n" % i)
    vt.resize(4, 20)
    assert vt.size == (4, 20)
    text = [vt.row_text(i) for i in range(vt.row_count())]
    assert text[-1].rstrip() == ""     # cursor row, content above survived
    assert "r4" in "\n".join(text)


def test_cursor_row_text_and_clamp_target():
    vt = InferiorScreen(4, 20)
    vt.feed(b"abc")                    # cursor after 'c' on row 0
    assert vt.cursor_row() == 0
    assert vt.cursor_row_text() == "abc"
    # the app clamps the cursor's column against this length
    x, y = vt.cursor_pos()
    assert x <= len(vt.cursor_row_text())


def test_lone_lf_keeps_the_column():
    # pyte is VT102-correct: LF moves down without returning the column.
    # Real pty output arrives as \r\n (ONLCR), so cgdbpy never depends on
    # \n doing both -- this test pins that assumption, which cost an hour
    # of debugging during the capability probe.
    vt = InferiorScreen(4, 20)
    vt.feed(b"AB\nCD\n")
    assert vt.row_text(1).rstrip() == "  CD"
    vt2 = InferiorScreen(4, 20)
    vt2.feed(b"AB\r\nCD\r\n")
    assert vt2.row_text(1).rstrip() == "CD"


def test_row_fragments_trim_trailing_blanks():
    vt = InferiorScreen(2, 20)
    vt.feed(b"AB\r\n")
    frags = vt.row_fragments(0)
    assert frags_text(frags) == "AB"
    assert all(len(t) <= 2 for _, t in frags)
    # a fully blank row renders nothing (the app's \n separator only)
    assert vt.row_fragments(1) == []


def test_row_fragments_merge_equal_runs():
    vt = InferiorScreen(2, 20)
    vt.feed(b"\x1b[31mRED\x1b[31mRED2\x1b[0m plain\r\n")
    # 'RED' and 'RED2' share a style: one run, then the reset run
    frags = vt.row_fragments(0)
    assert len(frags) == 2
    assert frags[0][1] == "REDRED2"
    assert "fg:ansired" in frags[0][0]
    assert frags[1][1] == " plain"


def test_feed_accepts_text():
    vt = InferiorScreen(2, 20)
    vt.feed("\x1b[31mRED\x1b[0m\r\n")
    assert vt.row_text(0) == "RED"


def test_all_row_texts_spans_history_and_screen():
    vt = InferiorScreen(3, 20)
    for i in range(6):
        vt.feed(b"n%d\r\n" % i)
    texts = vt.all_row_texts()
    assert texts == [vt.row_text(i) for i in range(vt.row_count())]
    assert "n0" in texts[0] and "n5" in texts[-2]
