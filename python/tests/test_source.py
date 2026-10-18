"""Source buffer, highlighting and breakpoint table tests.

The highlighting tests encode the finding from doc/python_specs.md §3.6:
lexing only the visible window produces WRONG colours for C/C++, because a
window can start inside a multi-line comment or raw string. The cache-everything
strategy used here is asserted against a full-file lex as the reference.
"""

import os
import tempfile

import pytest
from pygments import lex
from pygments.lexers import CppLexer

from cgdbpy.source import (ASYNC_LEX_THRESHOLD, Breakpoints, SourceFile,
                           SourceView)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TGDB_CPP = os.path.join(REPO, "lib", "tgdb", "tgdb.cpp")


def write_tmp(text, suffix=".c", encoding="utf-8"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
        f.write(text)
    return path


# ---------------------------------------------------------------------------
# loading and encoding
# ---------------------------------------------------------------------------
def test_load_lines():
    p = write_tmp("int a;\nint b;\nint c;\n")
    try:
        s = SourceFile(p)
        assert len(s) == 3
        assert s.line(0) == "int a;"
        assert s.line(2) == "int c;"
    finally:
        os.unlink(p)


def test_trailing_newline_does_not_create_a_phantom_line():
    p = write_tmp("a\nb\n")
    try:
        assert len(SourceFile(p)) == 2
    finally:
        os.unlink(p)


def test_no_trailing_newline():
    p = write_tmp("a\nb")
    try:
        s = SourceFile(p)
        assert len(s) == 2
        assert s.line(1) == "b"
    finally:
        os.unlink(p)


def test_crlf_file_is_normalised():
    fd, p = tempfile.mkstemp(suffix=".c")
    with os.fdopen(fd, "wb") as f:
        f.write(b"int a;\r\nint b;\r\n")
    try:
        s = SourceFile(p)
        assert s.crlf is True
        assert len(s) == 2
        assert s.line(0) == "int a;"       # no stray \r
        assert s.line(1) == "int b;"
    finally:
        os.unlink(p)


def test_utf8_with_bom():
    p = write_tmp("\ufeffint main() { return 0; }\n", encoding="utf-8")
    try:
        s = SourceFile(p)
        assert s.encoding == "utf-8-sig"
        assert s.line(0).startswith("int main")
    finally:
        os.unlink(p)


def test_cjk_content_survives():
    p = write_tmp('// 中文注释\nint 变量 = 1;\n', encoding="utf-8")
    try:
        s = SourceFile(p)
        assert "中文注释" in s.line(0)
    finally:
        os.unlink(p)


def test_latin1_fallback_never_raises():
    fd, p = tempfile.mkstemp(suffix=".c")
    with os.fdopen(fd, "wb") as f:
        f.write(b"// caf\xe9 comment\nint x;\n")     # latin-1 é, invalid utf-8
    try:
        s = SourceFile(p)
        assert len(s) == 2
        assert s.line(1) == "int x;"
    finally:
        os.unlink(p)


def test_line_out_of_range_is_empty():
    p = write_tmp("only\n")
    try:
        s = SourceFile(p)
        assert s.line(-1) == ""
        assert s.line(99) == ""
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# highlighting
# ---------------------------------------------------------------------------
def test_highlight_sync_produces_per_line_tokens():
    p = write_tmp("int main(void) {\n    return 0;\n}\n")
    try:
        s = SourceFile(p)
        s.highlight_sync()
        assert s.lex_state == "ready"
        toks = s.tokens_for_line(0)
        assert toks is not None
        text = "".join(v for _, v in toks)
        assert text == "int main(void) {"
    finally:
        os.unlink(p)


def test_tokens_reassemble_the_original_line():
    """For every line, concatenating the cached tokens must reproduce it."""
    p = write_tmp(
        '#include <stdio.h>\n'
        '/* a multi-line\n'
        '   comment */\n'
        'int main(void) {\n'
        '    const char *s = "a string with // and /* in it";\n'
        '    printf("%d\\n", 42);\n'
        '    return 0;\n'
        '}\n')
    try:
        s = SourceFile(p)
        s.highlight_sync()
        for i in range(len(s)):
            toks = s.tokens_for_line(i)
            assert toks is not None, "line %d has no tokens" % i
            assert "".join(v for _, v in toks) == s.line(i), \
                "line %d does not reassemble" % i
    finally:
        os.unlink(p)


def test_tokens_before_highlighting_are_none():
    p = write_tmp("int x;\n")
    try:
        s = SourceFile(p)
        assert s.lex_state == "none"
        assert s.tokens_for_line(0) is None
    finally:
        os.unlink(p)


def test_highlight_async_completes():
    p = write_tmp("int main() { return 0; }\n" * 50)
    try:
        s = SourceFile(p)
        done = []
        t = s.highlight_async(on_done=lambda f: done.append(f))
        t.join(timeout=30)
        assert s.lex_state == "ready"
        assert done == [s]
        assert s.tokens_for_line(0) is not None
    finally:
        os.unlink(p)


def test_ensure_highlighted_is_inline_for_small_files():
    p = write_tmp("int x;\n")
    try:
        s = SourceFile(p)
        s.ensure_highlighted()
        assert s.lex_state == "ready"
    finally:
        os.unlink(p)


def test_ensure_highlighted_defers_large_files():
    p = write_tmp("int x;\n" * (ASYNC_LEX_THRESHOLD + 10))
    try:
        s = SourceFile(p)
        s.ensure_highlighted()
        assert s.lex_state in ("pending", "ready")
        # wait for the background thread to finish so the temp file can go
        for _ in range(200):
            if s.lex_state == "ready":
                break
            import time
            time.sleep(0.05)
        assert s.lex_state == "ready"
    finally:
        os.unlink(p)


def test_unknown_extension_falls_back_to_text():
    p = write_tmp("some arbitrary text\n", suffix=".notareallanguage")
    try:
        s = SourceFile(p)
        s.highlight_sync()
        assert s.lex_state == "ready"
        assert s.tokens_for_line(0) is not None
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# the §6.1 finding: windowed lexing is wrong, cached full lexing is right
# ---------------------------------------------------------------------------
def _reference_line_tokens(path):
    """Independent reference: lex the whole file with Pygments directly."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out = [[] for _ in lines]
    ln = 0
    for tok, val in lex(text, CppLexer()):
        if ln >= len(out):
            break
        for j, part in enumerate(val.split("\n")):
            if ln + j < len(out) and part:
                out[ln + j].append((tok, part))
        ln += val.count("\n")
    return out


def _windowed_lex(path, start, count):
    """The naive approach: lex only the visible window in isolation."""
    with open(path, encoding="utf-8", errors="replace") as f:
        all_lines = f.read().split("\n")
    chunk = "\n".join(all_lines[start:start + count])
    out = [[] for _ in range(count)]
    ln = 0
    for tok, val in lex(chunk, CppLexer()):
        if ln >= count:
            break
        for j, part in enumerate(val.split("\n")):
            if ln + j < count and part:
                out[ln + j].append((tok, part))
        ln += val.count("\n")
    return out


@pytest.mark.skipif(not os.path.exists(TGDB_CPP),
                    reason="repo source not available")
def test_cached_highlighting_matches_a_full_file_lex():
    """The strategy we ship must agree with an independent full-file lex."""
    s = SourceFile(TGDB_CPP)
    s.highlight_sync()
    ref = _reference_line_tokens(TGDB_CPP)
    assert len(ref) == len(s)
    mismatches = []
    for i in range(len(s)):
        mine = [(t, v) for t, v in (s.tokens_for_line(i) or [])]
        theirs = [(t, v) for t, v in ref[i]]
        if mine != theirs:
            mismatches.append(i)
    assert not mismatches, "lines diverging from the reference: %r" % (
        mismatches[:20],)


@pytest.mark.skipif(not os.path.exists(TGDB_CPP),
                    reason="repo source not available")
def test_windowed_lexing_is_demonstrably_wrong():
    """Proves the naive alternative is not acceptable.

    This is the measurement behind doc/python_specs.md §3.6: on
    lib/tgdb/tgdb.cpp, 6 of 11 sampled 40-line windows disagree with the
    full-file lex. If this ever stops reproducing, the justification for the
    caching strategy needs re-examining.
    """
    ref = _reference_line_tokens(TGDB_CPP)
    total = len(ref)
    win = 40
    offsets = [o for o in (0, 100, 300, 500, 700, 900, 1100, 1300, 1500,
                           1700, 2000) if o + win <= total]
    assert len(offsets) >= 8
    diverging = 0
    worst = 0
    for off in offsets:
        got = _windowed_lex(TGDB_CPP, off, win)
        bad = 0
        for j in range(win):
            i = off + j
            if [(t, v) for t, v in got[j]] != [(t, v) for t, v in ref[i]]:
                bad += 1
        if bad:
            diverging += 1
            worst = max(worst, bad)
    assert diverging >= 4, (
        "expected windowed lexing to diverge on several windows, got %d"
        % diverging)
    assert worst >= 20, (
        "expected at least one window to be badly wrong, worst was %d lines"
        % worst)


@pytest.mark.skipif(not os.path.exists(TGDB_CPP),
                    reason="repo source not available")
def test_a_window_starting_inside_a_block_comment_is_the_failure_mode():
    """Pin the concrete mechanism: a window that *opens inside* a `/** ... */`
    block -- so the `/*` is not visible -- colours comment text as code."""
    with open(TGDB_CPP, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")

    # find a line strictly inside a multi-line comment: the comment must have
    # opened on an earlier line and not yet closed
    inside = None
    opened = None
    depth = 0
    for i, ln in enumerate(lines):
        before = depth
        depth += ln.count("/*") - ln.count("*/")
        if before > 0 and depth > 0 and ln.strip():
            inside, opened = i, opened
            break
        if before == 0 and depth > 0:
            opened = i
    assert inside is not None, "no multi-line comment found to test against"
    assert opened is not None and opened < inside

    ref = _reference_line_tokens(TGDB_CPP)
    # the window starts AT the interior line, so the opening /* is not in view
    got = _windowed_lex(TGDB_CPP, inside, 20)
    from pygments.token import Comment
    ref_is_comment = any(t in Comment for t, _ in ref[inside])
    got_is_comment = any(t in Comment for t, _ in got[0])
    assert ref_is_comment, (
        "reference says line %d is not a comment: %r" % (inside, lines[inside]))
    assert not got_is_comment, (
        "windowed lex unexpectedly agreed; the failure mode moved")


# ---------------------------------------------------------------------------
# SourceView geometry
# ---------------------------------------------------------------------------
def make_view(height=10, total=100):
    v = SourceView(height=height)
    return v, total


def test_view_initial_state():
    v, _ = make_view()
    assert v.top == 0 and v.cursor == 0


def test_move_to_clamps():
    v, total = make_view()
    v.move_to(-5, total)
    assert v.cursor == 0
    v.move_to(9999, total)
    assert v.cursor == total - 1


def test_move_to_scrolls_down():
    v, total = make_view(height=10)
    v.move_to(15, total)
    assert v.cursor == 15
    assert v.top == 6              # 15 - 10 + 1


def test_move_to_scrolls_up():
    v, total = make_view(height=10)
    v.move_to(50, total)
    v.move_to(3, total)
    assert v.top == 3


def test_ensure_visible_is_minimal():
    v, total = make_view(height=10)
    # cursor already on screen -> no scroll at all
    v.top = 20
    v.cursor = 25
    v.ensure_visible()
    assert v.top == 20

    # cursor below the window -> scroll just enough to show it at the bottom
    v.cursor = 35
    v.ensure_visible()
    assert v.top == 26             # 35 - 10 + 1

    # cursor above the window -> scroll up to it
    v.cursor = 5
    v.ensure_visible()
    assert v.top == 5


def test_center():
    v, total = make_view(height=10)
    v.cursor = 50
    v.center(total)
    assert v.top == 45


def test_center_clamps_at_the_end():
    v, total = make_view(height=10)
    v.cursor = 99
    v.center(total)
    assert v.top == 90             # total - height


def test_scroll_clamps():
    v, total = make_view(height=10)
    v.scroll(-100, total)
    assert v.top == 0
    v.scroll(1000, total)
    assert v.top == 90


def test_marks():
    v, total = make_view()
    v.cursor = 42
    v.set_mark("a")
    assert v.goto_mark("a") == 42
    assert v.goto_mark("z") is None


def test_resize():
    v, _ = make_view(height=10)
    v.resize(0)
    assert v.height == 1           # never zero


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def make_search_file():
    return write_tmp(
        "int alpha = 1;\n"
        "int beta = 2;\n"
        "int gamma = 3;\n"
        "int beta_again = 4;\n"
        "int omega = 5;\n")


def test_search_forward():
    p = make_search_file()
    try:
        s = SourceFile(p)
        v = SourceView(height=5)
        assert v.search_in(s, "beta", forward=True) == 1
        assert v.cursor == 1
        assert v.search_in(s, forward=True) == 3      # repeats last pattern
    finally:
        os.unlink(p)


def test_search_backward():
    p = make_search_file()
    try:
        s = SourceFile(p)
        v = SourceView(height=5)
        v.cursor = 4
        assert v.search_in(s, "beta", forward=False) == 3
        assert v.search_in(s, forward=False) == 1
    finally:
        os.unlink(p)


def test_search_wraps_when_wrapscan_on():
    p = make_search_file()
    try:
        s = SourceFile(p)
        v = SourceView(height=5)
        v.cursor = 4
        v.wrapscan = True
        assert v.search_in(s, "alpha", forward=True) == 0
    finally:
        os.unlink(p)


def test_search_does_not_wrap_when_wrapscan_off():
    p = make_search_file()
    try:
        s = SourceFile(p)
        v = SourceView(height=5)
        v.cursor = 4
        v.wrapscan = False
        assert v.search_in(s, "alpha", forward=True) is None
    finally:
        os.unlink(p)


def test_search_ignorecase():
    p = make_search_file()
    try:
        s = SourceFile(p)
        v = SourceView(height=5)
        assert v.search_in(s, "ALPHA", ignorecase=False) is None
        assert v.search_in(s, "ALPHA", ignorecase=True) == 0
    finally:
        os.unlink(p)


def test_search_regex():
    p = make_search_file()
    try:
        s = SourceFile(p)
        v = SourceView(height=5)
        assert v.search_in(s, r"int \w+_again") == 3
    finally:
        os.unlink(p)


def test_search_invalid_regex_returns_none():
    p = make_search_file()
    try:
        s = SourceFile(p)
        v = SourceView(height=5)
        assert v.search_in(s, "([unbalanced") is None
    finally:
        os.unlink(p)


def test_search_no_pattern_returns_none():
    p = make_search_file()
    try:
        s = SourceFile(p)
        v = SourceView(height=5)
        assert v.search_in(s, None) is None
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# Breakpoints
# ---------------------------------------------------------------------------
BT_TWO = {
    "BreakpointTable": {
        "nr_rows": "2",
        "body": [
            {"bkpt": {"number": "1", "enabled": "y",
                      "fullname": "/tmp/t.c", "line": "12"}},
            {"bkpt": {"number": "2", "enabled": "n",
                      "fullname": "/tmp/t.c", "line": "30"}},
        ],
    }
}

BT_ONE = {
    "BreakpointTable": {
        "nr_rows": "1",
        "body": {"bkpt": {"number": "1", "enabled": "y",
                          "fullname": "/tmp/t.c", "line": "12"}},
    }
}


def test_breakpoints_two_rows():
    b = Breakpoints()
    b.update_from_mi(BT_TWO)
    assert b.numbers() == ["1", "2"]
    assert b.at("/tmp/t.c", 12) == ["1"]
    assert b.at("/tmp/t.c", 30) == ["2"]
    assert b.at("/tmp/t.c", 99) == []


def test_breakpoints_single_row_is_a_dict():
    """The MI trap: one row comes back as a dict, not a one-element list."""
    b = Breakpoints()
    b.update_from_mi(BT_ONE)
    assert b.numbers() == ["1"]
    assert b.at("/tmp/t.c", 12) == ["1"]


def test_breakpoints_empty_table():
    b = Breakpoints()
    b.update_from_mi({"BreakpointTable": {"nr_rows": "0", "body": []}})
    assert b.numbers() == []


def test_breakpoints_missing_table():
    b = Breakpoints()
    b.update_from_mi({})
    b.update_from_mi(None)
    assert b.numbers() == []


def test_breakpoints_multi_location():
    """`break foo` on an inlined/template function yields 1.1, 1.2, ..."""
    b = Breakpoints()
    b.update_from_mi({"BreakpointTable": {"body": [
        {"bkpt": {"number": "1.1", "enabled": "y",
                  "fullname": "/tmp/t.c", "line": "12"}},
        {"bkpt": {"number": "1.2", "enabled": "y",
                  "fullname": "/tmp/t.c", "line": "40"}},
    ]}})
    assert b.numbers() == ["1"]
    assert b.at("/tmp/t.c", 12) == ["1"]
    assert b.at("/tmp/t.c", 40) == ["1"]


def test_breakpoint_created_async():
    b = Breakpoints()
    changed = b.absorb_async("breakpoint-created", {
        "bkpt": {"number": "3", "enabled": "y",
                 "fullname": "/tmp/t.c", "line": "7"}})
    assert changed is True
    assert b.at("/tmp/t.c", 7) == ["3"]


def test_breakpoint_deleted_async():
    b = Breakpoints()
    b.update_from_mi(BT_TWO)
    assert b.absorb_async("breakpoint-deleted",
                          {"id": "1", "bkpt": {"number": "1"}}) is True
    assert b.numbers() == ["2"]
    assert b.absorb_async("breakpoint-deleted", {"bkpt": {"number": "99"}}) \
        is False


def test_breakpoint_modified_async_toggles_enabled():
    b = Breakpoints()
    b.update_from_mi(BT_TWO)
    assert b.all()[1]["enabled"] is False
    b.absorb_async("breakpoint-modified", {
        "bkpt": {"number": "2", "enabled": "y",
                 "fullname": "/tmp/t.c", "line": "30"}})
    assert b.all()[1]["enabled"] is True


def test_breakpoints_path_matching_is_case_and_form_insensitive():
    b = Breakpoints()
    b.update_from_mi({"BreakpointTable": {"body": [
        {"bkpt": {"number": "1", "enabled": "y",
                  "fullname": "/tmp/./t.c", "line": "12"}}]}})
    assert b.at("/tmp/t.c", 12) == ["1"]


def test_breakpoints_clear():
    b = Breakpoints()
    b.update_from_mi(BT_TWO)
    b.clear()
    assert b.numbers() == []
