"""Console line-model tests.

Every case here maps to a row of the byte-semantics table in
doc/win_specs.md §8.1, which is the specification the C port's
scroller_win.cpp implements. One deliberate difference: the C version counts
*bytes* per line, this one counts *characters*, so a multi-byte character is
atomic and can never be split. That is strictly stronger than the C rule
("never split a UTF-8 sequence; continuation bytes may overflow") and makes
the model independent of the source encoding.
"""

import pytest

from cgdbpy.console import ConsoleModel, ScrollState, TAB_WIDTH


def lines_of(text, width=80):
    m = ConsoleModel(width=width)
    m.add_text(text)
    return list(m.lines), "".join(m.cur), m.cur_col


# ---------------------------------------------------------------------------
# \n commits a line
# ---------------------------------------------------------------------------
def test_newline_commits():
    committed, cur, col = lines_of("one\ntwo\n")
    assert committed == ["one", "two"]
    assert cur == ""
    assert col == 0


def test_text_without_newline_stays_current():
    committed, cur, col = lines_of("partial")
    assert committed == []
    assert cur == "partial"
    assert col == 7


def test_empty_lines_are_preserved():
    committed, _, _ = lines_of("a\n\n\nb\n")
    assert committed == ["a", "", "", "b"]


# ---------------------------------------------------------------------------
# \r returns the cursor to column 0 (overwrite, not append)
# ---------------------------------------------------------------------------
def test_cr_returns_to_column_zero():
    committed, cur, col = lines_of("hello\r")
    assert cur == "hello"
    assert col == 0


def test_cr_then_text_overwrites():
    # this is exactly how the C port's synthesised echo redraws a line:
    # "\r" + "(gdb) " + line + trailing erase
    committed, cur, col = lines_of("hello\rhi")
    assert cur == "hillo"
    assert col == 2


def test_cr_full_redraw():
    committed, cur, col = lines_of("(gdb) abc\r(gdb) x  ")
    assert cur == "(gdb) x  "
    assert col == 9


# ---------------------------------------------------------------------------
# \b moves the cursor left, floor 0
# ---------------------------------------------------------------------------
def test_backspace_moves_left():
    _, cur, col = lines_of("abc\b")
    assert cur == "abc"
    assert col == 2


def test_backspace_at_column_zero_stays():
    _, cur, col = lines_of("\b\b\b")
    assert cur == ""
    assert col == 0


def test_backspace_then_char_overwrites():
    _, cur, col = lines_of("abc\bX")
    assert cur == "abX"
    assert col == 3


def test_backspace_does_not_delete():
    # \b is a cursor move, not an erase -- deleting is the line editor's job
    _, cur, col = lines_of("abc\b\b\b")
    assert cur == "abc"
    assert col == 0


# ---------------------------------------------------------------------------
# TAB advances to the next multiple of 8
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("start,expected_col", [
    (0, TAB_WIDTH),
    (1, TAB_WIDTH),
    (7, TAB_WIDTH),
    (8, 16),
    (9, 16),
    (15, 16),
    (16, 24),
])
def test_tab_stops(start, expected_col):
    m = ConsoleModel(width=200)
    m.add_text("x" * start)
    m.add_text("\t")
    assert m.cur_col == expected_col


def test_tab_pads_with_spaces():
    m = ConsoleModel(width=200)
    m.add_text("ab\tc")
    assert "".join(m.cur) == "ab" + " " * 6 + "c"


# ---------------------------------------------------------------------------
# wrapping at the width
# ---------------------------------------------------------------------------
def test_wrap_at_width():
    committed, cur, col = lines_of("abcdefghij", width=4)
    assert committed == ["abcd", "efgh"]
    assert cur == "ij"
    assert col == 2


def test_exact_width_does_not_wrap_prematurely():
    committed, cur, col = lines_of("abcd", width=4)
    assert committed == []
    assert cur == "abcd"
    assert col == 4


def test_wrap_on_the_char_after_a_full_line():
    committed, cur, col = lines_of("abcde", width=4)
    assert committed == ["abcd"]
    assert cur == "e"


def test_wrap_with_newline():
    committed, _, _ = lines_of("abcdefgh\nij", width=3)
    assert committed == ["abc", "def", "gh"]


# ---------------------------------------------------------------------------
# multi-byte characters are atomic
# ---------------------------------------------------------------------------
def test_utf8_is_not_split_on_wrap():
    # 3 two-byte chars into a width-2 window: each char must stay whole
    committed, cur, col = lines_of("ééé", width=2)
    assert committed == ["éé"]
    assert cur == "é"
    assert all(len(l) <= 2 for l in committed)


def test_cjk_characters():
    committed, cur, col = lines_of("中文测试", width=8)
    assert committed == []
    assert cur == "中文测试"
    assert col == 4


def test_utf8_split_across_add_bytes_calls():
    m = ConsoleModel(width=80)
    data = "中".encode("utf-8")
    assert len(data) == 3
    m.add_bytes(data[:1])
    assert "".join(m.cur) == ""       # held back, not emitted as mojibake
    m.add_bytes(data[1:2])
    assert "".join(m.cur) == ""
    m.add_bytes(data[2:])
    assert "".join(m.cur) == "中"


def test_add_bytes_handles_ascii_stream():
    m = ConsoleModel(width=80)
    for b in b"hello\nworld\n":
        m.add_bytes(bytes([b]))
    assert list(m.lines) == ["hello", "world"]


def test_incomplete_sequence_can_be_flushed():
    m = ConsoleModel(width=80)
    m.add_bytes("中".encode("utf-8")[:2])
    assert "".join(m.cur) == ""
    m.flush_pending()
    assert "".join(m.cur) != ""       # replacement chars, but nothing lost


# ---------------------------------------------------------------------------
# ignored control bytes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ch", ["\a", "\x00", "\x01", "\x0b", "\x0c",
                                "\x1b", "\x7f"])
def test_control_bytes_are_ignored(ch):
    m = ConsoleModel(width=80)
    m.add_text("ab" + ch + "cd")
    assert "".join(m.cur) == "abcd"
    assert m.cur_col == 4


def test_bel_does_not_advance_cursor():
    m = ConsoleModel(width=80)
    m.add_text("xy\a")
    assert m.cur_col == 2


# ---------------------------------------------------------------------------
# scrollback limit
# ---------------------------------------------------------------------------
def test_scrollback_trims_oldest():
    m = ConsoleModel(width=80, scrollback=3)
    m.add_text("1\n2\n3\n4\n5\n")
    assert list(m.lines) == ["3", "4", "5"]


def test_set_scrollback_trims_immediately():
    m = ConsoleModel(width=80, scrollback=100)
    m.add_text("".join("%d\n" % i for i in range(50)))
    assert len(m.lines) == 50
    m.set_scrollback(10)
    assert len(m.lines) == 10
    assert list(m.lines)[-1] == "49"


# ---------------------------------------------------------------------------
# visible_lines / scroll geometry
# ---------------------------------------------------------------------------
def test_visible_lines_bottom_by_default():
    m = ConsoleModel(width=80)
    m.add_text("".join("L%d\n" % i for i in range(20)))
    # the uncommitted current line counts as the bottom row, so with 20
    # committed lines the last 5 rows are L16..L19 plus the empty current row
    got = m.visible_lines(height=5, delta=0)
    assert got == ["L16", "L17", "L18", "L19", ""]


def test_visible_lines_scrolled_back():
    m = ConsoleModel(width=80)
    m.add_text("".join("L%d\n" % i for i in range(20)))
    got = m.visible_lines(height=5, delta=5)
    assert got == ["L11", "L12", "L13", "L14", "L15"]


def test_visible_lines_pads_when_short():
    m = ConsoleModel(width=80)
    m.add_text("only\n")
    got = m.visible_lines(height=4, delta=0)
    assert len(got) == 4
    assert got[-2:] == ["only", ""]


def test_visible_lines_includes_current_line():
    m = ConsoleModel(width=80)
    m.add_text("committed\npartial")
    got = m.visible_lines(height=2, delta=0)
    assert got == ["committed", "partial"]


def test_max_delta():
    m = ConsoleModel(width=80)
    m.add_text("".join("L%d\n" % i for i in range(20)))
    # 20 committed + 1 current row, 5 visible -> 16 rows of scrollback
    assert m.max_delta(height=5) == 16


def test_max_delta_never_negative():
    m = ConsoleModel(width=80)
    m.add_text("one\n")
    assert m.max_delta(height=50) == 0


def test_resize_keeps_committed_wrapping():
    # win_specs.md §8.1: already-committed lines keep their old wrapping
    m = ConsoleModel(width=4)
    m.add_text("abcdefgh\n")
    assert list(m.lines) == ["abcd", "efgh"]
    m.resize(20)
    assert list(m.lines) == ["abcd", "efgh"]
    m.add_text("ijklmnopqrst")
    assert "".join(m.cur) == "ijklmnopqrst"


def test_clear():
    m = ConsoleModel(width=80)
    m.add_text("a\nb\nc")
    m.clear()
    assert list(m.lines) == []
    assert "".join(m.cur) == ""
    assert m.cur_col == 0


# ---------------------------------------------------------------------------
# ScrollState
# ---------------------------------------------------------------------------
def test_scroll_state_enter_leave():
    s = ScrollState()
    assert not s.active
    s.enter(10)
    assert s.active and s.delta == 0
    s.leave()
    assert not s.active and s.delta == 0


def test_scroll_state_up_down_clamped():
    s = ScrollState()
    s.enter(10)
    s.up(3, 10, 5)
    assert s.delta == 3
    s.up(100, 10, 5)
    assert s.delta == 10          # clamped at max_delta
    s.down(4, 10, 5)
    assert s.delta == 6
    s.down(100, 10, 5)
    assert s.delta == 0           # clamped at the bottom


def test_scroll_state_home_end():
    s = ScrollState()
    s.enter(10)
    s.home(10, 5)
    assert s.delta == 10
    s.end(10, 5)
    assert s.delta == 0


def test_scroll_state_paging():
    s = ScrollState()
    s.enter(100)
    s.page_up(100, 20)
    assert s.delta == 19          # height-1, keeping one line of context
    s.page_down(100, 20)
    assert s.delta == 0


def test_scroll_state_status_format():
    s = ScrollState()
    s.enter(42)
    s.up(7, 42, 10)
    assert s.status(42) == "[7/42]"


# ---------------------------------------------------------------------------
# the synthesised echo protocol from win_specs.md §8.2
# ---------------------------------------------------------------------------
def test_synthesised_full_line_redraw():
    """The C port redraws an edited line as
    "\\r" + "(gdb) " + line + trailing erase + "\\b"*n
    (win_specs.md §8.2). Feeding that byte stream must leave the cursor
    sitting just past the logical end of the edited line.
    """
    m = ConsoleModel(width=80)
    m.add_text("(gdb) hello")
    assert m.cur_col == 11

    # user pressed backspace: "hello" -> "hell". The redraw writes the shorter
    # text, one space to erase the leftover 'o', then backs up over it.
    m.add_text("\r(gdb) hell \b")
    assert "".join(m.cur) == "(gdb) hell "
    assert m.cur_col == 10          # just past "hell"

    # typing a char now overwrites the erase padding in place
    m.add_text("y")
    assert "".join(m.cur) == "(gdb) helly"
    assert m.cur_col == 11


def test_redraw_shrinking_by_several_columns():
    m = ConsoleModel(width=80)
    m.add_text("(gdb) print foo")
    # Ctrl-U clears the line: redraw as prompt + 9 spaces of erase, back up 9
    m.add_text("\r(gdb)          \b\b\b\b\b\b\b\b\b")
    assert "".join(m.cur) == "(gdb)          "
    assert m.cur_col == 6           # right after "(gdb) "


def test_incremental_append_fast_path():
    m = ConsoleModel(width=80)
    m.add_text("(gdb) ")
    for ch in "print x":
        m.add_text(ch)
    assert "".join(m.cur) == "(gdb) print x"
    assert m.cur_col == 13


def test_output_interleaved_with_a_half_typed_line():
    """win_specs.md §8.2: when output arrives while a partial line is being
    edited, the C port emits "\\n" + output + a full redraw."""
    m = ConsoleModel(width=80)
    m.add_text("(gdb) pri")
    m.add_text("\n")
    m.add_text("Breakpoint 1, main () at t.c:5\n")
    m.add_text("\r(gdb) pri")
    committed = list(m.lines)
    assert committed[0] == "(gdb) pri"
    assert committed[1] == "Breakpoint 1, main () at t.c:5"
    assert "".join(m.cur) == "(gdb) pri"
