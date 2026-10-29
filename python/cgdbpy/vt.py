"""The inferior's terminal screen: a real VT inside the GDB window (POSIX).

Counterpart of ``cgdb/vterminal.cpp`` + libvterm in the C version: the
inferior's output is rendered by a terminal emulator embedded in the GDB
window, so colours, cursor movement, screen clearing and full-screen
programs survive (the C scroller has been a vterm since 0.8.0; this module
is what turns the POSIX side of the Python port from the dumb line model
into the same shape -- see doc/python_specs.md §21).

pyte plays libvterm's role. Its capability matrix was probed before this
module was written (SGR 16/256/true-colour, DECSTBM, DECAWM, wide chars,
HistoryScreen scrollback all work). Two gaps are handled here:

  * alternate screen (DECSET/DECRST 1049/1047/47) -- pyte ignores them,
    so a full-screen program would paint over the console and never
    restore it. ``_AltScreen`` patches the save/switch/restore.
  * terminal query replies (DA/DSR) -- pyte routes them to
    ``write_process_input``, a documented override hook that defaults to
    a noop. The C version never wires libvterm's output callback either
    (no ``vterm_output_set_callback`` anywhere in cgdb/), so replying
    nothing is the parity behaviour; wiring the hook later is a one-liner.

The gdb console channel keeps using the dumb line model (console.py):
in MI mode gdb's own output carries no escapes (python_specs.md §15.5),
so only the inferior's stream goes through here.

Thread model: ``feed`` runs on the inferior-pty reader thread, rendering
on the main thread, ``flatten``-time mutation on the reader thread -- the
same lock-free style the ConsoleModel already uses.
"""

import pyte

# pyte's colour names -> prompt_toolkit's ANSI tokens. pyte calls SGR 33
# "brown" (what xterm shows as yellow); aixterm's bright set is
# "brightbrown" there.
_ANSI_TO_PTK = {
    "black": "ansiblack", "red": "ansired", "green": "ansigreen",
    "brown": "ansiyellow", "blue": "ansiblue", "magenta": "ansimagenta",
    "cyan": "ansicyan", "white": "ansiwhite",
    "brightblack": "ansibrightblack", "brightred": "ansibrightred",
    "brightgreen": "ansibrightgreen", "brightbrown": "ansibrightyellow",
    "brightblue": "ansibrightblue", "brightmagenta": "ansibrightmagenta",
    "brightcyan": "ansibrightcyan", "brightwhite": "ansibrightwhite",
}

_ALT_MODES = (1049, 1047, 47)


def _color_token(value):
    """A pyte colour name/hex as a ptk colour token, or None for default."""
    if value in (None, "default"):
        return None
    return _ANSI_TO_PTK.get(value, "#" + value)


class _AltScreen(pyte.HistoryScreen):
    """HistoryScreen + alternate-screen save/restore.

    pyte dispatches private DECSET/DECRST to ``set_mode``/``reset_mode``
    with ``private=True`` (there is no ``set_private_mode``), and shifts
    the mode codes left by five bits before consulting its tables -- so
    the raw values must be intercepted before the super call.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._alt_saved = None

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private") and [m for m in modes if m in _ALT_MODES]:
            if self._alt_saved is None:
                # save the base screen AND its scrollback: pyte has no
                # second buffer, so scrolling inside the alt screen would
                # otherwise push its rows into the base screen's history
                self._alt_saved = (dict(self.buffer),
                                   list(self.history.top),
                                   self.cursor.x, self.cursor.y)
                self.buffer.clear()
                self.history.top.clear()
                self.cursor_position()
            rest = [m for m in modes if m not in _ALT_MODES]
            if rest:
                super().set_mode(*rest, private=True)
            return
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private") and [m for m in modes if m in _ALT_MODES]:
            if self._alt_saved is not None:
                buf, hist, x, y = self._alt_saved
                self.buffer.clear()
                self.buffer.update(buf)
                self.history.top.clear()
                self.history.top.extend(hist)
                self.cursor.x, self.cursor.y = x, y
                self._alt_saved = None
            rest = [m for m in modes if m not in _ALT_MODES]
            if rest:
                super().reset_mode(*rest, private=True)
            return
        super().reset_mode(*modes, **kwargs)

    def reset_content(self):
        """Back to a pristine screen (but keep the size).

        Screen.reset() does not touch the scrollback or the alt-screen
        state, so both are cleared here as well.
        """
        self.reset()
        self.history.top.clear()
        self._alt_saved = None


class InferiorScreen:
    """The inferior's virtual terminal, always sized to the GDB pane.

    pyte's constructor is (columns, lines); everything in this API speaks
    (rows, cols) like the rest of cgdbpy.
    """

    def __init__(self, rows, cols, history=10000):
        self._screen = _AltScreen(cols, rows, history=history)
        self._stream = pyte.ByteStream(self._screen)

    # --- input ------------------------------------------------------------
    def feed(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        self._stream.feed(data)

    @property
    def size(self):
        return (self._screen.lines, self._screen.columns)

    def resize(self, rows, cols):
        if (rows, cols) == self.size:
            return
        self._screen.resize(lines=rows, columns=cols)

    # --- rows -------------------------------------------------------------
    # A row index addresses the scrollback first (oldest first, like
    # vterm's sb_push), then the live screen, exactly the order the lines
    # would be read back from a terminal.
    def row_count(self):
        return len(self._screen.history.top) + self._screen.lines

    def visible_row_range(self, height, delta):
        """The rows a `height`-row window shows, scrolled back `delta`."""
        total = self.row_count()
        end = max(0, total - delta)
        start = max(0, end - height)
        return range(start, end)

    def max_delta(self, height):
        return max(0, self.row_count() - height)

    def _row(self, i):
        hist = self._screen.history.top
        if i < len(hist):
            return hist[i]
        return self._screen.buffer[i - len(hist)]

    def row_text(self, i):
        row = self._row(i)
        cols = self._screen.columns
        return "".join(row[x].data for x in range(cols)).rstrip()

    def row_fragments(self, i, base="class:console"):
        """One row as ptk style fragments, merging equal-attribute runs.

        Blank trailing cells are trimmed so the inferior cannot paint a
        whole row of styled spaces past its output (and frames stay
        cheap); the scroll-status overlay needs plain text anyway.
        """
        row = self._row(i)
        cols = self._screen.columns
        runs = []
        cur_style, cur_chars = None, []

        def _flush():
            if cur_chars:
                runs.append((cur_style, "".join(cur_chars)))

        for x in range(cols):
            ch = row[x]
            style = _cell_style(ch, base)
            if style != cur_style:
                _flush()
                cur_style, cur_chars = style, []
            cur_chars.append(ch.data)
        _flush()

        while runs and runs[-1][1].strip() == "" and runs[-1][0] == base:
            runs.pop()
        if runs:
            style, text = runs[-1]
            trimmed = text.rstrip()
            if trimmed:
                runs[-1] = (style, trimmed)
            else:
                runs.pop()
        return runs

    def all_row_texts(self):
        return [self.row_text(i) for i in range(self.row_count())]

    # --- cursor -----------------------------------------------------------
    def cursor_pos(self):
        return (self._screen.cursor.x, self._screen.cursor.y)

    def cursor_row(self):
        """The cursor's row within the live screen (scroll-mode origin)."""
        return min(self._screen.cursor.y, self._screen.lines - 1)

    def cursor_row_text(self):
        """The rstripped text of the row the terminal cursor sits on.

        prompt_toolkit can only place a cursor on a written cell, so the
        app clamps the cursor's column against this.
        """
        y = self.cursor_row()
        i = self.row_count() - self._screen.lines + y
        i = max(0, min(i, self.row_count() - 1))
        return self.row_text(i)

    # --- flatten ------------------------------------------------------------
    def extract_lines(self):
        """The whole history as plain text lines, ready for ConsoleModel."""
        lines = [self.row_text(i) for i in range(self.row_count())]
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def is_blank(self):
        """True when there is nothing to flatten (cheap early-out)."""
        if self._screen.history.top:
            return False
        return not any(row.strip() for row in self._screen.display)

    def reset_content(self):
        self._screen.reset_content()
        self._stream = pyte.ByteStream(self._screen)

    def flatten(self):
        """extract_lines + reset_content in one step (returns the lines)."""
        lines = self.extract_lines()
        self.reset_content()
        return lines


def _cell_style(ch, base):
    parts = [base]
    fg = _color_token(ch.fg)
    if fg:
        parts.append("fg:" + fg)
    bg = _color_token(ch.bg)
    if bg:
        parts.append("bg:" + bg)
    if ch.bold:
        parts.append("bold")
    if ch.underscore:
        parts.append("underline")
    if ch.italics:
        parts.append("italic")
    if ch.reverse:
        parts.append("reverse")
    return " ".join(parts)
