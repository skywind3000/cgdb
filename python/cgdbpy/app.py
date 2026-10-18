"""The cgdb TUI, built on prompt_toolkit.

Layout mirrors cgdb: source pane on top, GDB console below, one status line,
and a `:` / `/` command line that replaces the status line while active.

    +------------------------------------------+
    | source pane (line numbers, bp marks)     |
    +------------------------------------------+
    | status line                              |
    +------------------------------------------+
    | gdb console scrollback                   |
    | (gdb) <edit line>                        |
    +------------------------------------------+

Two deliberate deviations from the C version, both recorded in
doc/python_version.md:

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
import sys
import threading

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import (BufferControl,
                                           FormattedTextControl)
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.processors import BeforeInput

from . import keycodes as kc
from . import kui
from . import mi
from .cgdbrc import Config, ConfigError
from .console import ConsoleModel, ScrollState
from .gdbclient import GdbClient, GdbError
from .highlight import build_style, style_for_token
from .inferior_tty import create as create_inferior_tty
from .source import Breakpoints, SourceFile, SourceView

FOCUS_SOURCE = "source"
FOCUS_GDB = "gdb"
FOCUS_COMMAND = "command"

GDB_PROMPT = "(gdb) "

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
                 output=None):
        self.config = config or Config()
        self.focus = FOCUS_SOURCE
        self.last_key = 0
        self.message = ""
        self.running_inferior = False
        self.gdb_alive = False

        # --- console ------------------------------------------------------
        self.console = ConsoleModel(width=80,
                                    scrollback=self.config.get(
                                        "scrollbackbuffersize"))
        self.scroll = ScrollState()

        # --- source -------------------------------------------------------
        self.src = None
        self.view = SourceView(height=20)
        self.breakpoints = Breakpoints()
        self.exec_line = None       # (path, 1-based line) gdb is stopped at

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
                                 accept_handler=self._on_cmd_accept)
        self.cmd_prefix = ":"
        self.search_forward = True
        self.search_origin = None

        # --- keys ---------------------------------------------------------
        self._key_queue = []
        self.map_set = kui.MapSet()
        self.imap_set = kui.MapSet()
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
    def _build_ui(self):
        # focusable=True on the source pane: without it Layout.focus() refuses
        # the window and ESC silently fails to leave the GDB pane.
        self.source_control = FormattedTextControl(self._source_text,
                                                   focusable=True)
        self.status_control = FormattedTextControl(self._status_text)
        self.console_control = FormattedTextControl(self._console_text,
                                                    focusable=True)
        self.cmdline_control = BufferControl(buffer=self.cmd_buffer)

        self.source_window = Window(self.source_control, wrap_lines=False,
                                    always_hide_cursor=True,
                                    height=D(weight=1, preferred=0.6))
        self.status_window = Window(self.status_control, height=1)
        self.console_window = Window(self.console_control, wrap_lines=False,
                                     always_hide_cursor=True,
                                     height=D(weight=1, preferred=0.4))
        self.edit_window = Window(
            BufferControl(
                buffer=self.gdb_buffer,
                input_processors=[BeforeInput(
                    lambda: [("class:console.prompt", GDB_PROMPT)])]),
            height=1, wrap_lines=False)
        self.cmdline_window = Window(self.cmdline_control, height=1,
                                     wrap_lines=False)

        # The command line replaces the gdb edit line while active. Using
        # ConditionalContainer rather than swapping HSplit.children keeps
        # prompt_toolkit's layout cache consistent.
        in_command = Condition(lambda: self.focus == FOCUS_COMMAND)
        self.root = HSplit([
            self.source_window,
            self.status_window,
            self.console_window,
            ConditionalContainer(self.edit_window, filter=~in_command),
            ConditionalContainer(self.cmdline_window, filter=in_command),
        ])

        self.kb = self._build_keybindings()
        self.style = build_style(bool(self.config.get("color")))

        self.app = Application(
            layout=Layout(self.root, focused_element=self.source_window),
            key_bindings=self.kb,
            full_screen=True,
            style=self.style,
            mouse_support=False,
            erase_when_done=False,
            output=self._output,
        )
        self._apply_ttimeoutlen()

    def _apply_ttimeoutlen(self):
        """Wire cgdb's `ttimeoutlen` onto prompt_toolkit's flush timer.

        This is not cosmetic. With VT input mode enabled, prompt_toolkit's
        Vt100Parser holds a lone ESC until it knows no escape sequence
        follows, and only releases it from Application.auto_flush_input()
        after `ttimeoutlen`. prompt_toolkit's default is 0.5 s, which made ESC
        -- cgdb's focus-switch key -- feel broken (measured: ESC arrived 499 ms
        after it was sent, while ordinary keys arrived in 0-3 ms). cgdb's own
        default is 100 ms, and that is what we use.
        """
        try:
            self.app.ttimeoutlen = self.config.get("ttimeoutlen") / 1000.0
        except Exception:
            pass

    def _build_keybindings(self):
        """Register bindings.

        Precedence design: while the GDB edit line has focus and we are not in
        scroll mode, ordinary typing and cursor editing belong to
        prompt_toolkit's Buffer (that is where the free line editor comes
        from). The catch-all is therefore filtered off in that state, and only
        the keys cgdb must own -- ESC, PageUp/PageDown, the F-keys, Ctrl-L,
        Ctrl-C -- stay bound unconditionally.
        """
        kb = KeyBindings()
        editing = Condition(self._buffer_owns_keys)

        @kb.add(Keys.Any, filter=~editing)
        def _any(event):
            self.on_key(event.key_sequence[-1])

        # keys cgdb owns in every focus
        for key in (Keys.PageUp, Keys.PageDown, Keys.ControlL,
                    Keys.ControlC, Keys.F1, Keys.F2, Keys.F3, Keys.F4,
                    Keys.F5, Keys.F6, Keys.F7, Keys.F8, Keys.F9, Keys.F10,
                    Keys.F11, Keys.F12):
            self._bind(kb, key)

        # Escape must be eager. prompt_toolkit treats a bare Escape as a
        # possible prefix of a longer sequence and otherwise waits out
        # `ttimeoutlen` (0.5 s) before calling the handler, which makes ESC
        # feel broken when it is cgdb's focus-switch key. Eager matches take
        # priority and suppress the longer-match wait.
        self._bind(kb, Keys.Escape, eager=True)

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
        """Route one mapped key. Mirrors internal_if_input()."""
        mode_key = self._cgdb_mode_key()
        if key == mode_key:
            if self.focus != FOCUS_SOURCE:
                if self.focus == FOCUS_COMMAND:
                    self._cancel_command()
                # leaving the GDB window also leaves its scroll mode, otherwise
                # the console stays frozen at an old offset with no visible
                # indication of why
                self.scroll.leave()
                self.set_focus(FOCUS_SOURCE)
            return
        if self.focus == FOCUS_COMMAND:
            self._command_key(key)
            return
        # global keys, available in every focus (interface.cpp:1511-1537)
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
        if self.focus == FOCUS_SOURCE:
            self.source_key(key)
        elif self.focus == FOCUS_GDB:
            self.gdb_key(key)

    def _cgdb_mode_key(self):
        name = str(self.config.get("cgdbmodekey")).lower()
        return {"esc": kc.ESC, "tab": kc.CTRL_I,
                "ctrl-g": kc.CTRL_G}.get(name, kc.ESC)

    # =====================================================================
    # Source window keys (cgdb_input + source_input)
    # =====================================================================
    def source_key(self, key):
        total = len(self.src) if self.src else 0
        # mark handling: m<char> and '<char>. cgdb does this with
        # last_key_pressed (interface.cpp:1425-1444), and so do we -- a
        # two-key sequence must not be resolvable by the mapping engine alone,
        # or `:map gg <Up>` could never fire.
        if self.last_key == ord("m") and not kc.is_special(key):
            self.view.set_mark(chr(key))
            self.message = "mark %s set at line %d" % (chr(key),
                                                       self.view.cursor + 1)
            self.last_key = 0
            return
        if self.last_key == ord("'") and not kc.is_special(key):
            ln = self.view.goto_mark(chr(key))
            if ln is None:
                self.message = "mark %s not set" % chr(key)
            else:
                self.view.move_to(ln, total)
            self.last_key = 0
            return
        if self.last_key == ord("g") and key == ord("g"):
            self.view.move_to(0, total)
            self.last_key = 0
            return

        if key == ord("i"):
            self.set_focus(FOCUS_GDB)
            return
        if key == ord("s"):
            self.scroll.enter(self.console.max_delta(self._console_height()))
            self.set_focus(FOCUS_GDB)
            return
        if key == ord(":"):
            self._start_command(":")
            return
        if key in (ord("/"), ord("?")):
            self.search_forward = (key == ord("/"))
            self.search_origin = self.view.cursor
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
            self.message = ("file dialog not implemented in the Python port; "
                            "use :open <path>")
            return
        if key == ord("+"):
            self._adjust_split(+1)
            return
        if key in (ord("-"), ord("_")):
            self._adjust_split(-1)
            return
        if key == ord("m"):
            self.last_key = ord("m")
            return
        if key == ord("'"):
            self.last_key = ord("'")
            return

        # motion
        h = self._source_height()
        if key in (ord("j"), kc.DOWN, kc.CTRL_N):
            self.view.move_to(self.view.cursor + 1, total)
        elif key in (ord("k"), kc.UP, kc.CTRL_P):
            self.view.move_to(self.view.cursor - 1, total)
        elif key == kc.CTRL_D:
            self.view.move_to(self.view.cursor + max(1, h // 2), total)
        elif key == kc.CTRL_U:
            self.view.move_to(self.view.cursor - max(1, h // 2), total)
        elif key == kc.CTRL_F or key == kc.NPAGE:
            self.view.move_to(self.view.cursor + h, total)
        elif key == kc.CTRL_B or key == kc.PPAGE:
            self.view.move_to(self.view.cursor - h, total)
        elif key == ord("G"):
            self.view.move_to(total - 1, total)
        elif key == ord("g"):
            # prefix of `gg`; resolved above on the next key. Anything else
            # after `g` falls through as a no-op, matching vim.
            self.last_key = ord("g")
            return
        elif key == kc.HOME:
            self.view.move_to(0, total)
        elif key == kc.END:
            self.view.move_to(total - 1, total)
        else:
            self.last_key = key
            return
        self.last_key = key

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
    def gdb_key(self, key):
        h = self._console_height()
        mx = self.console.max_delta(h)
        if self.scroll.active:
            if key in (kc.UP, ord("k")):
                self.scroll.up(1, mx, h)
            elif key in (kc.DOWN, ord("j")):
                self.scroll.down(1, mx, h)
            elif key in (kc.PPAGE, kc.CTRL_B):
                self.scroll.page_up(mx, h)
            elif key in (kc.NPAGE, kc.CTRL_F):
                self.scroll.page_down(mx, h)
            elif key in (kc.HOME, ord("g")):
                self.scroll.home(mx, h)
            elif key in (kc.END, ord("G")):
                self.scroll.end(mx, h)
            elif key == kc.ESC or key == ord("q") or key == ord("i"):
                self.scroll.leave()
                if key == ord("i"):
                    self.set_focus(FOCUS_GDB)
            return
        if key == kc.PPAGE:
            self.scroll.enter(mx)
            self.scroll.page_up(mx, h)
            return
        if key == kc.NPAGE:
            self.scroll.enter(mx)
            self.scroll.page_down(mx, h)
            return
        if key == kc.ESC:
            self.set_focus(FOCUS_SOURCE)
            return
        if key == ord(":"):
            self._start_command(":")
            return

    # =====================================================================
    # Command line
    # =====================================================================
    def _start_command(self, prefix):
        self.cmd_prefix = prefix
        self.cmd_buffer.text = prefix
        self.cmd_buffer.cursor_position = len(prefix)
        self._prev_focus = self.focus if self.focus != FOCUS_COMMAND \
            else FOCUS_SOURCE
        self.set_focus(FOCUS_COMMAND)

    def _end_command(self):
        self.set_focus(getattr(self, "_prev_focus", FOCUS_SOURCE))

    def _cancel_command(self):
        self.cmd_buffer.text = ""
        self._end_command()

    def _on_cmd_accept(self, buff):
        """Enter on the `:` / `/` command line."""
        text = buff.text
        buff.reset()
        self._end_command()
        self.execute_command(text)
        self.invalidate()

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
        if line.startswith("/"):
            self._do_search(line[1:], forward=True)
        elif line.startswith("?"):
            self._do_search(line[1:], forward=False)
        elif line.startswith(":"):
            self._do_colon(line[1:].strip())
        else:
            self.send_console_command(line)

    def _do_search(self, pattern, forward):
        if not self.src:
            self.message = "no source file"
            return
        if not pattern:
            self.message = "empty search pattern"
            return
        self.search_forward = forward
        hit = self.view.search_in(self.src, pattern, forward=forward,
                                  ignorecase=bool(self.config.get("ignorecase")))
        self.message = "" if hit is not None else "pattern not found: %s" % pattern

    def _do_colon(self, body):
        if not body:
            return
        parts = body.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("q", "quit", "q!"):
            self.quit()
            return
        if cmd in ("h", "help"):
            self.show_help()
            return
        if cmd == "set":
            try:
                result = self.config.execute("set " + arg if arg else "set")
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
            path, _, ln = arg.rpartition(":")
            if not path or not ln.isdigit():
                path, ln = arg, None
            self.open_file(path, int(ln) if ln else None)
            return
        if cmd == "map":
            self._do_map(arg, insert=False)
            return
        if cmd == "imap":
            self._do_map(arg, insert=True)
            return
        if cmd in ("unmap", "iunmap"):
            self._do_unmap(arg, insert=(cmd == "iunmap"))
            return
        if cmd == "clear":
            self.console.clear()
            self.message = ""
            return
        self.message = "unknown command: %s" % cmd

    def _do_map(self, arg, insert):
        fields = arg.split(None, 1)
        if len(fields) != 2:
            self.message = "usage: :map <lhs> <rhs>"
            return
        lhs, rhs = fields[0], fields[1].strip()
        try:
            target = self.imap_set if insert else self.map_set
            target.add(lhs, rhs)
            self.mapctx.set_map_set(self.map_set)
            self.message = "mapped %s -> %s" % (lhs, rhs)
        except ValueError as e:
            self.message = str(e)

    def _do_unmap(self, arg, insert):
        target = self.imap_set if insert else self.map_set
        if target.remove(arg.strip()):
            self.mapctx.set_map_set(self.map_set)
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
        """Visible rows in the source pane.

        render_info is from the previous frame, so this is one frame behind on
        a resize -- harmless for paging maths. The fallback is used on the
        first frame, before anything has been rendered.
        """
        ri = self.source_window.render_info
        if ri is not None and getattr(ri, "displayed_lines", None):
            return max(1, len(ri.displayed_lines))
        return max(1, self.view.height)

    def _console_height(self):
        ri = self.console_window.render_info
        if ri is not None and getattr(ri, "displayed_lines", None):
            return max(1, len(ri.displayed_lines))
        return max(3, self._total_height() - self._source_height() - 3)

    def _adjust_split(self, delta):
        """`+` / `-` grow or shrink the source pane (interface.cpp:1271-1281)."""
        total = self._total_height()
        wmh = int(self.config.get("winminheight"))
        cur = self.source_window.height
        if not isinstance(cur, int):
            cur = max(1, total - 12)
        new = max(wmh, min(cur + delta, total - 3))
        self.source_window.height = new

    def relayout(self):
        total = self._total_height()
        self.console.resize(self._total_width())
        self.view.resize(max(1, total - 12))
        self.invalidate()

    def set_focus(self, which):
        self.focus = which
        target = {FOCUS_SOURCE: self.source_window,
                  FOCUS_GDB: self.edit_window,
                  FOCUS_COMMAND: self.cmdline_window}[which]
        try:
            self.app.layout.focus(target)
        except Exception:
            pass

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
        width = self._total_width()
        # Both panes have layout-determined heights (weighted D()), so the
        # previous frame's render_info is a stable answer and there is no
        # content/height circularity. Emit exactly the visible rows: padding to
        # a fixed budget would push the real lines out of the window, because
        # prompt_toolkit displays formatted text from the top.
        h = self._source_height()
        self.view.height = h
        if self.src is None:
            return [("", " CGDB (Python) -- no source file loaded\n"
                         " open one with :open <path>, or start debugging\n"
                         " F5 run   F8 next   F10 step   :q quit")]
        total = len(self.src)
        self.view.ensure_visible()
        numw = max(4, len(str(total)) + 1)
        tabstop = int(self.config.get("tabstop"))
        showmarks = bool(self.config.get("showmarks"))
        gutter_w = 1 + numw + 2 + 1
        avail = max(0, width - gutter_w)

        out = []
        for i in range(self.view.top, min(self.view.top + h, total)):
            if out:
                out.append(("", "\n"))
            lineno = i + 1
            cur = (i == self.view.cursor)
            sel = "class:selected-line " if cur else ""
            is_exec = bool(self.exec_line and self.exec_line[1] == lineno
                           and self._same_file(self.exec_line[0],
                                               self.src.path))
            bps = self.breakpoints.at(self.src.path, lineno)
            out.append((sel + "class:breakpoint", "B" if bps else " "))
            out.append((sel + ("class:line-number.current" if cur
                               else "class:line-number"),
                        "%*d" % (numw, lineno)))
            out.append((sel + "class:exec-arrow", "=>" if is_exec else "  "))
            mark = None
            for ch, ln in self.view.marks.items():
                if ln == i:
                    mark = ch
                    break
            if mark and showmarks:
                out.append((sel + "class:breakpoint", mark))
            else:
                out.append((sel, " "))

            text = self.src.line(i)
            toks = self.src.tokens_for_line(i)
            if toks:
                col = 0
                for tok, val in toks:
                    if col >= avail:
                        break
                    val = val.expandtabs(tabstop)
                    if not val:
                        continue
                    room = avail - col
                    out.append((sel + style_for_token(tok), val[:room]))
                    col += len(val)
            else:
                out.append((sel, text.expandtabs(tabstop)[:avail]))
        return out

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
        h = self._console_height()
        delta = self.scroll.delta if self.scroll.active else 0
        # exactly h rows, top-padded, so the newest output sits at the bottom
        # of the pane the way a terminal would
        lines = self.console.visible_lines(h, delta)
        out = []
        for i, ln in enumerate(lines):
            if i:
                out.append(("", "\n"))
            out.append(("class:console", ln))
        if self.scroll.active:
            out.append(("class:status", " " + self.scroll.status(
                self.console.max_delta(h))))
        return out

    def _status_text(self):
        parts = []
        focus_name = {FOCUS_SOURCE: "CGDB", FOCUS_GDB: "GDB",
                      FOCUS_COMMAND: "CMD"}[self.focus]
        parts.append(("class:status.focus", " %s " % focus_name))
        if self.src:
            pos = "%d/%d" % (self.view.cursor + 1, len(self.src))
            parts.append(("class:status", " %s  %s " % (self.src.name, pos)))
        else:
            parts.append(("class:status", " no file "))
        if self.exec_line:
            parts.append(("class:status", " line %d " % self.exec_line[1]))
        if self.running_inferior:
            parts.append(("class:status.focus", " RUNNING "))
        if self.message:
            parts.append(("class:command", " %s" % self.message))
        return parts

    def show_help(self):
        self.message = ("F5 run  F6 continue  F7 finish  F8 next  F10 step  "
                        "ESC source  i gdb  space breakpoint  / search  "
                        ": command  Ctrl-L redraw")

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
        # give the inferior its own tty so its output does not corrupt the MI
        # stream (win_specs.md §4 E2a)
        try:
            self.tty = (self._tty_factory(self._on_inferior_output)
                        if self._tty_factory else
                        create_inferior_tty(self._on_inferior_output))
            self.tty.start()
            # The argument must NOT be quoted -- see InferiorTty.gdb_argument().
            self.gdb.call(self.tty.mi_command(), timeout=10)
            # gdb answers ^done even when it could not open the device, so
            # verify rather than trust (this exact silent failure is what let
            # inferior stdout corrupt the MI stream during development).
            shown = self.gdb.call("-inferior-tty-show", timeout=10)
            want = self.tty.gdb_argument()
            got = (shown.payload or {}).get("inferior_tty", "")
            if want not in got and got not in want:
                raise GdbError("gdb did not accept the inferior tty "
                               "(wanted %r, gdb has %r)" % (want, got))
        except Exception as e:
            self.tty = None
            self._on_console("cgdb: inferior tty unavailable (%s); "
                             "program output may interleave with MI\n" % e)
        self.refresh_breakpoints()
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
        reason = (payload or {}).get("reason", "")
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
        if reason:
            self.message = "stopped: %s" % reason
        self.refresh_breakpoints()
        self.invalidate()

    def _on_notify(self, cls, payload):
        if cls in ("breakpoint-created", "breakpoint-modified",
                   "breakpoint-deleted"):
            if self.breakpoints.absorb_async(cls, payload):
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
        self.console.add_text("%s%s\n" % (GDB_PROMPT, text))
        if not self.gdb or not self.gdb.running:
            self.console.add_text("cgdb: gdb is not running\n")
            return
        if self.running_inferior and self.tty is not None:
            # the inferior owns the terminal; forward raw input to it
            self.tty.write(text + "\n")
            return
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
        if self.running_inferior:
            self.gdb_continue()
            return
        self._run_async("-exec-run")

    def gdb_continue(self):
        self._run_async("-exec-continue")

    def gdb_next(self):
        self._run_async("-exec-next")

    def gdb_step(self):
        self._run_async("-exec-step")

    def gdb_finish(self):
        self._run_async("-exec-finish")

    def gdb_until(self, line=None):
        if line:
            self._run_async('-exec-until %d' % line)
        else:
            self._run_async("-exec-until")

    def gdb_up(self):
        self._run_async("-exec-up")

    def gdb_down(self):
        self._run_async("-exec-down")

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
    def open_file(self, path, line=None):
        if not path:
            return
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if not os.path.exists(path):
            self.message = "no such file: %s" % path
            return
        try:
            self.src = SourceFile(path)
        except OSError as e:
            self.message = "cannot read %s: %s" % (path, e)
            return
        self.src.ensure_highlighted()
        self.view.top = 0
        self.view.cursor = max(0, (line or 1) - 1)
        self.view.ensure_visible()
        self.console.resize(self._total_width())
        self.message = "%s (%d lines, %s)" % (self.src.name, len(self.src),
                                              self.src.encoding)
        self.invalidate()

    def goto_file_line(self, path, line):
        if self.src and self._same_file(self.src.path, path):
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
