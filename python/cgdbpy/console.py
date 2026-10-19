"""The synthesised GDB console window.

Python counterpart of cgdb/scroller_win.cpp. In MI mode gdb's own readline is
not running, so its console output is plain text with no cursor-movement
escapes; the only editing primitives that matter are CR / BS / overwrite. That
is what doc/win_specs.md §8.1 calls the "dumb terminal" line model, and this
module implements exactly the byte semantics recorded there:

    \\n      commit the current line
    \\r      cursor column -> 0
    \\b      cursor column -= 1 (floor 0)
    TAB      advance to the next multiple of 8
    other    printable/UTF-8 bytes overwrite at the cursor and advance
    wrap     at `width` columns, never splitting a UTF-8 sequence
             (continuation bytes are allowed to overflow)
    BEL etc  ignored

The interactive line editor itself is NOT here: prompt_toolkit's Buffer gives
history, word motion and kill commands for free (doc/python_specs.md §3.9),
so this module only models what gets *displayed*.
"""

from collections import deque

TAB_WIDTH = 8
DEFAULT_SCROLLBACK = 10000

# C0 controls that the model consumes rather than ignores
_CTRL_NEWLINE = 0x0A
_CTRL_CR = 0x0D
_CTRL_BS = 0x08
_CTRL_TAB = 0x09


def _is_utf8_continuation(b):
    return (b & 0xC0) == 0x80


class ConsoleModel:
    """Committed lines + the line currently being written."""

    def __init__(self, width=80, scrollback=DEFAULT_SCROLLBACK):
        self.width = max(1, width)
        self.scrollback = scrollback
        self.lines = deque()
        self.cur = []           # list[str], one entry per character
        self.cur_col = 0
        self._pending = bytearray()   # incomplete UTF-8 sequence
        self.total_added = 0

    # --- geometry ---------------------------------------------------------
    def resize(self, width):
        """Change the wrap width.

        Already-committed lines keep their old wrapping, matching the C port's
        documented v1 simplification (win_specs.md §8.1: "已提交行保持旧折行").
        """
        self.width = max(1, width)

    def set_scrollback(self, n):
        self.scrollback = max(1, n)
        self._trim()

    def _trim(self):
        over = len(self.lines) - self.scrollback
        if over > 0:
            for _ in range(over):
                self.lines.popleft()

    # --- input ------------------------------------------------------------
    def add_bytes(self, data):
        """Feed raw bytes. Splits UTF-8 sequences that straddle calls."""
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        buf = self._pending + bytearray(data)
        self._pending = bytearray()
        # hold back a trailing incomplete sequence
        cut = len(buf)
        if cut:
            # walk back over continuation bytes to find the lead byte
            i = cut - 1
            while i >= 0 and _is_utf8_continuation(buf[i]):
                i -= 1
            if i >= 0:
                lead = buf[i]
                if lead >= 0xC0:
                    if lead >= 0xF0:
                        need = 4
                    elif lead >= 0xE0:
                        need = 3
                    else:
                        need = 2
                    have = cut - i
                    if have < need:
                        self._pending = buf[i:]
                        cut = i
        text = bytes(buf[:cut]).decode("utf-8", "replace")
        self.add_text(text)

    def add_text(self, text):
        for ch in text:
            self._add_char(ch)
        self._trim()

    def _add_char(self, ch):
        o = ord(ch)
        if o == _CTRL_NEWLINE:
            self.commit_line()
            return
        if o == _CTRL_CR:
            self.cur_col = 0
            return
        if o == _CTRL_BS:
            if self.cur_col > 0:
                self.cur_col -= 1
            return
        if o == _CTRL_TAB:
            self._advance_to_tab_stop()
            return
        if o < 32 or o == 127:
            # BEL and friends are ignored (win_specs.md §8.1)
            return
        self._overwrite(ch)

    def _advance_to_tab_stop(self):
        nxt = (self.cur_col // TAB_WIDTH + 1) * TAB_WIDTH
        # a tab that runs past the width wraps like any other advance
        while self.cur_col < nxt:
            self._pad_one()
            if self.cur_col >= self.width:
                break

    def _pad_one(self):
        if self.cur_col >= self.width:
            self.commit_line()
        if self.cur_col < len(self.cur):
            self.cur_col += 1
        else:
            self.cur.append(" ")
            self.cur_col += 1

    def _overwrite(self, ch):
        if self.cur_col >= self.width:
            self.commit_line()
        if self.cur_col < len(self.cur):
            self.cur[self.cur_col] = ch
        else:
            while len(self.cur) < self.cur_col:
                self.cur.append(" ")
            self.cur.append(ch)
        self.cur_col += 1
        self.total_added += 1

    def commit_line(self):
        self.lines.append("".join(self.cur))
        self.cur = []
        self.cur_col = 0
        self._trim()

    def flush_pending(self):
        """Force any held-back incomplete UTF-8 sequence out as replacement."""
        if self._pending:
            text = bytes(self._pending).decode("utf-8", "replace")
            self._pending = bytearray()
            self.add_text(text)

    # --- view -------------------------------------------------------------
    def current_line(self, prefix=""):
        """The line being written, with an optional prompt prefix."""
        body = "".join(self.cur)
        if not prefix:
            return body
        # the cursor column is relative to the body, so the prefix shifts it
        return prefix + body

    def cursor_column(self, prefix_len=0):
        return min(self.cur_col + prefix_len, self.width - 1)

    def line_count(self):
        """Total displayable rows: committed lines plus the current one."""
        return len(self.lines) + (1 if self.cur or True else 0)

    def visible_lines(self, height, delta=0):
        """Return the `height` rows to display, scrolled back by `delta`.

        delta is measured in rows from the bottom, matching cgdb's scroll mode.
        The current (uncommitted) line counts as the last row.
        """
        allrows = list(self.lines) + ["".join(self.cur)]
        total = len(allrows)
        end = total - delta
        start = end - height
        out = []
        for i in range(start, end):
            out.append(allrows[i] if 0 <= i < total else "")
        return out

    def max_delta(self, height):
        total = len(self.lines) + 1
        return max(0, total - height)

    def search(self, rx, start_row, forward=True):
        """scr_search_next: index of the next row matching `rx`, or None.

        Rows are numbered like visible_lines(): 0 is the top committed
        line and len(lines) is the current (uncommitted) row.
        `start_row` is the previous hit (None starts from the far end).
        """
        rows = list(self.lines) + ["".join(self.cur)]
        n = len(rows)
        if forward:
            first = (start_row + 1) if start_row is not None else 0
            rng = range(first, n)
        else:
            first = (start_row - 1) if start_row is not None else n - 1
            rng = range(first, -1, -1)
        for i in rng:
            if rx.search(rows[i]):
                return i
        return None

    def clear(self):
        self.lines.clear()
        self.cur = []
        self.cur_col = 0
        self._pending = bytearray()


class ScrollState:
    """cgdb's scroll mode: delta rows back from the bottom, plus a cursor."""

    def __init__(self):
        self.active = False
        self.delta = 0
        self.row = 0
        self.col = 0

    def enter(self, max_delta):
        self.active = True
        self.delta = 0
        self.row = 0
        self.col = 0

    def leave(self):
        self.active = False
        self.delta = 0

    def up(self, n, max_delta, height):
        self.delta = min(self.delta + n, max_delta)
        self.row = max(0, self.row - n)

    def down(self, n, max_delta, height):
        self.delta = max(0, self.delta - n)
        self.row = min(height - 1, self.row + n)

    def home(self, max_delta, height):
        self.delta = max_delta
        self.row = 0

    def end(self, max_delta, height):
        self.delta = 0
        self.row = 0

    def page_up(self, max_delta, height):
        self.up(max(1, height - 1), max_delta, height)

    def page_down(self, max_delta, height):
        self.down(max(1, height - 1), max_delta, height)

    def status(self, max_delta):
        return "[%d/%d]" % (self.delta, max_delta)
