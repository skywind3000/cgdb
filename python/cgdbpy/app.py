"""The cgdb TUI, built on prompt_toolkit.

Layout mirrors cgdb: source pane on top, GDB console below, one status line,
and a `:` / `/` command line that replaces the status line while active.

    +------------------------------------------+
    | source pane (line numbers, bp marks)     |
    +------------------------------------------+
    | status line  <- the : and / command line |
    +------------------------------------------+
    | gdb console scrollback                   |
    +------------------------------------------+
    | (gdb) <edit line>                        |
    +------------------------------------------+

Focus starts in the GDB window, as in the C version, so gdb commands can be
typed straight away.

Ctrl-W swaps to the vertical split (interface.cpp:17-31): source and the
status line stack in the left column, a one-column '|' separator, and the
gdb console and edit line fill the right column:

    +-------------------------|---------------+
    | source pane             | gdb console   |
    |                         |               |
    | status <- : / cmdline   | (gdb) edit    |
    +-------------------------|---------------+

Pane sizes are Dimensions computed from the *live* terminal size, so
resizing the terminal re-derives the split on the next repaint.

Two deliberate deviations from the C version, both recorded in
doc/python_specs.md (§3.9 and §16.1):

  * The console is split into a scrollback region plus a pinned edit line
    instead of one scrolling terminal. prompt_toolkit's Buffer then provides
    the whole line editor (history, word motion, kill ring) for free, which is
    what the C port had to hand-write in 405 lines (tgdb.cpp:241-645).
  * prompt_toolkit decodes terminal input itself (on Windows via
    ReadConsoleInputW), so kui's terminal_keys layer is bypassed and only the
    user-map layer runs. The terminal layer still exists and is what the 198
    ported dejagnu cases exercise.
"""

import os
import re
import subprocess
import sys
import threading

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import (BufferControl,
                                           FormattedTextControl)
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.processors import BeforeInput

from . import keycodes as kc
from . import kui
from . import mi
from .cgdbrc import (Config, ConfigError, WSO_HORIZONTAL, WSO_VERTICAL)
from .console import ConsoleModel, ScrollState
from .filedlg import LABEL, FileDialog
from .gdbclient import GdbClient, GdbError
from .highlight import build_style, style_for_token
from .inferior_tty import create as create_inferior_tty
from .source import Breakpoints, SourceFile, SourceView

FOCUS_SOURCE = "source"
FOCUS_GDB = "gdb"
FOCUS_COMMAND = "command"
FOCUS_FILEDLG = "filedlg"

GDB_PROMPT = "(gdb) "

# Quarter marks the `+` and `_` keys jump between, as in WIN_SPLIT_TYPE
# (cgdbrc.h:96-102): what fraction of the screen the source pane gets.
# None means "not on a mark" (C's WIN_SPLIT_FREE), reached via `=` / `-`.
# The second spellings are the deprecated ones cgdbrc.cpp still accepts.
_SPLIT_MARKS = {
    "gdb_full": -2, "bottom_full": -2,
    "gdb_big": -1, "bottom_big": -1,
    "even": 0,
    "src_big": 1, "top_big": 1,
    "src_full": 2, "top_full": 2,
}

# `:` commands that drive gdb, transcribed from the commands[] table
# (cgdbrc.cpp:98-141). The -exec-* ones are MI commands; start/up/down/kill
# have no MI form and go through the console interpreter, like tgdb does.
_TGDB_COMMANDS = {
    "run": "-exec-run", "r": "-exec-run",
    "start": "start",
    "continue": "-exec-continue", "c": "-exec-continue",
    "next": "-exec-next", "n": "-exec-next",
    "nexti": "-exec-next-instruction",
    "step": "-exec-step", "s": "-exec-step",
    "stepi": "-exec-step-instruction",
    "finish": "-exec-finish", "f": "-exec-finish",
    "until": "-exec-until", "u": "-exec-until",
    "up": "up", "down": "down",
    "kill": "kill", "k": "kill",
}

# prompt_toolkit Keys -> cgdb_key ints. Anything not listed falls through to
# the single-character case in translate_key().
_PTK_TO_CGDB = {
    Keys.Escape: kc.ESC,
    Keys.ShiftEscape: kc.ESC,
    Keys.Up: kc.UP,
    Keys.Down: kc.DOWN,
    Keys.Left: kc.LEFT,
    Keys.Right: kc.RIGHT,
    Keys.Home: kc.HOME,
    Keys.End: kc.END,
    Keys.PageUp: kc.PPAGE,
    Keys.PageDown: kc.NPAGE,
    Keys.Delete: kc.DC,
    Keys.Insert: kc.IC,
    Keys.Backspace: kc.CTRL_H,
    Keys.Tab: kc.CTRL_I,
    Keys.BackTab: kc.CTRL_I,
    Keys.Enter: kc.CTRL_M,
    Keys.ControlSpace: 0,
    Keys.ControlA: kc.CTRL_A, Keys.ControlB: kc.CTRL_B,
    Keys.ControlC: kc.CTRL_C, Keys.ControlD: kc.CTRL_D,
    Keys.ControlE: kc.CTRL_E, Keys.ControlF: kc.CTRL_F,
    Keys.ControlG: kc.CTRL_G, Keys.ControlH: kc.CTRL_H,
    Keys.ControlI: kc.CTRL_I, Keys.ControlJ: kc.CTRL_J,
    Keys.ControlK: kc.CTRL_K, Keys.ControlL: kc.CTRL_L,
    Keys.ControlM: kc.CTRL_M, Keys.ControlN: kc.CTRL_N,
    Keys.ControlO: kc.CTRL_O, Keys.ControlP: kc.CTRL_P,
    Keys.ControlQ: kc.CTRL_Q, Keys.ControlR: kc.CTRL_R,
    Keys.ControlS: kc.CTRL_S, Keys.ControlT: kc.CTRL_T,
    Keys.ControlU: kc.CTRL_U, Keys.ControlV: kc.CTRL_V,
    Keys.ControlW: kc.CTRL_W, Keys.ControlX: kc.CTRL_X,
    Keys.ControlY: kc.CTRL_Y, Keys.ControlZ: kc.CTRL_Z,
    Keys.ControlLeft: kc.LEFT, Keys.ControlRight: kc.RIGHT,
    Keys.ControlUp: kc.UP, Keys.ControlDown: kc.DOWN,
    Keys.ShiftLeft: kc.LEFT, Keys.ShiftRight: kc.RIGHT,
    Keys.ShiftUp: kc.UP, Keys.ShiftDown: kc.DOWN,
}
for _i in range(1, 13):
    _ptk = getattr(Keys, "F%d" % _i, None)
    if _ptk is not None:
        _PTK_TO_CGDB[_ptk] = getattr(kc, "F%d" % _i)


def translate_key(key_press):
    """prompt_toolkit KeyPress -> cgdb_key int, or None if untranslatable."""
    k = key_press.key
    if k in _PTK_TO_CGDB:
        return _PTK_TO_CGDB[k]
    data = key_press.data
    if isinstance(data, str) and len(data) == 1:
        o = ord(data)
        if o < 256:
            return o
    return None


# Set CGDBPY_KEYLOG=<path> to append one line per key that reaches the
# handler. Diagnosing why a key did nothing in a full-screen TUI is otherwise
# guesswork: the binding may not have matched, the filter may have excluded it,
# or prompt_toolkit may still be waiting for a longer sequence.
_KEYLOG_PATH = os.environ.get("CGDBPY_KEYLOG")


def _debug_key(key_press, key):
    if not _KEYLOG_PATH:
        return
    try:
        import time as _t
        with open(_KEYLOG_PATH, "a", encoding="utf-8") as f:
            f.write("%.6f key=%r data=%r -> %r\n"
                    % (_t.time(), key_press.key, key_press.data, key))
    except OSError:
        pass


class CgdbApp:
    def __init__(self, gdb_path=None, gdb_args=(), config=None,
                 source_path=None, client_factory=None, tty_factory=None,
                 output=None, rc_lines=None):
        self.config = config or Config()
        # (lineno, command) pairs from ~/.cgdb/cgdbrc. C's rc file is a full
        # command language (command_parse_file feeds every line to the `:`
        # parser), so the lines are replayed through _do_colon once gdb is
        # up -- see run_rc_commands().
        self.rc_lines = list(rc_lines or ())
        # cgdb starts in the GDB window (interface.cpp:139,
        # `static enum Focus focus = GDB`), so the user can type gdb commands
        # immediately without pressing `i` first.
        self.focus = FOCUS_GDB
        self.last_key = 0
        # digits typed in the source window accumulate here as a line
        # number, consumed by j/k/G (G_line_number, interface.cpp:1312-1317)
        self._line_number = ""
        # a lone 'g' in GDB scroll mode is pending its second 'g'
        self._gdb_g = False
        self.message = ""
        self.running_inferior = False
        self.gdb_alive = False
        # set once start_gdb() completes; gates the "follow gdb down" exit so a
        # gdb that dies during startup leaves its error on screen
        self._gdb_ready = False
        self._quitting = False

        # --- console ------------------------------------------------------
        self.console = ConsoleModel(width=80,
                                    scrollback=self.config.get(
                                        "scrollbackbuffersize"))
        self.scroll = ScrollState()
        # the last console search: compiled pattern, the row/col of the last
        # hit and the direction it ran in, repeated by n/N in scroll mode
        # (scr_search_next + regex_direction_last)
        self._console_search = None
        self._console_search_row = None
        self._console_search_col = 0
        self._console_search_dir = True
        self._console_search_saved = None
        # where a `/` typed at the command line targets: the source window
        # or the console, decided by which window had focus
        self._search_target = "source"
        # the syntax option value already applied to the current file, so
        # _apply_config can tell a change from an echo
        self._applied_syntax = None

        # --- source -------------------------------------------------------
        self.src = None
        self.view = SourceView(height=20)
        # (row, x) of the selected line's cursor cell, written by
        # _source_text for _source_cursor_pos
        self._cursor_cell = None
        self.breakpoints = Breakpoints()
        self.exec_line = None       # (path, 1-based line) gdb is stopped at

        # --- split layout -------------------------------------------------
        # Mirrors interface.cpp: split_orientation is WSO_HORIZONTAL/WSO_
        # VERTICAL, window_shift moves the split point along the orientation
        # axis (rows in horizontal mode, columns in vertical mode), and
        # win_split is the quarter mark it last sat on (None = off-mark).
        self.split_orientation = str(self.config.get("winsplitorientation"))
        if self.split_orientation not in (WSO_HORIZONTAL, WSO_VERTICAL):
            self.split_orientation = WSO_HORIZONTAL
        self.win_split = _SPLIT_MARKS.get(str(self.config.get("winsplit")))
        # None until the first _apply_split(): the quarter mark has to be
        # resolved against the real terminal size, which only exists once
        # the output object is wired up.
        self.window_shift = None

        # --- file dialog ---------------------------------------------------
        # The `o` dialog replaces the whole screen while open, exactly as
        # if_draw()'s FILE_DLG branch does; _filedlg_active tracks whether
        # the *current layout tree* is the dialog (focus alone is not
        # enough, because the tree is only rebuilt on shape changes).
        self.filedlg = FileDialog()
        self._filedlg_active = False
        self._filedlg_g = False
        # the :help / F1 full-screen reference page (q closes it)
        self._help_active = False

        # --- gdb ----------------------------------------------------------
        self.tty = None
        self.gdb = None
        self.gdb_path = gdb_path
        self.gdb_args = list(gdb_args)
        self._pending_file_line = None
        # injection points so the UI can be tested without a real gdb
        self._client_factory = client_factory
        self._tty_factory = tty_factory
        # an explicit prompt_toolkit Output; None means auto-detect the
        # console. Tests inject DummyOutput because there is no screen buffer.
        self._output = output

        # --- editing ------------------------------------------------------
        self.gdb_buffer = Buffer(
            multiline=False,
            history=InMemoryHistory(),
            accept_handler=self._on_gdb_accept,
            complete_while_typing=False,
        )
        self.cmd_buffer = Buffer(multiline=False,
                                 history=InMemoryHistory(),
                                 accept_handler=self._on_cmd_accept,
                                 on_text_changed=self._on_cmd_text_changed)
        self.cmd_prefix = ":"
        self.search_forward = True
        self.search_origin = None

        # --- keys ---------------------------------------------------------
        self._key_queue = []
        self.map_set = kui.MapSet()
        self.imap_set = kui.MapSet()
        # focus==FILE_DLG / COMMAND clears the active map set in C
        # (cgdb.cpp:427-432); this stand-in is what "clear" points at
        self._empty_set = kui.MapSet()
        self.mapctx = kui.KeyContext(self.map_set, self._read_queued_key,
                                     timeout=self.config.get("timeoutlen"),
                                     name="normal", defer_on_empty=True)
        self._timeout_handle = None
        self._lock = threading.Lock()

        self.app = None
        self._build_ui()
        if source_path:
            self.open_file(source_path)

    # =====================================================================
    # UI construction
    # =====================================================================
    def _console_output(self):
        """Build the prompt_toolkit output at the console's *true* width.

        Win32Output.get_size() reports one column less than the console: it
        computes the visible width as `srWindow.Right - srWindow.Left` -- a
        0-based closed interval, so that is one short -- and then caps it at
        `dwSize.X - 1` on top ("We avoid the right margin, windows will wrap
        otherwise", a guard from the era of writing through the GDI console
        API). For this full-screen app the shortfall is a column of dead space
        on the right edge of every pane: the status bar/separator visibly
        stops one cell short, where cgdb paints the whole row (interface.cpp
        writes WIDTH spaces under HLG_STATUS_BAR). Rendering runs with
        autowrap disabled for the whole lifetime of a full-screen Application
        (the renderer turns it off on the first frame and only restores it
        when the app is done), so painting the last column cannot scroll.
        """
        if self._output is not None:
            return self._output
        from prompt_toolkit.output import create_output
        out = create_output()
        if sys.platform != "win32":
            return out
        try:
            from prompt_toolkit.output.windows10 import Windows10_Output
        except ImportError:
            return out
        if not isinstance(out, Windows10_Output):
            return out
        win = out.win32_output

        def full_width_get_size():
            info = win.get_win32_screen_buffer_info()
            columns = min(info.dwSize.X, info.srWindow.Right -
                          info.srWindow.Left + 1)
            return Size(rows=info.srWindow.Bottom - info.srWindow.Top + 1,
                        columns=columns)

        win.get_size = full_width_get_size
        # Vt100_Output.hide_cursor skips the ?25l write when it believes
        # the cursor is already hidden -- but a terminal resize can make
        # ConPTY restore the cursor behind our back, and the app would
        # never re-hide it. Force every hide to actually go out; the
        # renderer issues one at the start of each frame, so the state
        # is re-asserted on every repaint.
        vt = out.vt100_output
        if hasattr(vt, "_cursor_visible"):
            _orig_hide = vt.hide_cursor

            def _always_hiding_hide_cursor():
                vt._cursor_visible = None
                return _orig_hide()

            vt.hide_cursor = _always_hiding_hide_cursor
        return out

    def _build_ui(self):
        # focusable=True on the source pane: without it Layout.focus() refuses
        # the window and ESC silently fails to leave the GDB pane. The cursor
        # sits at the end of the selected line while this pane is focused,
        # like swin_curs_set(!!focus) + the final wrefresh in source_display.
        self.source_control = FormattedTextControl(
            self._source_text, focusable=True, show_cursor=True,
            get_cursor_position=self._source_cursor_pos)
        self.status_control = FormattedTextControl(self._status_text)
        self.console_control = FormattedTextControl(
            self._console_text, focusable=True, show_cursor=True,
            get_cursor_position=self._console_cursor_pos)
        self.cmdline_control = BufferControl(buffer=self.cmd_buffer)
        self.filedlg_control = FormattedTextControl(self._filedlg_text,
                                                    focusable=True)

        self.source_window = Window(self.source_control, wrap_lines=False,
                                    # Only the cursor bar shows while this
                                    # pane is focused: the hardware cursor
                                    # next to the bar reads as a second
                                    # cursor (user preference; C keeps
                                    # curs_set(1) there). The filter is
                                    # evaluated per render, so ESC/i toggle
                                    # it with the focus.
                                    always_hide_cursor=Condition(
                                        lambda: self.focus == FOCUS_SOURCE),
                                    height=D(weight=1, preferred=0.6))
        # Window(style=...) is prepended to every cell of the window including
        # the blank ones past the end of the text, which is what paints the bar
        # all the way to the right edge. cgdb gets the same effect by writing
        # WIDTH spaces under HLG_STATUS_BAR before the text
        # (interface.cpp:446-451).
        self.status_window = Window(self.status_control, height=1,
                                    style="class:status")
        self.console_window = Window(self.console_control, wrap_lines=False,
                                     height=D(weight=1, preferred=0.4))
        self.edit_window = Window(
            BufferControl(
                buffer=self.gdb_buffer,
                input_processors=[BeforeInput(
                    lambda: [("class:console.prompt", GDB_PROMPT)])]),
            height=1, wrap_lines=False)
        self.cmdline_window = Window(self.cmdline_control, height=1,
                                     wrap_lines=False, style="class:status")
        # the `o` dialog: full screen while open (if_draw's FILE_DLG branch)
        self.filedlg_window = Window(self.filedlg_control, wrap_lines=False,
                                     always_hide_cursor=True)
        self.help_window = Window(
            FormattedTextControl(self._help_text, focusable=True),
            wrap_lines=False, always_hide_cursor=True)

        # The `:` / `/` command line *is* the status bar in cgdb: it replaces
        # the status row, not the bottom edit line. update_status_win() draws
        # cur_sbc into status_win (interface.cpp:459-472). Using
        # ConditionalContainer rather than swapping container children keeps
        # prompt_toolkit's layout cache consistent.
        self.kb = self._build_keybindings()
        self.style = build_style(bool(self.config.get("color")))

        self.app = Application(
            layout=Layout(self._build_root(),
                          focused_element=self.edit_window),
            key_bindings=self.kb,
            full_screen=True,
            style=self.style,
            mouse_support=False,
            erase_when_done=False,
            output=self._console_output(),
        )
        self._apply_ttimeoutlen()
        self._apply_split()

    def _build_root(self):
        """The container tree for the current split orientation.

        Horizontal (interface.cpp:1-11): source / status / gdb stacked.
        Vertical (interface.cpp:17-31): source and the status bar stack in
        the left column, a one-column '|' separator, and the gdb window
        fills the right column -- the status bar is the bottom row of the
        left column, not of the screen. The file dialog replaces the whole
        screen (if_draw draws only filedlg_display when focus is FILE_DLG).
        """
        if self._filedlg_active:
            self.left_col = None
            self.vsep_window = None
            self.root = self.filedlg_window
            return self.root
        if self._help_active:
            self.left_col = None
            self.vsep_window = None
            self.root = self.help_window
            return self.root
        in_command = Condition(lambda: self.focus == FOCUS_COMMAND)
        status = ConditionalContainer(self.status_window, filter=~in_command)
        cmdline = ConditionalContainer(self.cmdline_window, filter=in_command)
        if self.split_orientation == WSO_VERTICAL:
            self.left_col = HSplit([self.source_window, status, cmdline])
            # SWIN_SYM_VLINE column, drawn full height by separator_display
            # (interface.cpp:412-434). Window(char=) fills the whole pane.
            self.vsep_window = Window(width=D(min=1, max=1), char="|")
            right = HSplit([self.console_window, self.edit_window])
            self.root = VSplit([self.left_col, self.vsep_window, right])
        else:
            self.left_col = None
            self.vsep_window = None
            self.root = HSplit([self.source_window, status, cmdline,
                                self.console_window, self.edit_window])
        return self.root

    def _apply_ttimeoutlen(self):
        """Wire cgdb's timeout options onto prompt_toolkit's two waits.

        There are two of them, and both matter:

          * ``app.ttimeoutlen`` -- the vt100/win32 input parser holding a
            lone ESC byte until it knows no escape sequence follows.
          * ``app.timeoutlen`` -- the KeyProcessor flushing an incomplete
            key *binding* prefix, e.g. ESC waiting to see whether b follows
            (``escape,b`` = Alt-b). That is vim's keycode timeout, so it
            takes ttimeoutlen when `ttimeout` is on, else timeoutlen when
            `timeout` is on (cgdbrc.cpp:1198-1220); both off means "wait
            forever", which ptk spells None.

        prompt_toolkit's defaults are 0.5 s / 1.0 s; with ESC being the
        focus-switch key those values feel broken. cgdb's own default is
        100 ms, and that is what we use.
        """
        try:
            self.app.ttimeoutlen = self.config.get("ttimeoutlen") / 1000.0
            ttm = self.config.get("ttimeoutlen")
            if ttm < 0:
                ttm = self.config.get("timeoutlen")
            if bool(self.config.get("ttimeout")):
                self.app.timeoutlen = ttm / 1000.0
            elif bool(self.config.get("timeout")):
                self.app.timeoutlen = \
                    self.config.get("timeoutlen") / 1000.0
            else:
                self.app.timeoutlen = None
        except Exception:
            pass

    def _mode_key_is(self, name):
        return str(self.config.get("cgdbmodekey")).strip().lower() == name

    def _build_keybindings(self):
        """Register bindings.

        Precedence design: while the GDB edit line has focus and we are not
        in scroll mode, ordinary typing and cursor editing belong to
        prompt_toolkit's Buffer (that is where the free line editor comes
        from). The catch-all is therefore filtered off in that state, and
        only the keys cgdb must own -- ESC, PageUp/PageDown, the F-keys,
        Ctrl-C -- stay bound unconditionally.

        Escape is deliberately NOT eager. A bare ESC must wait out the
        keycode timeout (ttimeoutlen) so that ESC followed by b/f/d/
        Backspace within the window is Alt-b/f/d/Backspace -- readline word
        motions, exactly as C's kui resolves "\033b" to BACKWARD_WORD.
        prompt_toolkit's KeyProcessor holds the prefix and flushes on
        `app.timeoutlen`, which _apply_ttimeoutlen keeps in sync with
        ttimeoutlen. When the flush comes, our own escape binding (an
        application-level binding outranks the default emacs one in
        prompt_toolkit's merge order) runs the cgdb_mode_key switch.
        """
        kb = KeyBindings()
        editing = Condition(self._buffer_owns_keys)

        @kb.add(Keys.Any, filter=~editing)
        def _any(event):
            self.on_key(event.key_sequence[-1])

        # keys cgdb owns in every focus. Ctrl-L is deliberately NOT here:
        # gdb_input passes it to readline (result = 1), and only the source
        # window relayouts -- the ~editing catch-all routes it there.
        for key in (Keys.PageUp, Keys.PageDown,
                    Keys.ControlC, Keys.F1, Keys.F2, Keys.F3, Keys.F4,
                    Keys.F5, Keys.F6, Keys.F7, Keys.F8, Keys.F9, Keys.F10,
                    Keys.F11, Keys.F12):
            self._bind(kb, key)

        self._bind(kb, Keys.Escape)

        # ESC + b/f/d/Backspace == Alt-b/f/d/Backspace == the readline word
        # operations (kui_term.cpp:130-133 + cgdb.cpp:1040-1052). Registered
        # at application level so they win over ptk's own defaults and fire
        # only while a Buffer owns the keys, i.e. while a line is edited.
        from prompt_toolkit.key_binding.bindings.named_commands import \
            get_by_name as _nc
        for seq, name in ((("escape", "b"), "backward-word"),
                          (("escape", "f"), "forward-word"),
                          (("escape", "d"), "kill-word"),
                          (("escape", Keys.Backspace),
                           "backward-kill-word")):
            @kb.add(*seq, filter=editing)
            def _word_op(event, name=name):
                _nc(name)(event)

        # C-d on an empty GDB edit line: readline reads EOF and gdb exits,
        # taking cgdb with it (kui_term CTRL_D -> "\004" -> tgdb_send_char).
        @kb.add(Keys.ControlD,
                filter=Condition(lambda: self.focus == FOCUS_GDB
                                 and not self.scroll.active
                                 and not self.gdb_buffer.text))
        def _ctrl_d_exit(event):
            self._run_async("-gdb-exit")

        # when cgdbmodekey is reconfigured to Tab / Ctrl-G those keys must
        # reach on_key() instead of the Buffer's own bindings; only then is
        # the binding active at all
        for ptk_key, mode_key in ((Keys.Tab, "tab"),
                                  (Keys.ControlG, "ctrl-g")):
            @kb.add(ptk_key, filter=Condition(
                lambda mk=mode_key: self._mode_key_is(mk)))
            def _mode_key_binding(event):
                self.on_key(event.key_sequence[-1])

        # in GDB focus the arrow keys must reach the Buffer (history), so they
        # are only ours when scroll mode is on
        scrolling = Condition(lambda: self.scroll.active)
        for key in (Keys.Up, Keys.Down, Keys.Left, Keys.Right, Keys.Home,
                    Keys.End):
            @kb.add(key, filter=editing & scrolling)
            def _scroll_key(event, key=key):
                self.on_key(event.key_sequence[-1])
        return kb

    def _bind(self, kb, key, eager=False):
        @kb.add(key, eager=eager)
        def _handler(event, key=key):
            self.on_key(event.key_sequence[-1])

    def _buffer_owns_keys(self):
        """True when a prompt_toolkit Buffer should handle keys, not us.

        Both the GDB edit line and the `:` command line are Buffers. Letting
        prompt_toolkit own ordinary typing there is what gives us the line
        editor and the command-line history for free; intercepting those keys
        in the catch-all instead would silently swallow every character.
        """
        if self.focus == FOCUS_COMMAND:
            return True
        return self.focus == FOCUS_GDB and not self.scroll.active

    # =====================================================================
    # Key handling
    # =====================================================================
    def _read_queued_key(self):
        with self._lock:
            return self._key_queue.pop(0) if self._key_queue else None

    def on_key(self, key_press):
        key = translate_key(key_press)
        _debug_key(key_press, key)
        if key is None:
            return
        if key == kc.CTRL_C:
            # ^C with gdb idle aborts the line being composed: readline
            # clears it and reprints the prompt (tgdb writes VINTR). The
            # local edit line is our readline, so clear it here too.
            if not self.running_inferior and self.focus == FOCUS_GDB \
                    and self.gdb_buffer.text:
                self.gdb_buffer.reset()
            self.interrupt()
            self.invalidate()
            return
        with self._lock:
            self._key_queue.append(key)
        self._drain_keys()

    def _drain_keys(self):
        while True:
            k = self.mapctx.getkey()
            if k is kui.PENDING:
                self._arm_map_timeout()
                return
            if k is None:
                return
            self.dispatch(k)
            self.invalidate()

    def _arm_map_timeout(self):
        if self._timeout_handle is not None:
            return
        if not bool(self.config.get("timeout")):
            # :set notimeout: a half-typed mapping waits forever, exactly
            # as C's kui does (cgdbrc.cpp:1213-1220)
            return
        delay = self.config.get("timeoutlen") / 1000.0
        try:
            loop = self.app.loop if hasattr(self.app, "loop") else None
            import asyncio
            loop = loop or asyncio.get_event_loop()
            self._timeout_handle = loop.call_later(delay, self._map_timeout)
        except Exception:
            # no loop available (headless tests): resolve immediately
            self._timeout_handle = None
            self._map_timeout()

    def _map_timeout(self):
        self._timeout_handle = None
        k = self.mapctx.flush()
        if k is not None and k is not kui.PENDING:
            self.dispatch(k)
            self._drain_keys()
            self.invalidate()

    def dispatch(self, key):
        """Route one mapped key. Mirrors internal_if_input().

        Every key becomes last_key_pressed when it is done being processed
        (interface.cpp:1594-1601); handlers that consume an m/g/' prefix
        zero it. A pending status message is consumed at the same point --
        if_display_message output only survives until the next draw in C.
        """
        self.message = ""
        if self._help_active:
            if key == ord("q") or key == kc.ESC:
                self._close_help()
            return
        mode_key = self._cgdb_mode_key()
        if key == mode_key:
            if self.focus != FOCUS_SOURCE:
                if self.focus == FOCUS_COMMAND:
                    self._cancel_command()
                # leaving the GDB window *keeps* scroll mode and its
                # position, as in C (interface.cpp:1522-1555 never touches
                # in_scroll_mode); the scroll cursor and the [d/max] status
                # make the parked state visible
                self.set_focus(FOCUS_SOURCE)
            self.last_key = key
            return
        if self.focus == FOCUS_FILEDLG:
            # internal_if_input routes FILE_DLG straight to the dialog,
            # before any global key (interface.cpp:1586-1606)
            self._filedialog_key(key)
            self.last_key = key
            return
        if self.focus == FOCUS_COMMAND:
            self._command_key(key)
            self.last_key = key
            return
        if self.focus == FOCUS_SOURCE:
            self.source_key(key)
        elif self.focus == FOCUS_GDB:
            self.gdb_key(key)
        if self.last_key != 0:
            self.last_key = key

    def _cgdb_mode_key(self):
        """The key that leaves the GDB window (cgdbrc's cgdbmodekey).

        Accepts a single character, the three historical spellings, and any
        ``<X>`` keycode notation kui knows -- C takes anything
        kui_term_get_cgdb_key_from_keycode resolves (cgdbrc.cpp:437-461).
        """
        name = str(self.config.get("cgdbmodekey")).strip()
        if not name:
            return kc.ESC
        if len(name) == 1 and not name.startswith("<"):
            return ord(name.lower()) if name.isalpha() else ord(name)
        low = name.lower()
        if low in ("esc", "<esc>", "escape"):
            return kc.ESC
        if low in ("tab", "<tab>", "<c-i>"):
            return kc.CTRL_I
        if low in ("ctrl-g", "<c-g>", "^g"):
            return kc.CTRL_G
        try:
            keys = kc.string_to_keys(name)
            if keys:
                return keys[0]
        except Exception:
            pass
        return kc.ESC

    # =====================================================================
    # Source window keys (cgdb_input + source_input)
    # =====================================================================
    def source_key(self, key):
        # cgdb_input's early returns (i, s, :, /, ?, o, F-keys, ESC) happen
        # BEFORE source_input, so the digit count survives them; it is only
        # source_input itself that clears or extends it
        # (interface.cpp:1298-1303 vs 1312-1317).
        self._source_key(key)

    def _source_key(self, key):
        total = len(self.src) if self.src else 0
        # mark handling: m<char> and '<char>. cgdb does this with
        # last_key_pressed (interface.cpp:1425-1444), and so do we -- a
        # two-key sequence must not be resolvable by the mapping engine alone,
        # or `:map gg <Up>` could never fire. C only accepts a-zA-Z as a mark
        # name; an unset mark or a non-mark char falls back to normal key
        # processing (sources.cpp:641-656, 694-703).
        if self.last_key == ord("m"):
            if not kc.is_special(key) and chr(key).isalpha():
                self.view.set_mark(chr(key))
                self.last_key = 0
                self._line_number = ""
                return
            # not a mark name: the key falls through as itself
        elif self.last_key == ord("'"):
            ch = chr(key) if not kc.is_special(key) else ""
            if ch and (ch.isalnum() or ch in ("'", ".")):
                if self._goto_mark_key(ch, total):
                    self.last_key = 0
                    self._line_number = ""
                    return
            # unset mark / non-mark char: falls through as itself
        if self.last_key == ord("g") and key == ord("g"):
            self.view.move_to(0, total)
            self.last_key = 0
            self._line_number = ""
            return

        if key == ord("i"):
            self.set_focus(FOCUS_GDB)
            return
        if key == ord("s"):
            self.scroll.enter(self.console.max_delta(self._console_height()),
                              row=self._console_cursor_row())
            self.set_focus(FOCUS_GDB)
            return
        if key == ord(":"):
            self._start_command(":")
            return
        if key in (ord("/"), ord("?")):
            self.search_forward = (key == ord("/"))
            self.search_origin = self.view.cursor
            self._search_target = "source"
            self._start_command("/" if self.search_forward else "?")
            return
        if key == ord("n"):
            self._repeat_search(self.search_forward, total)
            return
        if key == ord("N"):
            self._repeat_search(not self.search_forward, total)
            return
        if key == ord(" "):
            self.toggle_breakpoint()
            return
        if key == ord("t"):
            self.toggle_breakpoint(temporary=True)
            return
        if key == ord("o"):
            # tgdb_request_inferiors_source_files: the file dialog opens
            # once gdb answers with the debuggee's source list
            self._open_filedlg()
            return
        if key == ord("u"):
            # run until the cursor line (interface.cpp:1304-1311); C passes
            # the *displayed* file's path too, so the right file:line runs
            if self.src:
                self.gdb_until(self.src.path, self.view.cursor + 1)
            else:
                self.message = "no source file"
            return
        if key == kc.CTRL_W:
            self._toggle_orientation()
            return
        if key == ord("="):
            self._window_resize(+1)
            return
        if key == ord("-"):
            self._window_resize(-1)
            return
        if key == ord("+"):
            self._window_resize_jump(+1)
            return
        if key == ord("_"):
            self._window_resize_jump(-1)
            return
        # the F-keys live in cgdb_input, so they only fire from the source
        # window (interface.cpp:1484-1509); the GDB window sends them to gdb
        if key == kc.F1:
            self.show_help()
            return
        if key == kc.F5:
            self.gdb_run()
            return
        if key == kc.F6:
            self.gdb_continue()
            return
        if key == kc.F7:
            self.gdb_finish()
            return
        if key == kc.F8:
            self.gdb_next()
            return
        if key == kc.F10:
            self.gdb_step()
            return
        if key == kc.CTRL_L:
            self.relayout()
            return
        if key == ord("m"):
            self.last_key = ord("m")
            return
        if key == ord("'"):
            self.last_key = ord("'")
            return

        # source_input proper starts here. C reads G_line_number as the
        # PREVIOUS keys left it and only updates it after the switch
        # (interface.cpp:1298-1303), so `5` then `j` moves five lines.
        try:
            count = int(self._line_number) if self._line_number else 1
        except ValueError:
            count = 1
        if count > 0x7FFFFFFF:
            count = 1     # C's atoi overflow leaves the count at 1
        h = self._source_height()
        if key in (ord("h"), kc.LEFT):
            self.view.hscroll(-1, self._max_source_left())
        elif key in (ord("l"), kc.RIGHT):
            self.view.hscroll(+1, self._max_source_left())
        elif key in (ord("j"), kc.DOWN):
            self.view.move_to(self.view.cursor + count, total)
        elif key in (ord("k"), kc.UP):
            self.view.move_to(self.view.cursor - count, total)
        elif key == kc.CTRL_D:
            self.view.move_to(self.view.cursor + max(1, h // 2), total)
        elif key == kc.CTRL_U:
            self.view.move_to(self.view.cursor - max(1, h // 2), total)
        elif key == kc.CTRL_F or key == kc.NPAGE:
            self.view.move_to(self.view.cursor + h - 1, total)
        elif key == kc.CTRL_B or key == kc.PPAGE:
            self.view.move_to(self.view.cursor - h + 1, total)
        elif key == ord("G"):
            # with a count, G goes to that line; without, the last line
            if self._line_number:
                self.view.move_to(int(self._line_number) - 1, total)
            else:
                self.view.move_to(total - 1, total)
        elif key == ord("g"):
            # prefix of `gg`; resolved above on the next key. Anything else
            # after `g` falls through as a no-op, matching vim.
            self.last_key = ord("g")
        else:
            self.last_key = key
        # motion. j/k and the arrows honour the digit count, like
        # source_vscroll(sview, lineno) does in C. h/l scroll the text
        # columns (source_hscroll, interface.cpp:1243-1247); pages move
        # height-1 rows so one line of context overlaps, as C does.
        # Store digits into G_line_number for the 'G' command, after the
        # motion has consumed whatever the previous keys accumulated.
        if 0x30 <= key <= 0x39:
            self._line_number += chr(key)
        else:
            self._line_number = ""

    def _goto_mark_key(self, ch, total):
        """`'x`, `''` (jump back) and `'.` (the executing line).

        sources.cpp:684-696: a successful `'x` jump records where it jumped
        FROM so `''` bounces back, and `'.` goes to the current executing
        line. Returns False when the mark does not exist, so the caller can
        let the key fall through to normal processing like C does.
        """
        if ch == "'":
            ln = self.view.jump_back
            if ln is None:
                return False
        elif ch == ".":
            if self.exec_line and self.src and self._same_file(
                    self.exec_line[0], self.src.path):
                ln = self.exec_line[1] - 1
            else:
                return False
        else:
            ln = self.view.goto_mark(ch)
            if ln is None:
                return False
        origin = self.view.cursor
        self.view.move_to(ln, total)
        if ch not in ("'", "."):
            self.view.jump_back = origin
        return True

    def _max_source_left(self):
        """The sel_col clamp: sources.cpp:1091-1110 stops the horizontal
        scroll at `max_width - width + lwidth + 6` so the view cannot roll
        off into blank columns."""
        if self.src is None or not len(self.src):
            return 0
        lwidth = len(str(len(self.src)))
        avail = max(0, self._source_width() - lwidth - 2)
        tabstop = int(self.config.get("tabstop"))
        return max(0, self.src.display_max_width(tabstop) - avail + 6)

    def _repeat_search(self, forward, total):
        if not self.src or not self.view.search_pattern:
            self.message = "no previous search pattern"
            return
        hit = self.view.search_in(self.src, forward=forward,
                                  ignorecase=bool(self.config.get("ignorecase")))
        if hit is None:
            self.message = "pattern not found"

    # =====================================================================
    # GDB window keys
    # =====================================================================
    def _console_cursor_row(self):
        """The row the terminal cursor sits on: the current output line."""
        h = self._console_height()
        return max(0, min(self.console.line_count() - 1, h - 1))

    def gdb_key(self, key):
        h = self._console_height()
        mx = self.console.max_delta(h)
        # a lone 'g' is pending its second 'g' (gdb_input's gg, which jumps
        # home only on the pair -- unlike the source window's prefix logic)
        g_pending = self._gdb_g
        self._gdb_g = False
        if self.scroll.active:
            w = self._gdb_width()
            if key == ord("g"):
                if g_pending:
                    self.scroll.home(mx, h)
                else:
                    self._gdb_g = True
            elif key in (kc.UP, ord("k"), kc.CTRL_P):
                # the cursor walks first; the window only scrolls once the
                # cursor hits the edge (scr_up/scr_down, scroller.cpp:216-237)
                self.scroll.up(1, mx, h)
            elif key in (kc.DOWN, ord("j"), kc.CTRL_N):
                self.scroll.down(1, mx, h)
            elif key == kc.CTRL_U:
                self.scroll.up(max(1, h // 2), mx, h)
            elif key == kc.CTRL_D:
                self.scroll.down(max(1, h // 2), mx, h)
            elif key == kc.PPAGE:
                self.scroll.page_up(mx, h)
            elif key == kc.NPAGE:
                self.scroll.page_down(mx, h)
            elif key in (kc.HOME, kc.F11):
                self.scroll.home(mx, h)
            elif key in (kc.END, kc.F12, ord("G")):
                self.scroll.end(mx, h)
            elif key in (kc.LEFT, ord("h")):
                # h/l move the *scroll cursor's* column; the window never
                # pans sideways (scr_left/scr_right, scroller.cpp:254-284)
                self.scroll.left(w)
            elif key in (kc.RIGHT, ord("l")):
                self.scroll.right(w)
            elif key == ord("0"):
                self.scroll.beginning_of_row()
            elif key == ord("$"):
                self.scroll.end_of_row(w)
            elif key in (ord("/"), ord("?")):
                # console search: the pattern is typed at the same status
                # command line a source search uses, then n/N repeat it
                self._search_target = "console"
                self.search_forward = (key == ord("/"))
                self._console_search_saved = (self.scroll.active,
                                              self.scroll.delta,
                                              self.scroll.row)
                self._start_command("/" if self.search_forward else "?")
            elif key == ord("n"):
                self._console_search_next(self._console_search_dir)
            elif key == ord("N"):
                self._console_search_next(not self._console_search_dir)
            elif key in (ord("q"), ord("i"), kc.CTRL_M):
                # q/i/Enter all leave scroll mode parked at the bottom
                # (gdb_input: scr_end + set_scroll_mode(false))
                self.scroll.leave()
                self.set_focus(FOCUS_GDB)
            # everything else in scroll mode is swallowed, as C does by
            # returning result = 0 (no key reaches gdb from here).
            return
        if key == kc.PPAGE:
            self.scroll.enter(mx, row=self._console_cursor_row())
            self.set_focus(FOCUS_GDB)
            self.scroll.page_up(mx, h)
            return
        if key == kc.ESC:
            self.set_focus(FOCUS_SOURCE)
            return
        if self.running_inferior:
            # POSIX: the inferior owns the pty, keys pass through
            # byte-wise, exactly as C writes each key into the console
            # pty (gdb_input's default -> result=1 -> tgdb_send_char).
            # Windows: the inferior owns its own console window; there
            # is nothing to forward, so the key is dropped (C does the
            # same).
            self._send_key_to_inferior(key)
            return
        # Keys that reach THIS branch while the edit line has focus can
        # only come from a :map expansion: _buffer_owns_keys() keeps
        # terminal-typed ordinary keys in prompt_toolkit's Buffer, but a
        # mapping's rhs is dispatched key by key by the kui drain. C sends
        # such keys to gdb's readline through the pty; here they have to
        # be typed into the synthesized edit line instead -- that is what
        # makes `:map <f4> iinfo locals<cr><esc>` work: 'i' switches to
        # the GDB window, the text accumulates, <cr> runs the command,
        # <esc> returns to the source window.
        if key == kc.CTRL_M:
            self.gdb_buffer.validate_and_handle()
        elif key == kc.CTRL_H:
            self.gdb_buffer.delete_before_cursor(1)
        elif not kc.is_special(key) and 32 <= key < 127:
            self.gdb_buffer.insert_text(chr(key))
        # other control keys from a mapping rhs are dropped for now; the
        # readline editing defaults stay owned by prompt_toolkit's bindings

    def _send_key_to_inferior(self, key):
        """Byte-wise passthrough while the inferior runs (POSIX pty only).

        C writes each key into the console pty; its line discipline echoes
        the input back. The pty master here has no echo, so printable keys
        and edits are echoed into the console model by hand. On Windows the
        inferior reads from its own console window and write() is a no-op,
        so nothing is echoed either.
        """
        if self.tty is None:
            return
        if key == kc.CTRL_M:
            ch = "\n"
        elif key == kc.CTRL_H:
            ch = "\b"
        elif not kc.is_special(key) and 32 <= key < 127:
            ch = chr(key)
        else:
            return
        if self.tty.write(ch):
            self.console.add_text(ch)
            self.invalidate()

    # =====================================================================
    # File dialog (filedlg.cpp)
    # =====================================================================
    def _open_filedlg(self):
        """`o`: browse the debuggee's source files.

        C defers to tgdb_request_inferiors_source_files and opens the dialog
        when the list arrives; -file-list-exec-source-files is the MI form
        of that request, so the dialog opens from the result callback.
        """
        if not self.gdb or not self.gdb.running:
            self.message = "gdb is not running"
            return
        self.message = "loading source files..."
        self.invalidate()
        self._run_async("-file-list-exec-source-files",
                        on_result=self._on_source_files)

    def _on_source_files(self, rec):
        if rec is None or rec.kind != "result" or rec.cls != "done":
            return
        files = (rec.payload or {}).get("files") or []
        entries = []
        for item in files:
            if isinstance(item, tuple):
                item = item[1]
            if isinstance(item, dict):
                name = item.get("file") or ""
                full = item.get("fullname") or name
                if name:
                    entries.append((name, full))
        if not entries:
            self.message = ("Error: No sources available! "
                            "Was the program compiled with debug?")
            self.invalidate()
            return
        self.filedlg.set_files(entries)
        if not self.filedlg.count():
            # every entry pointed at a file that no longer exists
            self.message = ("Error: No sources available! "
                            "Was the program compiled with debug?")
            self.invalidate()
            return
        self.set_focus(FOCUS_FILEDLG)

    def _filedialog_key(self, key):
        """One key in the dialog (filedlg_recv_char, filedlg.cpp:584-673)."""
        h = max(1, self._total_height() - 2)   # list rows; C reserves 2
        g = self._filedlg_g
        self._filedlg_g = False
        if key == ord("q"):
            self.set_focus(FOCUS_SOURCE)
        elif key in (ord("j"), kc.DOWN, kc.CTRL_N):
            self.filedlg.vscroll(1)
        elif key in (ord("k"), kc.UP, kc.CTRL_P):
            self.filedlg.vscroll(-1)
        elif key in (kc.NPAGE, kc.CTRL_F):
            self.filedlg.vscroll(max(1, h - 1))
        elif key in (kc.PPAGE, kc.CTRL_B):
            self.filedlg.vscroll(-max(1, h - 1))
        elif key == kc.CTRL_D:
            self.filedlg.vscroll(max(1, h // 2))
        elif key == kc.CTRL_U:
            self.filedlg.vscroll(-max(1, h // 2))
        elif key in (ord("l"), kc.RIGHT):
            self.filedlg.left += 1
        elif key in (ord("h"), kc.LEFT):
            self.filedlg.left = max(0, self.filedlg.left - 1)
        elif key in (ord("/"), ord("?")):
            self._search_target = "filedlg"
            self._filedlg_search_origin = self.filedlg.sel
            self.filedlg.message = ""
            self._start_command("/" if key == ord("/") else "?")
        elif key == ord("n"):
            self._filedialog_search_repeat(self.filedlg.search_dir)
        elif key == ord("N"):
            self._filedialog_search_repeat(not self.filedlg.search_dir)
        elif key == kc.CTRL_M:
            # Enter opens the selection (if_show_file); re-opening the
            # current file keeps the cursor where it was, as in C
            name, fullname = self.filedlg.selected()
            self.set_focus(FOCUS_SOURCE)
            self.open_file(fullname or name)
        elif key == ord("g"):
            if g:
                self.filedlg.goto(0)
            else:
                self._filedlg_g = True
        elif key == ord("G"):
            # G jumps to the row given by the digits typed so far; a bare
            # G does nothing -- verbatim C behaviour (filedlg.cpp:651-657)
            if self.filedlg.line_number:
                self.filedlg.goto(int(self.filedlg.line_number) - 1)
        # digits accumulate for G (filedlg.cpp:668-673)
        if 0x30 <= key <= 0x39:
            self.filedlg.line_number += chr(key)
        else:
            self.filedlg.line_number = ""
        self.invalidate()

    def _do_filedlg_search(self, pattern, forward):
        """`/` typed in the dialog (filedlg_search_regex). Matching is
        hard-coded case-insensitive -- C never consults :set ignorecase
        here (filedlg.cpp:548) -- and an empty pattern replays the last
        one in its original direction."""
        self._search_target = "filedlg"
        if not pattern and self.filedlg.search is None:
            self.filedlg.message = "no previous search pattern"
            return
        hit = self.filedlg.find(pattern, forward)
        if hit is None and pattern:
            self.filedlg.message = "pattern not found: %s" % pattern
        else:
            self.filedlg.message = ""

    def _filedialog_search_repeat(self, forward):
        if self.filedlg.search is None:
            self.filedlg.message = "no previous search pattern"
            return
        hit = self.filedlg.find("", forward)
        self.filedlg.message = "" if hit is not None else "pattern not found"

    def _filedlg_text(self):
        """The dialog screen (filedlg_display, filedlg.cpp:329-427).

        The label row, `height - 2` list rows -- C reserves a bottom
        status row -- and that row last, where the dialog's messages live
        in HLG_STATUS_BAR. The selected row is a bold line number plus the
        `->` arrow (filedlg.cpp:404-412), ordinary rows get a bold `|`,
        and `~` fills past the end. A list shorter than the screen is
        block-centered, as C does.
        """
        fd = self.filedlg
        width = self._total_width()
        rows = max(1, self._total_height() - 2)
        count = fd.count()
        if count < rows:
            start = (count - rows) // 2
        else:
            start = max(0, min(fd.sel - rows // 2, count - rows))
        numw = max(1, len(str(max(count, 1))))
        avail = max(1, width - numw - 3)

        out = [("class:filedlg.label",
                LABEL[:width].center(width).rstrip())]
        for r in range(rows):
            i = start + r
            out.append(("", "\n"))
            if not (0 <= i < count):
                out.append(("class:filedlg.filler",
                            " " * (numw - 1) + "~" + " |"))
                continue
            name = fd.files[i][0][fd.left:fd.left + avail]
            num = "%*d" % (numw, i + 1)
            if i == fd.sel:
                out.append(("class:filedlg.selnum", num))
                out.append(("class:filedlg.arrow", "->"))
                out.append(("", name))
            else:
                out.append(("class:filedlg", num))
                out.append(("class:filedlg.selnum", " |"))
                out.append(("", name))
        msg = (fd.message or "")[:width]
        out.append(("", "\n"))
        out.append(("class:status", msg.ljust(width)))
        return out

    # =====================================================================
    # Command line
    # =====================================================================
    def _start_command(self, prefix):
        self.cmd_prefix = prefix
        self.cmd_buffer.text = prefix
        self.cmd_buffer.cursor_position = len(prefix)
        self._prev_focus = self.focus if self.focus != FOCUS_COMMAND \
            else FOCUS_SOURCE
        if prefix in ("/", "?") and self._search_target == "source":
            # orig_line_regex: where the cursor was when the search began
            self.search_origin = self.view.cursor
        self.set_focus(FOCUS_COMMAND)

    def _end_command(self):
        self.set_focus(getattr(self, "_prev_focus", FOCUS_SOURCE))

    def _cancel_command(self):
        """ESC on the command line: cancel and undo the search preview.

        C restores the source cursor to orig_line_regex
        (interface.cpp:1529-1543) and, for a regex typed in the GDB window,
        puts the scroller back where it was (scr_disable_search(false));
        a `:` line just closes. Focus returns to wherever the line was
        opened from, which reproduces both of C's outcomes.
        """
        if self._search_target == "source" and self.search_origin is not None:
            if self.src:
                self.view.move_to(self.search_origin, len(self.src))
            self.search_origin = None
        elif self._search_target == "console":
            saved = self._console_search_saved
            if saved:
                self.scroll.active, self.scroll.delta, self.scroll.row = \
                    saved
                self._console_search_saved = None
        elif self._search_target == "filedlg":
            origin = getattr(self, "_filedlg_search_origin", None)
            if origin is not None:
                self.filedlg.goto(origin)
                self._filedlg_search_origin = None
        self.cmd_buffer.text = ""
        self._end_command()

    def _on_cmd_accept(self, buff):
        """Enter on the `:` / `/` command line."""
        text = buff.text
        buff.reset()
        if self.cmd_prefix and not text.startswith(self.cmd_prefix):
            # the ':'/'/' itself was backspaced away: there is nothing to
            # run. A bare `q` must never leak to gdb as a console command.
            self._end_command()
            return
        self._end_command()
        self.execute_command(text)
        self.invalidate()

    def _on_cmd_text_changed(self, buff):
        """Live search preview: jump while the `/` pattern is being typed.

        status_bar_regex_input / gdb_input_regex_input /
        filedlg_capture_regex jump on every character and redraw; the
        buffer already types the pattern, so hooking text_changed gives the
        same feel. Partial regexes simply fail to compile and leave the
        view where it was. The source window, the console and the file
        dialog all get it.
        """
        text = buff.text
        if self.focus == FOCUS_COMMAND and self.cmd_prefix \
                and len(text) < len(self.cmd_prefix):
            # backspacing over the ':' itself closes the command line, like
            # C's empty-line backspace (interface.cpp:1087-1096) -- the
            # prefix is never part of the editable text there
            self._cancel_command()
            return
        if len(text) < 2 or text[0] not in "/?":
            return
        forward = (text[0] == "/")
        pattern = text[1:]
        if self._search_target == "source" and self.src:
            self.view.search_in(self.src, pattern,
                                forward=forward,
                                ignorecase=bool(self.config.get(
                                    "ignorecase")))
        elif self._search_target == "console":
            try:
                rx = re.compile(pattern, re.IGNORECASE
                                if bool(self.config.get("ignorecase")) else 0)
            except re.error:
                return
            hit = self.console.search(rx, *self._console_search_origin(),
                                      forward=forward,
                                      wrapscan=bool(self.config.get(
                                          "wrapscan")))
            if hit is not None:
                self._goto_console_row(*hit)
        elif self._search_target == "filedlg":
            hit = self.filedlg.find(pattern, forward, live=True)
            if hit is not None:
                self.filedlg.sel = hit

    def _command_key(self, key):
        """Only keys the Buffer does not own reach here (F-keys, Ctrl-L).

        Ordinary typing and Enter belong to cmd_buffer via its accept_handler;
        Escape is handled by the cgdb-mode-key branch in dispatch().
        """
        return

    def execute_command(self, line):
        line = line.strip()
        if not line:
            return
        if line[:1] in ("/", "?"):
            forward = (line[0] == "/")
            if self._search_target == "console":
                self._do_console_search(line[1:], forward)
            elif self._search_target == "filedlg":
                self._do_filedlg_search(line[1:], forward)
            else:
                self._do_search(line[1:], forward)
        elif line.startswith(":"):
            self._do_colon(line[1:].strip())
        else:
            self.send_console_command(line)

    def _do_search(self, pattern, forward):
        self._search_target = "source"
        if not self.src:
            self.message = "no source file"
            return
        if not pattern:
            # `/` + Enter repeats the last pattern in the direction the
            # search line was opened with (interface.cpp:1008-1017)
            self._repeat_search(forward, len(self.src))
            return
        self.search_forward = forward
        hit = self.view.search_in(self.src, pattern, forward=forward,
                                  ignorecase=bool(self.config.get("ignorecase")))
        self.message = "" if hit is not None else "pattern not found: %s" % pattern

    def _do_console_search(self, pattern, forward):
        """Search the GDB window's scrollback, from a `/` typed there.

        C captures the regex inside the scroller (gdb_input_regex_input)
        with the status bar as echo; here the status command line holds
        the pattern and Enter jumps. An empty pattern repeats the last one
        in its original direction (`/` + Enter, interface.cpp:1008-1017);
        the scan starts at the scroll cursor so a second match on the same
        row is reachable, and wraps when `wrapscan` is on. C highlights
        the match with HLG_INCSEARCH while typing -- live jumping is here,
        the highlight remains on the gap list.
        """
        self._search_target = "console"
        wrapscan = bool(self.config.get("wrapscan"))
        if not pattern:
            if self._console_search is None:
                self.message = "no previous search pattern"
                return
            pattern = self._console_search.pattern
            forward = self._console_search_dir
        try:
            rx = re.compile(pattern, re.IGNORECASE
                            if bool(self.config.get("ignorecase")) else 0)
        except re.error as e:
            self.message = "bad pattern: %s" % e
            return
        hit = self.console.search(rx, *self._console_search_origin(),
                                  forward=forward, wrapscan=wrapscan)
        if hit is None:
            self.message = "pattern not found: %s" % pattern
            return
        self._console_search = rx
        self._console_search_dir = forward
        self._console_search_row, self._console_search_col = hit
        self._goto_console_row(*hit)
        self.message = ""

    def _console_search_origin(self):
        """The (row, col) a console search starts scanning from: the scroll
        cursor's absolute position (scroller.cpp:319-329)."""
        h = self._console_height()
        if not self.scroll.active:
            self.scroll.enter(self.console.max_delta(h),
                              row=self._console_cursor_row())
        total = self.console.line_count()
        row = total - self.scroll.delta - h + self.scroll.row
        return (max(0, min(row, total - 1)), self.scroll.col)

    def _console_search_next(self, forward):
        """n/N in the GDB window's scroll mode (scr_search_next)."""
        if self._console_search is None:
            self.message = "no previous search pattern"
            return
        hit = self.console.search(self._console_search,
                                  self._console_search_row,
                                  self._console_search_col,
                                  forward=forward,
                                  wrapscan=bool(self.config.get("wrapscan")))
        if hit is None:
            self.message = "pattern not found"
            return
        self._console_search_row, self._console_search_col = hit
        self._goto_console_row(*hit)
        self.message = ""

    def _goto_console_row(self, row, col=0):
        """Put the hit row under the scroll cursor.

        The hit lands at the top of the window when there is room below;
        when it is inside the last `h` rows the view stays parked at the
        bottom and the cursor moves onto the hit's row instead -- that is
        where the next search continues from, so the cursor must actually
        sit on it."""
        h = self._console_height()
        total = self.console.line_count()
        if not self.scroll.active:
            self.scroll.enter(self.console.max_delta(h),
                              row=self._console_cursor_row())
        row = max(0, min(row, max(0, total - 1)))
        delta = total - h - row
        if delta < 0:
            delta = 0
            self.scroll.row = max(0, min(h - 1, row - (total - h)))
        else:
            self.scroll.delta = min(delta, self.console.max_delta(h))
            self.scroll.row = 0
        self.scroll.delta = delta
        self.scroll.col = min(col, max(0, self._gdb_width() - 1))

    def _do_colon(self, body):
        """Dispatch one `:` command.

        The command set is the C one (commands[] in cgdbrc.cpp:98-141 plus
        the NUMBER grammar of command_parse_string, cgdbrc.cpp:1058-1073);
        `:open` and `:clear` are Python-port extras, and unknown commands
        report an error exactly like C's "Unknown command:" path.
        """
        if not body:
            return
        # command names are case-sensitive in C (the lexer keywords and the
        # bsearch over commands[] both use strcmp); values stay case-blind
        # because every value parser uses strcasecmp
        parts = body.split(None, 1)
        cmd = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("q", "quit"):
            self.quit()
            return
        if cmd == "help":
            self.show_help()
            return
        # NUMBER: `:123` jumps to a line, `:+N` / `:-N` scroll by N
        # (command_parse_string's NUMBER case). The lexer's NUMBER token is
        # `[+-]?{D}+` followed by whatever -- the trailing junk is ignored,
        # and `+` / `-` alone are not numbers.
        m = re.match(r"^([+-]?\d+)", cmd)
        if m and (cmd[0].isdigit() or cmd[0] in "+-"):
            if not self.src:
                return          # C's clamp_line is silent without a file
            num = m.group(1)
            if num[0] == "+":
                self.view.move_to(self.view.cursor + int(num[1:]), len(self.src))
            elif num[0] == "-":
                self.view.move_to(self.view.cursor - int(num[1:]), len(self.src))
            else:
                self.view.move_to(int(num) - 1, len(self.src))
            return
        if cmd in ("+", "-"):
            return
        if cmd in _TGDB_COMMANDS:
            target = _TGDB_COMMANDS[cmd]
            if target.startswith("-"):
                self._run_async(target)
            else:
                self._run_async("-interpreter-exec console %s"
                                % mi.quote(target))
            return
        if cmd == "insert":
            self.set_focus(FOCUS_GDB)
            return
        if cmd == "focus":
            low = arg.lower()
            if low == "cgdb":
                self.set_focus(FOCUS_SOURCE)
            elif low == "gdb":
                self.set_focus(FOCUS_GDB)
            else:
                self.message = "usage: :focus cgdb|gdb"
            return
        if cmd in ("edit", "e"):
            # command_source_reload: re-read the current file
            if self.src:
                self.open_file(self.src.path, self.view.cursor + 1)
            else:
                self.message = "no source file to reload"
            return
        if cmd == "noh":
            # hlsearch rendering is not implemented yet (python_specs.md
            # 8.2); accept the command so rc files behave like C
            return
        if cmd == "bang":
            # C stubs this out too (command_do_bang returns 0)
            return
        if cmd == "syntax":
            # command_parse_syntax: bare `:syntax` reports, `<lang>` forces,
            # `on`/`yes` highlight with the *detected* language
            if not arg:
                forced = getattr(self.src, "_forced_lexer", None) \
                    if self.src else None
                self.message = "syntax: %s" % (
                    "auto" if forced is None else forced)
                return
            try:
                self.config.set("syntax", arg)
            except ConfigError as e:
                self.message = str(e)
                return
            self._applied_syntax = None     # force (re)apply
            if self._apply_syntax_option():
                self.message = "syntax: %s" % (
                    "auto" if arg in ("", "unknown", "on", "yes") else arg)
            return
        if cmd in ("shell", "sh"):
            self._do_shell()
            return
        if cmd == "set":
            try:
                result = self.config.apply_set(arg)
                if isinstance(result, list):
                    self.message = "  ".join(result)
                else:
                    self.message = "%s" % (result,)
                self._apply_config()
            except ConfigError as e:
                self.message = str(e)
            return
        if cmd == "open":
            if not arg:
                self.message = "usage: :open <file>[:line]"
                return
            path, ln = self._split_file_line(arg)
            self.open_file(path, ln)
            return
        if cmd in ("map", "imap", "im"):
            self._do_map(arg, insert=cmd != "map")
            return
        if cmd in ("unmap", "unm", "iunmap", "iu"):
            self._do_unmap(arg, insert=cmd in ("iunmap", "iu"))
            return
        if cmd == "clear":
            self.console.clear()
            self.message = ""
            return
        self.message = "Unknown command: %s" % body

    def _do_map(self, arg, insert):
        fields = arg.split(None, 1)
        if len(fields) != 2:
            self.message = "usage: :map <lhs> <rhs>"
            return
        lhs, rhs = fields[0], fields[1].strip()
        try:
            target = self.imap_set if insert else self.map_set
            target.add(lhs, rhs)
            self._sync_map_set()
            self.message = "mapped %s -> %s" % (lhs, rhs)
        except ValueError as e:
            self.message = str(e)

    def _do_unmap(self, arg, insert):
        target = self.imap_set if insert else self.map_set
        if target.remove(arg.strip()):
            self._sync_map_set()
            self.message = "unmapped %s" % arg.strip()
        else:
            self.message = "no such mapping: %s" % arg.strip()

    def _apply_config(self):
        self.style = build_style(bool(self.config.get("color")))
        if self.app:
            self.app.style = self.style
        self.console.set_scrollback(int(self.config.get("scrollbackbuffersize")))
        self.view.wrapscan = bool(self.config.get("wrapscan"))
        self.mapctx.timeout = self.config.get("timeoutlen")
        self._apply_ttimeoutlen()
        self._apply_split_options()
        self._apply_syntax_option()

    def _apply_syntax_option(self):
        """:set syntax=<lang> forces the lexer of the current file
        (if_highlight_sviewer); ""/"unknown" returns to detection, and
        "on"/"yes" mean highlighting on with *detected* language
        (command_parse_syntax: they never name a lexer).
        Returns False (and sets a message) for an unknown language."""
        value = str(self.config.get("syntax") or "")
        if value == self._applied_syntax:
            return True
        forced = None if value in ("", "unknown", "on", "yes") else value
        if self.src:
            try:
                self.src.force_lexer(forced)
            except ValueError as e:
                self.message = str(e)
                return False
        self._applied_syntax = value
        return True

    def _do_shell(self):
        """:sh -- suspend the UI and run a subshell (run_shell_command).

        run_in_terminal(func) detaches the input and puts the terminal in
        cooked mode while the shell owns the console, then redraws the
        interface; it needs the event loop running, which is exactly when
        a user can type the command.
        """
        from prompt_toolkit.application import run_in_terminal
        if not self.app or not self.app.is_running:
            self.message = "shell unavailable (no running application)"
            return
        shell = os.environ.get("COMSPEC" if os.name == "nt" else "SHELL",
                               "cmd.exe" if os.name == "nt" else "/bin/sh")

        def spawn():
            try:
                subprocess.call(shell)
            except OSError as e:
                self.message = "shell unavailable (%s)" % e
                return
            # run_shell_command waits for one ENTER before handing the
            # terminal back (cgdb.cpp:185-219), so the shell's last screen
            # is not instantly erased by the redraw
            try:
                sys.stdout.write("\nHit ENTER to continue...")
                sys.stdout.flush()
                sys.stdin.readline()
            except OSError:
                pass

        run_in_terminal(spawn)

    def _apply_split_options(self):
        """React to :set winsplitorientation / winsplit / winmin*.

        The C side wires these through command_set_winsplitorientation() /
        command_set_winsplit() -> if_set_*() -> reset_window_shift() +
        if_layout(), and command_set_winmin{height,width} -> if_layout().
        """
        orientation = str(self.config.get("winsplitorientation"))
        if orientation in (WSO_HORIZONTAL, WSO_VERTICAL) and \
                orientation != self.split_orientation:
            self.split_orientation = orientation
            self._reset_window_shift()
        mark = _SPLIT_MARKS.get(str(self.config.get("winsplit")))
        if mark is not None and mark != self.win_split:
            self.win_split = mark
            self._reset_window_shift()
        self._apply_split()

    # =====================================================================
    # Layout
    # =====================================================================
    def _total_height(self):
        try:
            return self.app.output.get_size().rows
        except Exception:
            return 40

    def _total_width(self):
        try:
            return self.app.output.get_size().columns
        except Exception:
            return 110

    def _source_height(self):
        """Visible rows in the source pane, from the same geometry that
        sizes the window.

        C derives every pane from live HEIGHT/WIDTH arithmetic
        (get_src_height, interface.cpp:242-256). Going through
        render_info.displayed_lines instead creates a feedback loop: the
        content is as long as what the previous frame drew, so a pane that
        GREW (after Ctrl-W, `=` or a terminal resize) never fills its new
        region -- the GDB console only used the top half of the right
        column in vertical mode.
        """
        if self.split_orientation == WSO_VERTICAL:
            # the left column, above the status row
            return max(0, self._total_height() - 1)
        return max(0, self._src_extent())

    def _console_height(self):
        """get_gdb_height (interface.cpp:328-342): the gdb pane fills what
        the source pane and the status row leave behind; vertically it is
        the right column minus the edit line."""
        if self.split_orientation == WSO_VERTICAL:
            return max(0, self._total_height() - 1)
        return max(0, self._total_height() - self._source_height() - 2)

    def _source_width(self):
        """get_src_width (interface.cpp:258-272)."""
        if self.split_orientation == WSO_VERTICAL:
            return max(0, self._src_extent())
        return self._total_width()

    def relayout(self):
        total = self._total_height()
        self.view.resize(max(1, total - 12))
        self._apply_split()
        # keep the inferior's terminal size in step with the GDB pane
        # (tgdb_resize_console -> TIOCSWINSZ on the shared pty); the
        # Windows pipe tty has no size, so only the pty path reacts
        if self.tty is not None:
            try:
                self.tty.resize(self._console_height(), self._gdb_width())
            except Exception:
                pass

    # =====================================================================
    # Split geometry (interface.cpp:560-752, 1789-1795)
    # =====================================================================
    def _split_extent(self):
        """The axis window_shift moves along: rows in horizontal mode,
        columns in vertical mode (validate_window_sizes).

        Read live, like the C HEIGHT/WIDTH macros: the pane dimensions below
        are callables evaluated on every layout pass, so a terminal resize
        is picked up on the next repaint -- prompt_toolkit polls the output
        size (Application._poll_output_size, 0.5s) and repaints on change.
        """
        if self.split_orientation == WSO_VERTICAL:
            return self._total_width()
        return self._total_height()

    def _validate_window_sizes(self):
        """Clamp window_shift to the winminheight/winminwidth bounds.

        validate_window_sizes (interface.cpp:560-590): the split point may
        not starve either pane below the user's minimum. Called from
        _src_extent(), i.e. on every layout pass, which is where the C
        version runs it too (if_layout is the resize path's destination).
        """
        ext = self._split_extent()
        odd = (ext + 1) % 2
        minimum = int(self.config.get(
            "winminwidth" if self.split_orientation == WSO_VERTICAL
            else "winminheight"))
        max_shift = (ext // 2) - odd - minimum
        min_shift = -(ext // 2) + minimum
        if self.window_shift > max_shift:
            self.window_shift = max_shift
        elif self.window_shift < min_shift:
            self.window_shift = min_shift

    def _src_extent(self):
        """Size of the source pane along the split axis.

        get_src_height()/get_src_width() (interface.cpp:242-272): half the
        screen plus the shift. (ext + 0.5) / 2 truncates to ext/2 for even
        sizes and one below it for odd ones, exactly like the C code.
        """
        self._validate_window_sizes()
        return int((self._split_extent() + 0.5) / 2) + self.window_shift

    def _gdb_width(self):
        """get_gdb_width() (interface.cpp:343-363): full width horizontally,
        the right column minus the separator vertically."""
        if self.split_orientation == WSO_VERTICAL:
            return max(0, self._total_width() - self._src_extent() - 1)
        return self._total_width()

    def _exact_dim(self, extent):
        """A Dimension locked to `extent`, as a callable over the live value.

        to_dimension() resolves callables on every layout pass, which is
        what keeps the split tracking the terminal size.
        """
        return lambda: D(preferred=extent(), min=extent(), max=extent())

    def _apply_split(self):
        """(Re)build the layout tree and its dimensions for the orientation.

        if_layout() (interface.cpp:598-640) re-derives every window's size
        and position on each layout change. Sizes here are callables
        evaluated per layout pass, so only an orientation change actually
        needs to rebuild the container tree.
        """
        if self.window_shift is None:
            self._reset_window_shift()
        if self._filedlg_active:
            # the dialog is the whole tree; pane geometry is irrelevant
            self._rebuild_layout()
            return
        root = self._build_root()
        if self.split_orientation == WSO_VERTICAL:
            # the source pane fills the left column above the status row
            self.source_window.height = D(weight=1)
            self.left_col.width = self._exact_dim(self._src_extent)
        else:
            self.source_window.height = self._exact_dim(self._src_extent)
        if self.app:
            self.app.layout = Layout(root)
            self.set_focus(self.focus)
        self.console.resize(self._gdb_width())
        self.invalidate()

    def _reset_window_shift(self):
        """Put the split point back onto the quarter mark.

        reset_window_shift (interface.cpp:1789-1795), used when the
        orientation or the winsplit option changes: the mark is re-read
        against the *current* axis, so the pane keeps its fraction.
        """
        half = self._split_extent() // 2
        mark = self.win_split if self.win_split is not None else 0
        self.window_shift = int(half * (mark / 2.0))

    def _toggle_orientation(self):
        """Ctrl-W: swap horizontal/vertical split (interface.cpp:1498-1509).

        window_shift carries over unchanged, as in C -- only its unit
        changes from rows to columns.
        """
        self.split_orientation = (WSO_VERTICAL
                                  if self.split_orientation == WSO_HORIZONTAL
                                  else WSO_HORIZONTAL)
        self._apply_split()

    def _window_resize(self, delta):
        """`=` / `-`: move the split one step along its axis.

        source_input's '=' and '-' cases (interface.cpp:1263-1281) call
        increase/decrease_win_height(0): window_shift by one and the quarter
        mark is forgotten (WIN_SPLIT_FREE, here None).
        """
        self.win_split = None
        self.window_shift += delta
        self._apply_split()

    def _window_resize_jump(self, direction):
        """`+` / `_`: jump to the next quarter mark.

        increase/decrease_win_height(1) (interface.cpp:672-752). The C jump
        functions scale the mark against HEIGHT/2 even in vertical mode
        where the shift counts columns; reset_window_shift is
        orientation-aware and so is this, otherwise the split would move by
        screen-height fractions in a screen-width direction.
        """
        half = self._split_extent() // 2
        mark = self.win_split
        if mark is None:
            mark = int((2 * self.window_shift) / half) if half else 0
            if direction > 0 and self.window_shift > 0:
                mark += 1
            elif direction < 0 and self.window_shift < 0:
                mark -= 1
        else:
            mark += direction
        mark = max(-2, min(2, mark))
        self.win_split = mark
        self.window_shift = int(half * (mark / 2.0))
        self._apply_split()

    def _sync_map_set(self):
        """Point the mapping engine at the set the current focus uses
        (cgdb.cpp:427-432: source -> :map, GDB -> :imap, dialog and
        command line -> cleared)."""
        target = {FOCUS_SOURCE: self.map_set,
                  FOCUS_GDB: self.imap_set}.get(self.focus, self._empty_set)
        if self.mapctx.map_set is not target:
            self.mapctx.set_map_set(target)

    def set_focus(self, which):
        self.focus = which
        # cgdb swaps the active map set with the focus: without this a
        # `:map q ...` would hijack the file dialog's q key.
        self._sync_map_set()
        # In GDB focus with scroll mode on, the console window is the
        # focused control -- that is what makes prompt_toolkit draw the
        # scroll cursor on it; otherwise the edit line keeps the focus (and
        # its own cursor). cgdb switches the scroller's cursor visibility,
        # which is the same distinction expressed differently.
        if which == FOCUS_GDB and self.scroll.active:
            target = self.console_window
        else:
            target = {FOCUS_SOURCE: self.source_window,
                      FOCUS_GDB: self.edit_window,
                      FOCUS_COMMAND: self.cmdline_window,
                      FOCUS_FILEDLG: self.filedlg_window}[which]
        # The dialog replaces the whole screen, so crossing its boundary
        # needs a new layout tree; ordinary focus moves stay in one tree.
        was_dialog = self._filedlg_active
        self._filedlg_active = (which == FOCUS_FILEDLG)
        if was_dialog != self._filedlg_active:
            self._rebuild_layout()
            return
        try:
            self.app.layout.focus(target)
        except Exception:
            pass

    def _rebuild_layout(self):
        """Swap in a freshly built container tree (dialog open/close)."""
        root = self._build_root()
        if self.app:
            self.app.layout = Layout(root)
            self.set_focus(self.focus)
        self.invalidate()

    def invalidate(self):
        if self.app:
            try:
                self.app.invalidate()
            except Exception:
                pass

    # =====================================================================
    # Rendering
    # =====================================================================
    def _source_text(self):
        """One frame of the source pane (source_display, sources.cpp:760-1058).

        Per line: `lineno │ marker text`, lwidth = digits of the line count
        (no minimum). Breakpoints have no character of their own -- they
        color the line NUMBER, red for enabled and yellow for disabled,
        ahead of the executing (green) and selected (white) line numbers.
        The bar column shows a mark char, the arrow styles' `│` (colored,
        but the SAME glyph as the plain `│` so the column stays aligned
        across fonts), or the plain `│` (bold while this pane is focused).
        The marker column is `>` (shortarrow) or `---->` / a block that
        reaches to the line's first non-blank (longarrow / block) -- the
        dashes are ASCII, unlike C's `─`, because box-drawing glyphs and
        ASCII text mix badly in some Windows fonts/codepages. The
        `highlight` styles paint the whole line. Past EOF the rows are `~`
        lines, and a file shorter than the window is block-centered -- the
        top line is *derived* from the cursor each frame
        (sources.cpp:837-847), which is what keeps the cursor line
        vertically centered like the C version.
        """
        width = self._source_width()
        h = self._source_height()
        self.view.height = h
        if self.src is None:
            return [("", " CGDB (Python) -- no source file loaded\n"
                         " open one with :open <path>, or start debugging\n"
                         " F5 run   F8 next   F10 step   :q quit")]
        total = len(self.src)
        if total == 0:
            return [("", " CGDB (Python) -- empty source file")]
        top = self.view.top_line(total, h)
        self.view.top = top
        lwidth = len(str(total))
        tabstop = int(self.config.get("tabstop"))
        showmarks = bool(self.config.get("showmarks"))
        eld = str(self.config.get("executinglinedisplay"))
        sld = str(self.config.get("selectedlinedisplay"))
        gutter = lwidth + 2
        avail = max(0, width - gutter)
        focused = (self.focus == FOCUS_SOURCE)
        # cap the horizontal scroll at the longest line (sources.cpp:1091-1110)
        max_left = max(0, self.src.display_max_width(tabstop) - avail + 6)
        left = min(self.view.left, max_left)
        self.view.left = left
        exe_line_no = None
        if self.exec_line and self._same_file(self.exec_line[0],
                                              self.src.path):
            exe_line_no = self.exec_line[1] - 1

        out = []
        for r in range(h):
            line = top + r
            if out:
                out.append(("", "\n"))
            if line < 0 or line >= total:
                out.append(("class:line-number", " " * (lwidth - 1) + "~"))
                out.append(("class:vline-focus" if focused
                            else "class:vline", "│"))
                out.append(("", " "))
                continue
            lineno = line + 1
            is_sel = (line == self.view.cursor)
            is_exe = (line == exe_line_no)
            raw = self.src.line(line)
            leading = len(raw) - len(raw.lstrip(" \t"))

            # --- line number cell: breakpoint color wins, then exe, sel ---
            bp = self.breakpoints.line_status(self.src.path, lineno)
            if bp == "enabled":
                numcls = "class:breakpoint"
            elif bp == "disabled":
                numcls = "class:breakpoint.disabled"
            elif is_exe:
                numcls = "class:line-number.executing"
            elif is_sel:
                numcls = "class:line-number.current"
            else:
                numcls = "class:line-number"
            out.append((numcls, "%*d" % (lwidth, lineno)))

            # --- which display style governs this line (exe wins over sel) --
            if is_exe:
                style, arrow_cls = eld, "class:exec-arrow"
                block_cls, hl_cls = "class:exec-block", \
                    "class:exec-line-highlight"
            elif is_sel:
                style, arrow_cls = sld, "class:sel-arrow"
                block_cls, hl_cls = "class:sel-block", "class:selected-line"
            else:
                style = None

            # --- bar column: mark > arrow-style LTEE > plain VLINE --------
            mark = None
            if showmarks:
                for ch, ln in self.view.marks.items():
                    if ln == line:
                        mark = ch
                        break
            if mark is not None:
                out.append(("class:mark", mark))
            elif is_exe and style in ("shortarrow", "longarrow"):
                out.append((arrow_cls, "│"))
            elif is_sel and style in ("shortarrow", "longarrow"):
                out.append((arrow_cls, "│"))
            else:
                out.append(("class:vline-focus" if focused
                            else "class:vline", "│"))

            # --- marker column; longarrow/block reach over the leading ws --
            colskip = 0
            if style == "shortarrow":
                out.append((arrow_cls, ">"))
            elif style == "longarrow":
                n = max(0, leading - (left + 1))
                out.append((arrow_cls, "-" * n + ">"))
                colskip = n
            elif style == "block":
                n = max(0, leading - (left + 1))
                out.append(("", " " * n))
                out.append((block_cls, " "))
                colskip = n
            elif style == "highlight":
                out.append((hl_cls, " "))
            else:
                out.append(("", " "))

            # --- text -------------------------------------------------------
            line_cls = hl_cls if style == "highlight" else ""
            # ptk concatenates class names by SPLITTING ON SPACES: without
            # the separator "class:selected-lineclass:kw" parses as one
            # unknown class and the whole style (fg and bg) is lost
            tok_prefix = (line_cls + " ") if line_cls else ""
            eff_left = left + colskip
            toks = self.src.tokens_for_line(line)
            col = 0
            if toks:
                for tok, val in toks:
                    if col >= avail:
                        break
                    val = val.expandtabs(tabstop)
                    if not val:
                        continue
                    if eff_left:
                        if len(val) <= eff_left:
                            eff_left -= len(val)
                            continue
                        val = val[eff_left:]
                        eff_left = 0
                    room = avail - col
                    out.append((tok_prefix + style_for_token(tok),
                                val[:room]))
                    col += len(val[:room])
            else:
                body = raw.expandtabs(tabstop)[left + colskip:
                                               left + colskip + avail]
                out.append((line_cls, body))
                col = len(body)
            if line_cls and col < avail:
                # the highlight styles paint the whole row, not just the text
                out.append((line_cls, " " * (avail - col)))
            if is_sel:
                # a written cell for the terminal cursor to sit on --
                # curses leaves it right after the last character
                # (swin_curs_set + the final wrefresh in source_display)
                if col < avail:
                    out.append(("", " "))
                self._cursor_cell = (r, gutter + min(col, avail - 1))
        return out

    def _source_cursor_pos(self):
        """Cursor at the end of the selected line (sources.cpp:831 + the
        final focus refresh in interface.cpp). The cell itself is written
        by _source_text and recorded there, because prompt_toolkit can
        only place a cursor on a cell that exists."""
        if self.src is None or not len(self.src) or self.focus != FOCUS_SOURCE:
            return None
        cell = getattr(self, "_cursor_cell", None)
        if cell is None:
            return None
        return Point(x=cell[1], y=cell[0])

    @staticmethod
    def _same_file(a, b):
        if not a or not b:
            return False
        try:
            return os.path.normcase(os.path.abspath(a)) == \
                os.path.normcase(os.path.abspath(b))
        except Exception:
            return a == b

    def _console_text(self):
        # ConsoleModel wraps its lines to the gdb pane's width; re-sync here
        # so a terminal resize rewraps them (C: tgdb_resize_console from
        # if_layout, which is also the resize path's destination).
        live = self._gdb_width()
        if live and live != self.console.width:
            self.console.resize(live)
        h = self._console_height()
        delta = self.scroll.delta if self.scroll.active else 0
        # up to h rows, filled from the top like a real terminal (vterm
        # writes from row 0): sparse output leaves the bottom blank, the
        # current line is the last one, exactly as a terminal looks.
        rows = self.console.visible_lines(h, delta)
        out = []
        for i, ln in enumerate(rows):
            if i:
                out.append(("", "\n"))
            if i == 0 and self.scroll.active:
                # scroller.cpp:632-644: [delta/scrollback] overlays the
                # RIGHT edge of the first row while scroll mode is on
                status = self.scroll.status(self.console.max_delta(h))
                cut = max(0, live - len(status))
                pad = max(0, cut - len(ln))
                out.append(("class:console", ln[:cut] + " " * pad))
                out.append(("class:scroll-status", status))
            else:
                out.append(("class:console", ln))
        return out

    def _console_cursor_pos(self):
        """The scroll-mode cursor, drawn on the console window while it is
        the focused GDB control (scroller.cpp:587-593). The column is
        clamped onto the row -- prompt_toolkit can only place a cursor on
        a written cell."""
        if not self.scroll.active:
            return None
        h = self._console_height()
        rows = self.console.visible_lines(h, self.scroll.delta)
        r = self.scroll.row
        if not 0 <= r < len(rows):
            return None
        col = min(self.scroll.col, len(rows[r]))
        return Point(x=col, y=r)

    def _status_text(self):
        """The status row in C's shape (update_status_win, interface.cpp:429-478).

        The content is the current file's full path, or a pending message
        replacing the whole row until the next key (if_display_message);
        over-long content shows its tail behind a `>`. The `*` the C source
        paints for GDB focus is dead code there -- if_display_message
        repaints the full row right after it -- and the official cgdb
        shows none either (measured), so neither does this.
        """
        width = max(1, self._source_width())
        if self.message:
            text = self.message
        elif self.src:
            text = self.src.path
        else:
            text = ""
        if len(text) > width:
            text = ">" + text[len(text) - width + 1:]
        return [("class:status", text)]

    def _help_text(self):
        """The :help cheatsheet, kept to one screen on purpose.

        C swaps in the tgdb help *file* (a full manual); this port has no
        such file and shows this built-in reference instead -- modes and
        window switching first, then what the F-keys do, then the common
        motions and the file/breakpoint keys.
        """
        lines = [
            "CGDB (Python) -- key reference",
            "",
            "modes",
            "  ESC   to the source window        i     to the GDB window",
            "  s     GDB window, scroll mode     :     command line",
            "",
            "function keys (source window)",
            "  F5 run   F6 continue   F7 finish   F8 next   F10 step",
            "",
            "moving around (source window)",
            "  h j k l   move, h/l scrolls      gg / G   top / bottom",
            "  Ctrl-F/B, PageUp/PageDown         Ctrl-U/D   page / half page",
            "  / ? n N   search                  5j   a count moves 5 lines",
            "",
            "files and breakpoints (source window)",
            "  space     toggle breakpoint       t     temporary breakpoint",
            "  o         open the file dialog    u     run until this line",
            "",
            "misc: Ctrl-W split, = - + _ resize, Ctrl-L redraw,",
            "      Ctrl-C interrupt, m/' marks -- q or ESC closes this page",
        ]
        out = []
        for i, text in enumerate(lines):
            if i:
                out.append(("", "\n"))
            cls = "class:filedlg.label" if i == 0 else ""
            if text and text.endswith(":") and len(text) < 30:
                cls = "class:filedlg.label"
            out.append((cls, text))
        return out

    def show_help(self):
        """:help / F1 -- the full-screen key reference.

        C swaps the whole screen for the tgdb help file; this port shows
        its own reference page the same way (q closes).
        """
        self._help_active = True
        self._rebuild_layout()

    def _close_help(self):
        self._help_active = False
        self._rebuild_layout()

    # =====================================================================
    # GDB interaction
    # =====================================================================
    def start_gdb(self):
        callbacks = {
            "on_console": self._on_console,
            "on_target": self._on_target,
            "on_log": self._on_log,
            "on_stopped": self._on_stopped,
            "on_running": self._on_running,
            "on_notify": self._on_notify,
            "on_exit": self._on_gdb_exit,
        }
        self.gdb = (self._client_factory(callbacks, self.gdb_path,
                                         self.gdb_args)
                    if self._client_factory else
                    GdbClient(gdb_path=self.gdb_path, args=self.gdb_args,
                              callbacks=callbacks))
        try:
            self.gdb.start()
        except GdbError as e:
            self.console.add_text("cgdb: %s\n" % e)
            self.message = "gdb failed to start"
            return False
        self.gdb_alive = True
        # Give the inferior its own terminal so its output does not
        # corrupt the MI stream (win_specs.md §4 E2a): a pty on POSIX,
        # its own console window on Windows (gdb "new-console on", the
        # Visual Studio convention -- the popup handles scanf/ANSI/
        # Ctrl-C natively and the MI stream stays clean, PoC-verified
        # on gdb 17.1; .tmp/poc-newcon).
        try:
            self.tty = (self._tty_factory(self._on_inferior_output)
                        if self._tty_factory else
                        create_inferior_tty(self._on_inferior_output))
            self.tty.start()
            # The pty device path must NOT be quoted -- see
            # PtyInferiorTty.mi_command().
            self.gdb.call(self.tty.mi_command(), timeout=10)
            # gdb answers ^done even when it could not apply the setting,
            # so verify rather than trust (the silent-failure mode once
            # let inferior stdout corrupt the MI stream during
            # development).
            shown = self.gdb.call(self.tty.verify_command(), timeout=10)
            if not self.tty.verify_ok(shown.payload if shown else None):
                raise GdbError("gdb did not accept the inferior terminal "
                               "setting (%r)" % (shown and shown.payload))
        except Exception as e:
            self.tty = None
            self._on_console("cgdb: inferior terminal unavailable (%s); "
                             "program output may interleave with MI\n" % e)
        # C asks gdb for the current location as soon as gdb is ready
        # (tgdb_request_current_location -> -file-list-exec-source-file);
        # that is what puts the debuggee's main file into the source
        # window before anything has run. Without symbols gdb answers
        # ^error and there is simply nothing to show.
        try:
            rec = self.gdb.call("-file-list-exec-source-file", timeout=10)
        except GdbError:
            rec = None
        if rec is not None and rec.kind == "result" and rec.cls == "done":
            payload = rec.payload or {}
            path = payload.get("fullname") or payload.get("file") or ""
            try:
                line = int(payload.get("line") or 0)
            except (TypeError, ValueError):
                line = 0
            if path:
                self.goto_file_line(path, line or None)
        self.refresh_breakpoints()
        self._gdb_ready = True
        return True

    def _on_console(self, text):
        if not text:
            return
        self.console.add_bytes(text)
        self.invalidate()

    def _on_target(self, text):
        # the inferior's own output, routed through gdb's @ stream
        self._on_console(text)

    def _on_inferior_output(self, data):
        self.console.add_bytes(data)
        self.invalidate()

    def _on_log(self, text):
        if text:
            self.console.add_bytes(text)
            self.invalidate()

    def _on_running(self, payload):
        self.running_inferior = True
        self.invalidate()

    def _on_stopped(self, payload):
        self.running_inferior = False
        frame = (payload or {}).get("frame") or {}
        if isinstance(frame, list):
            frame = frame[0] if frame else {}
        fullname = frame.get("fullname")
        line = frame.get("line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        if fullname and line:
            self.exec_line = (fullname, line)
            self.goto_file_line(fullname, line)
        self.refresh_breakpoints()
        self.invalidate()

    def _on_notify(self, cls, payload):
        if cls in ("breakpoint-created", "breakpoint-modified",
                   "breakpoint-deleted"):
            if self.breakpoints.absorb_async(cls, payload):
                self.invalidate()
        elif cls == "thread-selected":
            # switching threads moves the source position too: C treats
            # =thread-selected exactly like *stopped and runs it through
            # source_position_changed (tgdb.cpp:486-504); `thread 2` typed
            # at the GDB window produces only this record.
            frame = (payload or {}).get("frame") or {}
            if isinstance(frame, list):
                frame = frame[0] if frame else {}
            fullname = frame.get("fullname")
            line = frame.get("line")
            try:
                line = int(line) if line is not None else None
            except (TypeError, ValueError):
                line = None
            if fullname and line:
                self.exec_line = (fullname, line)
                self.goto_file_line(fullname, line)
                self.invalidate()
        elif cls == "thread-group-exited":
            self.running_inferior = False
            self.invalidate()

    def _on_gdb_exit(self, code):
        self.gdb_alive = False
        self.running_inferior = False
        self.console.add_text("\ncgdb: gdb exited (code %s)\n" % (code,))
        self.message = "gdb exited"
        self.invalidate()
        if not self._gdb_ready:
            # gdb died during startup. Stay up: prompt_toolkit restores the
            # alternate screen on exit, so quitting now would erase the only
            # explanation the user has. This mirrors the spawn-failure path in
            # start_gdb(), which also keeps the UI alive to show the message.
            return
        # Otherwise cgdb follows gdb down: EOF from the MI channel makes
        # tgdb_process() fail, which ends main_loop() and the program
        # (cgdb.cpp:760-772). This callback runs on the reader thread and
        # Application.exit() resolves an asyncio future, which is not
        # thread-safe, so hand it to the UI loop.
        self._call_on_ui_thread(self.quit)

    def _call_on_ui_thread(self, fn):
        loop = getattr(self.app, "loop", None) if self.app else None
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(fn)
                return
            except RuntimeError:
                pass
        fn()

    # --- commands ---------------------------------------------------------
    def _safe_call(self, command, timeout=15.0):
        if not self.gdb or not self.gdb.running:
            self.message = "gdb is not running"
            return None
        try:
            return self.gdb.call(command, timeout=timeout)
        except GdbError as e:
            self.message = str(e)
            return None

    def send_console_command(self, text):
        """Run a user-typed gdb command and echo it into the console."""
        if not self.gdb or not self.gdb.running:
            self.console.add_text("%s%s\n" % (GDB_PROMPT, text))
            self.console.add_text("cgdb: gdb is not running\n")
            return
        if self.running_inferior:
            # POSIX: the inferior owns the pty; the input goes to it raw
            # and is echoed without a (gdb) prompt -- the prompt belongs
            # to gdb, which is not reading input while the inferior runs.
            # Windows: the inferior reads its own console window, there is
            # nothing to deliver (the line is not echoed either).
            if self.tty is not None:
                self.tty.write(text + "\n")
            return
        self.console.add_text("%s%s\n" % (GDB_PROMPT, text))
        threading.Thread(target=self._console_command_worker, args=(text,),
                         daemon=True).start()

    def _console_command_worker(self, text):
        try:
            self.gdb.console_command(text, timeout=60)
        except GdbError as e:
            # An ^error result is already echoed by GdbClient._dispatch, so
            # only report failures that produced no record at all (timeouts,
            # a dead pipe) -- otherwise the user sees the same error twice.
            if "timeout" in str(e) or "failed" in str(e):
                self._on_console("cgdb: %s\n" % e)
        finally:
            self.invalidate()

    def _on_gdb_accept(self, buff):
        text = buff.text
        buff.reset()
        self.send_console_command(text)

    def _run_async(self, command, timeout=30.0, on_result=None):
        if not self.gdb or not self.gdb.running:
            self.message = "gdb is not running"
            return
        threading.Thread(target=self._async_worker,
                         args=(command, timeout, on_result),
                         daemon=True).start()

    def _async_worker(self, command, timeout, on_result):
        rec = self._safe_call(command, timeout)
        if rec is not None and on_result is not None:
            try:
                on_result(rec)
            except Exception:
                import traceback
                sys.stderr.write("cgdbpy: result handler failed:\n%s"
                                 % traceback.format_exc())
        self.invalidate()

    def gdb_run(self):
        # C's F5 is TGDB_RUN, unconditionally: while the inferior runs the
        # command queues and restarts it once gdb is back in charge. Break
        # in first so a running inferior actually sees the run.
        if self.running_inferior:
            self.interrupt()
        self._run_async("-exec-run")

    def gdb_continue(self):
        self._run_async("-exec-continue")

    def gdb_next(self):
        self._run_async("-exec-next")

    def gdb_step(self):
        self._run_async("-exec-step")

    def gdb_finish(self):
        self._run_async("-exec-finish")

    def gdb_until(self, path=None, line=None):
        """`u`: run until the displayed file's line.

        tgdb passes path AND line (tgdb_request_until_line); when the
        source window shows some other file than the current frame, an
        `-exec-until N` would silently target the frame's file, so the
        cross-file case goes through the console spelling gdb expects.
        """
        if not path or not line or (self.exec_line and
                                    self._same_file(self.exec_line[0], path)):
            self._run_async("-exec-until %d" % (line or 0))
            return
        self._run_async('-interpreter-exec console %s'
                        % mi.quote('until "%s":%d' % (path, line)))

    def gdb_up(self):
        # MI has no -exec-up/-exec-down; the console commands are what
        # tgdb sends for both (tgdb.cpp:836-838)
        self._run_async('-interpreter-exec console %s' % mi.quote("up"))

    def gdb_down(self):
        self._run_async('-interpreter-exec console %s' % mi.quote("down"))

    def interrupt(self):
        if self.gdb:
            self.gdb.interrupt()

    def refresh_breakpoints(self):
        rec = self._safe_call("-break-info", timeout=10)
        if rec is not None and rec.kind == "result" and rec.cls == "done":
            self.breakpoints.update_from_mi(rec.payload)
            self.invalidate()

    def toggle_breakpoint(self, temporary=False):
        if not self.src:
            self.message = "no source file"
            return
        line = self.view.cursor + 1
        existing = self.breakpoints.at(self.src.path, line)
        if existing:
            for n in existing:
                self._run_async("-break-delete %s" % n,
                                on_result=self._on_break_deleted)
            self.message = "deleted breakpoint %s" % ",".join(existing)
            # reflect the deletion immediately; the async reply only confirms
            for n in existing:
                self.breakpoints.absorb_async("breakpoint-deleted", {"id": n})
        else:
            cmd = "-break-insert -t" if temporary else "-break-insert"
            self._run_async(
                '%s -- %s' % (cmd, mi.breakpoint_location(self.src.path, line)),
                on_result=self._on_break_inserted)
            self.message = "%s breakpoint at %s:%d" % (
                "temporary" if temporary else "", self.src.name, line)

    def _on_break_inserted(self, rec):
        """Absorb the bkpt from a `-break-insert` result.

        gdb does NOT emit =breakpoint-created for -break-insert (measured on
        17.1, see test_gdb_integration.test_break_insert_does_not_emit_a_notif
        ication), so waiting for a notification would leave the gutter marker
        missing until the next stop.
        """
        if rec.kind == "result" and rec.cls == "done":
            self.breakpoints.absorb_result_bkpt(rec.payload)
            self.invalidate()

    def _on_break_deleted(self, rec):
        if rec.kind == "result":
            self.refresh_breakpoints()

    # --- source files -----------------------------------------------------
    @staticmethod
    def _split_file_line(arg):
        """Split a `path[:line]` argument into (path, line-or-None).

        Ambiguous on Windows, where `:` is also the drive separator: a plain
        rpartition(":") reads `C:\\dir\\f.c` as file "C" and turns a directory
        whose last component is numeric (`C:\\dir\\123`) into a request for its
        line 123. Trust the filesystem first -- if the whole argument exists it
        is a path -- and only then try the suffix.
        """
        if os.path.exists(arg):
            return arg, None
        head, sep, tail = arg.rpartition(":")
        if sep and head and tail.isdigit():
            return head, int(tail)
        return arg, None

    def open_file(self, path, line=None):
        if not path:
            return
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        # re-opening the current buffer keeps the view: `:e` re-reads only
        # when the file changed on disk, and choosing the already-open file
        # from the dialog leaves the cursor exactly where it was
        # (source_set_exec_line / source_reload, sources.cpp:1112-1181)
        if self.src and self._same_file(self.src.path, path):
            self._reload_if_changed()
            if line:
                self.view.move_to(line - 1, len(self.src))
            self.invalidate()
            return
        if not os.path.exists(path):
            self.message = "no such file: %s" % path
            return
        try:
            self.src = SourceFile(path)
        except OSError as e:
            self.message = "cannot read %s: %s" % (path, e)
            return
        self.src.ensure_highlighted(
            on_done=lambda _f: self._call_on_ui_thread(self.invalidate))
        self.view.left = 0
        self.view.marks = {}
        # a :set syntax=<lang> from before this file was open applies now
        self._applied_syntax = None
        self._apply_syntax_option()
        self.view.cursor = max(0, (line or 1) - 1)
        self.console.resize(self._gdb_width())
        self.message = "%s (%d lines, %s)" % (self.src.name, len(self.src),
                                              self.src.encoding)
        self.invalidate()

    def _reload_if_changed(self, force=False):
        """source_reload: re-read the current file when its mtime moved
        (autosourcereload runs on every stop; `:e` checks the same dirty
        test), keeping the cursor where it was (sources.cpp:1363-1400)."""
        if not self.src:
            return
        if not force and not (bool(self.config.get("autosourcereload"))
                              and self.src.changed_on_disk()):
            return
        try:
            fresh = SourceFile(self.src.path)
        except OSError:
            return
        cursor = self.view.cursor
        self.src = fresh
        self.src.ensure_highlighted(
            on_done=lambda _f: self._call_on_ui_thread(self.invalidate))
        self.view.move_to(min(cursor, max(0, len(self.src) - 1)),
                          len(self.src))
        self._applied_syntax = None
        self._apply_syntax_option()

    def goto_file_line(self, path, line):
        if self.src and self._same_file(self.src.path, path):
            self._reload_if_changed()
            self.view.move_to(line - 1, len(self.src))
            self.invalidate()
            return
        if os.path.exists(path):
            self.open_file(path, line)

    def quit(self):
        try:
            if self.gdb and self.gdb.running:
                self.gdb.call("-gdb-exit", timeout=3)
        except Exception:
            pass
        if self.tty:
            self.tty.close()
        if self.gdb:
            self.gdb.stop()
        self._quitting = True
        if self.app:
            try:
                self.app.exit()
            except Exception:
                # Application.exit() raises when the app is not running,
                # which is the normal case for :q issued from a test or before
                # the event loop starts. The flags above still took effect.
                pass

    # =====================================================================
    def run(self):
        ok = self.start_gdb()
        self.relayout()
        self.run_rc_commands()
        if not ok:
            self.message = "gdb did not start; :q to quit"
        try:
            self.app.run()
        finally:
            if self.tty:
                self.tty.close()
            if self.gdb:
                self.gdb.stop()
        return 0

    def run_rc_commands(self):
        """Replay the rc file's command lines.

        command_parse_file runs after kui and tgdb exist but before the
        first frame, so rc `map`/`set`/`:start` all take effect; errors are
        reported per line and parsing continues (cgdbrc.cpp:1101-1134).
        """
        for lineno, line in self.rc_lines:
            self.message = ""
            try:
                self._do_colon(line)
            except Exception as e:
                self.console.add_text(
                    "Error parsing line %d: %s\n" % (lineno, e))
                continue
            # an unknown command is a parse failure in C's rc path too
            if self.message.startswith("Unknown command"):
                self.console.add_text(
                    "Error parsing line %d: %s\n" % (lineno, self.message))
        self.message = ""
        self.rc_lines = []
