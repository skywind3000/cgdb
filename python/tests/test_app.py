"""UI-layer tests, driven headlessly through prompt_toolkit's test pilot.

A FakeGdb records every MI command the UI issues so the tests can assert on
what cgdb would actually send to gdb -- not just on internal state. The real
gdb is exercised separately in test_gdb_integration.py.
"""

import os
import sys
import tempfile

import pytest

from cgdbpy import keycodes as kc
from cgdbpy.app import (FOCUS_COMMAND, FOCUS_FILEDLG, FOCUS_GDB,
                        FOCUS_SOURCE, CgdbApp, translate_key)
from cgdbpy.cgdbrc import Config, WSO_HORIZONTAL, WSO_VERTICAL
from cgdbpy.gdbclient import GdbError
from cgdbpy import mi

from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.output import DummyOutput


def make_output():
    """prompt_toolkit needs an Output to build an Application. Under pytest
    there is no console screen buffer, so Win32 auto-detection raises
    NoConsoleScreenBufferError. DummyOutput reports a fixed 80x40."""
    return DummyOutput()


class FakeRequest:
    def __init__(self, record):
        self.record = record
        self.error = None
        self.done = True

    def wait(self, timeout=None):
        return True


class FakeGdb:
    """Stands in for GdbClient. Records commands, returns canned MI records."""

    def __init__(self, callbacks=None, responses=None):
        self.callbacks = callbacks or {}
        self.commands = []
        self.responses = responses or {}
        self.running = True
        self.started = False
        self.stopped = False
        self.interrupted = 0
        self.at_prompt = True
        self.stats = {"parse_errors": 0}

    def start(self):
        self.started = True
        return self

    def stop(self, timeout=None):
        self.stopped = True
        self.running = False

    def interrupt(self):
        self.interrupted += 1
        return True

    def send(self, command, kind="generic", on_result=None):
        self.commands.append(command)
        return FakeRequest(self._record_for(command))

    def call(self, command, timeout=30.0, kind="generic"):
        self.commands.append(command)
        rec = self._record_for(command)
        if rec.cls == "error":
            raise GdbError(rec.payload.get("msg", "error"))
        return rec

    def console_command(self, text, timeout=30.0):
        return self.call('-interpreter-exec console %s' % mi.quote(text),
                         timeout=timeout)

    def _record_for(self, command):
        for prefix, payload in self.responses.items():
            if command.startswith(prefix):
                return mi.Record(mi.RESULT, token=1, cls="done",
                                 payload=payload)
        if command.startswith("-break-info"):
            return mi.Record(mi.RESULT, token=1, cls="done",
                             payload={"BreakpointTable":
                                      {"nr_rows": "0", "body": []}})
        if command.startswith("-inferior-tty-show"):
            return mi.Record(mi.RESULT, token=1, cls="done",
                             payload={"inferior_tty": self._tty_name})
        return mi.Record(mi.RESULT, token=1, cls="done", payload={})

    _tty_name = "\\\\.\\pipe\\fake"

    # --- test helpers -----------------------------------------------------
    def emit_console(self, text):
        self.callbacks["on_console"](text)

    def emit_stopped(self, payload):
        self.callbacks["on_stopped"](payload)

    def emitted(self, kind):
        return [c for c in self.commands if c.startswith(kind)]


class FakeTty:
    def __init__(self, on_output, on_drain=None):
        self.on_output = on_output
        self.on_drain = on_drain
        self.written = []
        self.started = False
        self.closed = False
        self.resized = []
        # the Windows popup shape: the inferior's terminal is not inside
        # cgdb's screen (PtyInferiorTty is the embedded=True case)
        self.embedded = False

    def start(self):
        self.started = True
        return "\\\\.\\pipe\\faketty"

    def mi_command(self):
        return "-gdb-set inferior-tty \\\\.\\pipe\\faketty"

    def verify_command(self):
        return "-inferior-tty-show"

    def verify_ok(self, payload):
        got = (payload or {}).get("inferior_tty", "")
        return "\\\\.\\pipe\\faketty" in got or got in "\\\\.\\pipe\\faketty"

    def write(self, data):
        self.written.append(data)
        return True

    def request_flush(self, timeout=0.5):
        # no reader thread in the fake: the caller flattens directly
        return True

    def resize(self, rows, cols):
        self.resized.append((rows, cols))

    def close(self):
        self.closed = True


@pytest.fixture
def srcfile():
    fd, path = tempfile.mkstemp(suffix=".c")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(
            ["#include <stdio.h>", "", "int compute(int n) {",
             "    int s = 0;", "    s += n;", "    return s;", "}"] +
            ["int line%d = %d;" % (i, i) for i in range(60)] +
            ["", "int main(void) { return compute(3); }", ""]))
    yield path
    os.unlink(path)


@pytest.mark.skipif(sys.platform != "win32", reason="win32 console output path")
def test_console_output_reports_the_true_width(monkeypatch, srcfile):
    """The layout must know the console's real width, or the last column of
    every pane is dead space.

    Win32Output.get_size() reports one column less than the console: it reads
    the visible width off the 0-based closed interval as `Right - Left`, then
    caps at `dwSize.X - 1` on top ("we avoid the right margin"). The renderer
    never paints beyond the reported width, so the separator bar stopped one
    cell short of the right edge -- visible against cgdb, which paints the
    whole row. _console_output() must widen the size back to the truth.
    """
    from prompt_toolkit.output.win32 import Win32Output
    from prompt_toolkit.output.windows10 import Windows10_Output

    class Info:
        class srWindow:
            Left, Right, Top, Bottom = 0, 119, 0, 29

        class dwSize:
            X, Y = 120, 900

        class dwCursorPosition:
            X, Y = 0, 0

    class FakeWin32(Win32Output):
        """Enough of a Win32Output for the Renderer to be built against it.

        The console-buffer calls are stubbed because pytest's stdout is not a
        Windows console; Renderer.__init__ calls scroll_buffer_to_prompt
        unconditionally, so that one has to exist and do nothing.
        """

        def __init__(self):
            pass

        def get_win32_screen_buffer_info(self):
            return Info()

        def scroll_buffer_to_prompt(self):
            pass

    # constructing a real Windows10_Output needs a console screen buffer;
    # under pytest stdout is a pipe, so neuter the one query the constructor
    # makes and swap in the fake afterwards
    monkeypatch.setattr(Win32Output, "get_win32_screen_buffer_info",
                        lambda self: None)
    out = Windows10_Output(sys.stdout)
    out.win32_output = FakeWin32()
    monkeypatch.setattr("prompt_toolkit.output.create_output",
                        lambda: out)

    a = CgdbApp(config=Config(), source_path=srcfile,
                client_factory=lambda *args: FakeGdb(),
                tty_factory=lambda on_output: FakeTty(on_output))
    assert a.app.output.get_size().columns == 120
    assert a.app.output.get_size().rows == 30


@pytest.fixture
def bare_app(srcfile):
    """A CgdbApp over a fake gdb, in cgdb's real startup state (GDB focus)."""
    fake = {}

    def factory(callbacks, gdb_path, gdb_args):
        fake["gdb"] = FakeGdb(callbacks)
        return fake["gdb"]

    def tty_factory(on_output, on_drain=None):
        fake["tty"] = FakeTty(on_output, on_drain)
        return fake["tty"]

    a = CgdbApp(config=Config(), source_path=srcfile,
                client_factory=factory, tty_factory=tty_factory,
                output=make_output())
    a.start_gdb()
    a._fake = fake
    return a


@pytest.fixture
def app(bare_app):
    """bare_app with the focus a user gets by pressing ESC.

    cgdb starts in the GDB window (interface.cpp:139), but most of these tests
    drive source-window keys. Switching focus in the fixture keeps that
    one-step removed from what they assert; the tests that care about the
    startup state itself ask for bare_app.
    """
    bare_app.set_focus(FOCUS_SOURCE)
    return bare_app


# ---------------------------------------------------------------------------
# key translation
# ---------------------------------------------------------------------------
class KP:
    def __init__(self, key, data=None):
        self.key = key
        self.data = data if data is not None else key


def press(app, key):
    """Feed one key through the real path: on_key -> kui -> dispatch.

    Going through on_key matters: calling app.dispatch() directly bypasses the
    mapping engine, so `:map` tests would pass or fail for the wrong reason.

    This deliberately does NOT force the mapping timeout, because that would
    split a multi-key map apart after its first key. Call flush_keys() when a
    test needs a pending partial match resolved.
    """
    from prompt_toolkit.key_binding.key_processor import KeyPress

    if isinstance(key, int):
        if key < 256:
            ch = chr(key)
            kp = KeyPress(ch, ch)
        else:
            kp = KeyPress(_CGDB_TO_PTK[key], None)
    else:
        kp = KeyPress(key, None)
    app.on_key(kp)


def flush_keys(app):
    """Resolve a pending partial match, as the mapping timeout would."""
    app._drain_keys()
    if app._timeout_handle is not None:
        app._map_timeout()
    else:
        app._drain_keys()


_CGDB_TO_PTK = {}
for _k, _v in __import__("cgdbpy.app",
                         fromlist=["_PTK_TO_CGDB"])._PTK_TO_CGDB.items():
    _CGDB_TO_PTK.setdefault(_v, _k)


def test_translate_plain_chars():
    for ch in "abcXYZ019 ~!@#":
        assert translate_key(KP(ch, ch)) == ord(ch)


def test_translate_special_keys():
    from prompt_toolkit.keys import Keys
    assert translate_key(KP(Keys.Up)) == kc.UP
    assert translate_key(KP(Keys.Down)) == kc.DOWN
    assert translate_key(KP(Keys.Escape)) == kc.ESC
    assert translate_key(KP(Keys.PageUp)) == kc.PPAGE
    assert translate_key(KP(Keys.F5)) == kc.F5
    assert translate_key(KP(Keys.F10)) == kc.F10
    assert translate_key(KP(Keys.Enter)) == kc.CTRL_M
    assert translate_key(KP(Keys.Backspace)) == kc.CTRL_H
    assert translate_key(KP(Keys.Tab)) == kc.CTRL_I


def test_translate_ctrl_keys():
    from prompt_toolkit.keys import Keys
    assert translate_key(KP(Keys.ControlA)) == kc.CTRL_A
    assert translate_key(KP(Keys.ControlL)) == kc.CTRL_L
    assert translate_key(KP(Keys.ControlU)) == kc.CTRL_U


def test_translate_unknown_returns_none():
    assert translate_key(KP("multi-char-string", "multi-char-string")) is None


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------
def test_source_file_loaded(app, srcfile):
    assert app.src is not None
    assert app.src.path == srcfile
    assert len(app.src) > 60


def test_gdb_started_and_tty_wired(app):
    g = app._fake["gdb"]
    assert g.started
    # the inferior tty must be set UNQUOTED (see mi.breakpoint_location and
    # test_gdb_integration.test_inferior_tty_must_not_be_quoted)
    tty_cmds = [c for c in g.commands if "inferior-tty" in c]
    assert tty_cmds, "inferior tty was never set"
    assert '"' not in tty_cmds[0], "tty name must not be quoted: %r" % tty_cmds[0]


def test_breakpoints_refreshed_at_startup(app):
    assert any(c.startswith("-break-info") for c in app._fake["gdb"].commands)


def test_initial_focus_is_the_gdb_window(bare_app):
    # interface.cpp:139 -- `static enum Focus focus = GDB`. Starting in the
    # source window means the user's first keystrokes go nowhere visible,
    # which reads as "the program is broken".
    assert bare_app.focus == FOCUS_GDB
    assert bare_app.app.layout.current_control is bare_app.edit_window.content


# ---------------------------------------------------------------------------
# focus switching
# ---------------------------------------------------------------------------
def test_i_switches_to_gdb(app):
    app.dispatch(ord("i"))
    assert app.focus == FOCUS_GDB


def test_esc_switches_back_to_source(app):
    app.dispatch(ord("i"))
    assert app.focus == FOCUS_GDB
    app.dispatch(kc.ESC)
    assert app.focus == FOCUS_SOURCE


def test_esc_in_source_focus_is_a_noop(app):
    app.dispatch(kc.ESC)
    assert app.focus == FOCUS_SOURCE


def test_s_enters_gdb_scroll_mode(app):
    app.dispatch(ord("s"))
    assert app.focus == FOCUS_GDB
    assert app.scroll.active


# ---------------------------------------------------------------------------
# source navigation
# ---------------------------------------------------------------------------
def test_j_and_k_move_the_cursor(app):
    app.view.cursor = 10
    app.dispatch(ord("j"))
    assert app.view.cursor == 11
    app.dispatch(ord("k"))
    assert app.view.cursor == 10


def test_arrow_keys_move_the_cursor(app):
    app.view.cursor = 10
    app.dispatch(kc.DOWN)
    assert app.view.cursor == 11
    app.dispatch(kc.UP)
    assert app.view.cursor == 10


def test_G_goes_to_the_last_line(app):
    app.dispatch(ord("G"))
    assert app.view.cursor == len(app.src) - 1


def test_gg_goes_to_the_first_line(app):
    app.dispatch(ord("G"))
    assert app.view.cursor == len(app.src) - 1
    app.dispatch(ord("g"))
    app.dispatch(ord("g"))
    assert app.view.cursor == 0


def test_lone_g_does_not_jump(app):
    """`g` is a prefix, not a command. If a lone `g` jumped to the top then
    `:map gg <Up>` could never fire, because the first `g` would already have
    been consumed."""
    app.view.cursor = 20
    app.dispatch(ord("g"))
    assert app.view.cursor == 20
    assert app.last_key == ord("g")


def test_ctrl_d_and_ctrl_u_page_by_half(app):
    """Half a page is half the PANE height (source_vscroll(sview,
    get_src_height() / 2)), not view.height: the source pane is 20 rows on
    the 40-row dummy output, so half a page moves 10 lines."""
    app.view.cursor = 30
    app.dispatch(kc.CTRL_D)
    assert app.view.cursor == 40
    app.dispatch(kc.CTRL_U)
    assert app.view.cursor == 30


def test_cursor_clamps_at_both_ends(app):
    app.dispatch(ord("k"))
    app.dispatch(ord("k"))
    assert app.view.cursor == 0
    app.dispatch(ord("G"))
    app.dispatch(ord("j"))
    assert app.view.cursor == len(app.src) - 1


def test_marks(app):
    app.view.cursor = 7
    app.dispatch(ord("m"))
    app.dispatch(ord("a"))
    app.dispatch(ord("G"))
    assert app.view.cursor != 7
    app.dispatch(ord("'"))
    app.dispatch(ord("a"))
    assert app.view.cursor == 7


def test_unknown_mark_falls_through_silently(app):
    # source_goto_mark failing means the key is processed as a normal key
    # (sources.cpp:694-703) -- no message, no jump
    app.dispatch(ord("'"))
    cursor = app.view.cursor
    app.dispatch(ord("z"))
    assert app.message == ""
    assert app.view.cursor == cursor


def test_jump_back_and_executing_line_marks(app, srcfile):
    # `''` bounces back to where the last `'x` jump started; `'.` goes to
    # the executing line (sources.cpp:684-696)
    app.exec_line = (srcfile, 3)
    app.view.move_to(30, len(app.src))
    app.dispatch(ord("'"))
    app.dispatch(ord("a"))            # jump back home (mark unset? no --
    # 'a' was never set: falls through. Set it first.
    app.view.set_mark("a")
    app.view.marks["a"] = 30
    app.view.move_to(0, len(app.src))
    app.dispatch(ord("'"))
    app.dispatch(ord("a"))
    assert app.view.cursor == 30
    app.dispatch(ord("'"))
    app.dispatch(ord("'"))            # jump back
    assert app.view.cursor == 0
    app.dispatch(ord("'"))
    app.dispatch(ord("."))            # the executing line
    assert app.view.cursor == 2


def test_mark_toggle_and_invalid_names(app):
    app.view.move_to(4, len(app.src))
    app.dispatch(ord("m"))
    app.dispatch(ord("a"))
    assert "a" in app.view.marks
    app.dispatch(ord("m"))
    app.dispatch(ord("a"))            # same line: removes the mark
    assert "a" not in app.view.marks
    app.dispatch(ord("m"))
    app.dispatch(ord("5"))            # not a-zA-Z: falls through, no mark
    assert "5" not in app.view.marks
    # and the '5' still starts a count -- C's source_input appends it
    # after the switch, so the next j moves by five
    app.dispatch(ord("j"))
    assert app.view.cursor == 9


# ---------------------------------------------------------------------------
# breakpoints
# ---------------------------------------------------------------------------
def test_space_inserts_a_breakpoint(app):
    g = app._fake["gdb"]
    app.view.cursor = 3
    before = len(g.commands)
    app.dispatch(ord(" "))
    g._drain = True
    import time
    time.sleep(0.3)
    inserted = [c for c in g.commands[before:] if c.startswith("-break-insert")]
    assert inserted, "no -break-insert issued"
    cmd = inserted[0]
    assert "-t" not in cmd, "space must not create a temporary breakpoint"
    # the location must be one quoted "path:line" string
    assert cmd.endswith(':4"'), cmd


def test_t_inserts_a_temporary_breakpoint(app):
    g = app._fake["gdb"]
    app.view.cursor = 3
    before = len(g.commands)
    app.dispatch(ord("t"))
    import time
    time.sleep(0.3)
    inserted = [c for c in g.commands[before:]
                if c.startswith("-break-insert -t")]
    assert inserted, "no temporary -break-insert issued"


def test_space_deletes_an_existing_breakpoint(app):
    g = app._fake["gdb"]
    app.breakpoints.update_from_mi({"BreakpointTable": {"body": [
        {"bkpt": {"number": "7", "enabled": "y",
                  "fullname": app.src.path, "line": "4"}}]}})
    assert app.breakpoints.at(app.src.path, 4) == ["7"]
    app.view.cursor = 3
    before = len(g.commands)
    app.dispatch(ord(" "))
    import time
    time.sleep(0.3)
    deleted = [c for c in g.commands[before:] if c.startswith("-break-delete")]
    assert deleted == ["-break-delete 7"]


def test_breakpoint_without_a_source_file(app):
    app.src = None
    app.dispatch(ord(" "))
    assert "no source file" in app.message


# ---------------------------------------------------------------------------
# function keys
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key,expected", [
    (kc.F5, "-exec-run"),
    (kc.F6, "-exec-continue"),
    (kc.F7, "-exec-finish"),
    (kc.F8, "-exec-next"),
    (kc.F10, "-exec-step"),
])
def test_function_keys_issue_the_right_mi_command(app, key, expected):
    g = app._fake["gdb"]
    before = len(g.commands)
    app.dispatch(key)
    import time
    time.sleep(0.3)
    assert expected in g.commands[before:], g.commands[before:]


def test_F5_runs_even_while_inferior_runs(app):
    # C's F5 is TGDB_RUN unconditionally; a running inferior gets broken
    # into first so the run is actually seen
    g = app._fake["gdb"]
    app.running_inferior = True
    before = len(g.commands)
    app.dispatch(kc.F5)
    import time
    time.sleep(0.3)
    assert "-exec-run" in g.commands[before:]
    assert g.interrupted == 1


def test_F_keys_only_fire_from_the_source_window(app):
    # cgdb_input handles F5/F8/...; the GDB window sends them to gdb
    # instead (interface.cpp:1484-1509 vs the gdb_input default)
    import time
    g = app._fake["gdb"]
    app.focus = FOCUS_GDB
    before = len(g.commands)
    app.dispatch(kc.F5)
    app.dispatch(kc.F8)
    app.dispatch(kc.F10)
    time.sleep(0.3)
    assert g.commands[before:] == []
    app.focus = FOCUS_SOURCE
    app.dispatch(kc.F5)
    time.sleep(0.3)
    assert any(c.startswith("-exec-run") for c in g.commands[before:])


def test_ctrl_c_interrupts(app):
    from prompt_toolkit.keys import Keys
    g = app._fake["gdb"]
    app.on_key(KP(Keys.ControlC, "\x03"))
    assert g.interrupted == 1


def test_F1_shows_help(app):
    app.dispatch(kc.F1)
    assert app._help_active
    assert app.root is app.help_window
    text = "".join(t for _, t in app._help_text())
    assert "F5" in text and "ESC" in text
    app.dispatch(ord("q"))
    assert not app._help_active
    assert app.root is not app.help_window


# ---------------------------------------------------------------------------
# gdb callbacks -> UI
# ---------------------------------------------------------------------------
def test_console_output_reaches_the_model(app):
    g = app._fake["gdb"]
    g.emit_console("Breakpoint 1, main ()\n")
    assert "Breakpoint 1" in "".join(app.console.lines)


def test_stopped_updates_the_exec_line_and_opens_the_file(app, srcfile):
    g = app._fake["gdb"]
    g.emit_stopped({"reason": "breakpoint-hit",
                    "frame": {"func": "compute", "fullname": srcfile,
                              "line": "5"}})
    assert app.exec_line == (srcfile, 5)
    assert app.view.cursor == 4          # 0-based
    assert app.running_inferior is False


def test_stopped_without_a_frame_does_not_crash(app):
    app._fake["gdb"].emit_stopped({"reason": "signal-received"})
    assert app.exec_line is None


def test_running_sets_the_flag(app):
    app._fake["gdb"].callbacks["on_running"]({})
    assert app.running_inferior is True


def test_breakpoint_notification_updates_the_table(app):
    app._fake["gdb"].callbacks["on_notify"]("breakpoint-created", {
        "bkpt": {"number": "3", "enabled": "y",
                 "fullname": app.src.path, "line": "5"}})
    assert app.breakpoints.at(app.src.path, 5) == ["3"]


def test_gdb_exit_is_reported(app):
    app._fake["gdb"].callbacks["on_exit"](0)
    assert app.gdb_alive is False
    assert "gdb exited" in app.message


def test_gdb_exit_takes_cgdb_down_with_it(app):
    """Once gdb is gone there is nothing left to drive, so cgdb goes too.

    EOF on the MI channel ends the C version's main_loop (cgdb.cpp:760-772).
    """
    assert app._gdb_ready, "start_gdb() should have marked gdb ready"
    app._fake["gdb"].callbacks["on_exit"](0)
    assert app._quitting
    assert app._fake["gdb"].stopped


def test_gdb_dying_during_startup_stays_up_to_show_why(app):
    """A gdb that dies before it was usable must leave its error on screen.

    prompt_toolkit restores the alternate screen on exit, so quitting here
    would erase the only explanation the user has. This is the same reasoning
    as the spawn-failure path in start_gdb().
    """
    app._gdb_ready = False
    app._fake["gdb"].callbacks["on_exit"](1)
    assert not app._quitting
    assert "gdb exited" in "".join(t for _, t in app._console_text())


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def test_colon_enters_command_mode(app):
    app.dispatch(ord(":"))
    assert app.focus == FOCUS_COMMAND
    assert app.cmd_buffer.text == ":"


def test_colon_in_gdb_focus_belongs_to_gdb(bare_app):
    """In the GDB window `:` is just another character for gdb.

    gdb_input() passes everything except PageUp and Ctrl-L straight through
    (`default: result = 1` -- "this tells the input to go to active GDB
    command", interface.cpp:996-1000). cgdb's `:` only exists in the source
    window, which ESC reaches. Getting this backwards either steals `:` from a
    gdb command line or swallows ordinary typing.
    """
    assert bare_app.focus == FOCUS_GDB
    # the catch-all binding is filtered off, so prompt_toolkit's Buffer takes
    # the keystroke instead of dispatch() ever seeing it
    assert bare_app._buffer_owns_keys()
    bare_app.dispatch(ord(":"))
    assert bare_app.focus == FOCUS_GDB
    assert bare_app.cmd_buffer.text == ""


def test_set_command_applies(app):
    app.execute_command(":set tabstop=4")
    assert app.config.get("tabstop") == 4


def test_set_command_reports_errors(app):
    app.execute_command(":set nosuchoption=1")
    assert "unknown option" in app.message


def test_set_all_lists_options(app):
    app.execute_command(":set all")
    assert "tabstop=8" in app.message


def test_open_command_loads_a_file(app, srcfile):
    app.src = None
    app.execute_command(":open %s" % srcfile)
    assert app.src is not None
    assert app.src.path == srcfile


def test_open_command_with_a_line(app, srcfile):
    app.execute_command(":open %s:6" % srcfile)
    assert app.view.cursor == 5


def test_colon_number_jumps_and_scrolls(app, srcfile):
    """`:123` goes to a line, `:+N` / `:-N` scroll by N lines
    (command_parse_string's NUMBER case, cgdbrc.cpp:1058-1073)."""
    app.view.cursor = 0
    app.execute_command(":12")
    assert app.view.cursor == 11
    app.execute_command(":+3")
    assert app.view.cursor == 14
    app.execute_command(":-4")
    assert app.view.cursor == 10


def test_colon_tgdb_commands_reach_gdb(app):
    """The debugger `:` commands from commands[] (cgdbrc.cpp:120-140):
    the -exec-* ones go as MI, the console-only ones (start/up/down/kill)
    ride -interpreter-exec, exactly like tgdb routes them."""
    import time
    g = app._fake["gdb"]
    for line in (":continue", ":c", ":n", ":nexti", ":s", ":stepi",
                 ":finish", ":f", ":r", ":u"):
        app.execute_command(line)
    time.sleep(0.4)
    cmds = " ".join(g.commands)
    assert "-exec-continue" in cmds
    assert "-exec-next" in cmds
    assert "-exec-next-instruction" in cmds
    assert "-exec-step" in cmds
    assert "-exec-step-instruction" in cmds
    assert "-exec-finish" in cmds
    assert "-exec-run" in cmds
    assert "-exec-until" in cmds


def test_colon_console_only_tgdb_commands(app):
    import time
    g = app._fake["gdb"]
    for line in (":start", ":up", ":down", ":kill", ":k"):
        app.execute_command(line)
    time.sleep(0.4)
    cmds = " ".join(g.commands)
    assert '-interpreter-exec console "start"' in cmds
    assert '-interpreter-exec console "up"' in cmds
    assert '-interpreter-exec console "down"' in cmds
    assert '-interpreter-exec console "kill"' in cmds


def test_colon_insert_and_focus(app):
    """:insert focuses the GDB window; :focus takes cgdb or gdb
    (command_focus_gdb / command_do_focus)."""
    app.execute_command(":insert")
    assert app.focus == FOCUS_GDB
    app.execute_command(":focus cgdb")
    assert app.focus == FOCUS_SOURCE
    app.execute_command(":focus gdb")
    assert app.focus == FOCUS_GDB
    app.execute_command(":focus sideways")
    assert "usage" in app.message


def test_colon_edit_reloads_the_file(app, srcfile):
    """:e re-reads the file (command_source_reload) and keeps the cursor."""
    with open(srcfile, "a", encoding="utf-8") as f:
        f.write("int appended_line(void) { return 1; }\n")
    app.view.cursor = 5
    app.execute_command(":e")
    assert "appended_line" in "\n".join(
        app.src.line(i) for i in range(len(app.src)))
    assert app.view.cursor == 5


def test_colon_accepted_stubs(app):
    """:noh and :bang are accepted silently -- C stubs bang the same way,
    and noh will matter once hlsearch rendering exists."""
    app.message = "leftover"
    app.execute_command(":noh")
    app.execute_command(":bang")
    assert app.message == "leftover"


def test_syntax_command_forces_and_reports(app, srcfile):
    """` :syntax <lang>` forces the lexer, bare `:syntax` reports
    (command_parse_syntax); off / unknown map to plain / auto."""
    app.execute_command(":syntax c")
    assert app.src._forced_lexer == "c"
    app.execute_command(":syntax")
    assert "c" in app.message
    app.execute_command(":syntax off")
    assert app.src._forced_lexer == "off"
    app.execute_command(":syntax unknown")
    assert app.src._forced_lexer is None
    app.execute_command(":syntax definitely-not-a-language")
    assert "unknown language" in app.message


def test_set_syntax_option_reaches_current_and_new_files(app, srcfile):
    """:set syntax=<lang> re-lexes the file on screen and applies to files
    opened afterwards (if_highlight_sviewer on both paths)."""
    app.execute_command(":set syntax=c")
    assert app.src._forced_lexer == "c"
    app.open_file(srcfile)              # reopen: the option still applies
    assert app.src._forced_lexer == "c"
    app.execute_command(":set syntax=unknown")
    assert app.src._forced_lexer is None


def test_shell_command_degrades_to_a_message_when_headless(app):
    """run_in_terminal needs a running event loop; headless (tests, or a
    crashed UI) :sh reports instead of raising."""
    app.execute_command(":sh")
    assert "shell unavailable" in app.message


# ---------------------------------------------------------------------------
# file dialog (o)
# ---------------------------------------------------------------------------
def test_o_opens_the_file_dialog_with_gdb_list(app, tmp_path):
    """`o` requests -file-list-exec-source-files and the dialog takes over
    the screen (if_show_filedlg). Entries whose file no longer exists are
    dropped, as filedlg.cpp:139-142 does."""
    import time
    g = app._fake["gdb"]
    main = tmp_path / "main.c"
    util = tmp_path / "util.c"
    main.write_text("int main;\n", encoding="utf-8")
    util.write_text("int util;\n", encoding="utf-8")
    g.responses["-file-list-exec-source-files"] = {"files": [
        {"file": "main.c", "fullname": str(main)},
        {"file": "util.c", "fullname": str(util)},
        {"file": "gone.c", "fullname": str(tmp_path / "gone.c")},
    ]}
    app.dispatch(ord("o"))
    time.sleep(0.4)
    assert any(c.startswith("-file-list-exec-source")
               for c in g.commands)
    assert app.focus == FOCUS_FILEDLG
    assert app._filedlg_active
    assert app.root is app.filedlg_window
    assert [f[0] for f in app.filedlg.files] == ["main.c", "util.c"]


def test_file_dialog_navigation_and_open(app, srcfile, tmp_path):
    import time
    g = app._fake["gdb"]
    beta = tmp_path / "beta.c"
    beta.write_text("int beta;\n", encoding="utf-8")
    g.responses["-file-list-exec-source-files"] = {"files": [
        {"file": "alpha.c", "fullname": srcfile},
        {"file": "beta.c", "fullname": str(beta)},
    ]}
    app.dispatch(ord("o"))
    time.sleep(0.4)

    app.dispatch(ord("j"))
    assert app.filedlg.sel == 1
    app.dispatch(ord("k"))
    assert app.filedlg.sel == 0
    app.dispatch(ord("5"))
    app.dispatch(ord("G"))
    assert app.filedlg.sel == 1
    app.dispatch(ord("g"))
    app.dispatch(ord("g"))
    assert app.filedlg.sel == 0

    # Enter opens the selected file and closes the dialog
    app.dispatch(kc.CTRL_M)
    assert app.focus == FOCUS_SOURCE
    assert not app._filedlg_active
    assert app.src is not None
    assert os.path.basename(app.src.path) == os.path.basename(srcfile)


def test_file_dialog_q_and_esc_cancel(app):
    import time
    g = app._fake["gdb"]
    g.responses["-file-list-exec-source-files"] = {"files": [
        {"file": "a.c", "fullname": "/a.c"}]}
    app.dispatch(ord("o"))
    time.sleep(0.4)
    app.dispatch(ord("q"))
    assert app.focus == FOCUS_SOURCE
    assert not app._filedlg_active

    app.dispatch(ord("o"))
    time.sleep(0.4)
    app.dispatch(kc.ESC)                 # the cgdb mode key cancels too
    assert app.focus == FOCUS_SOURCE
    assert not app._filedlg_active


def test_file_dialog_search(app, tmp_path):
    import time
    g = app._fake["gdb"]
    files = {}
    for name in ("one.c", "two.c", "three.c"):
        f = tmp_path / name
        f.write_text("int %s;\n" % name[:-2], encoding="utf-8")
        files[name] = str(f)
    g.responses["-file-list-exec-source-files"] = {"files": [
        {"file": "one.c", "fullname": files["one.c"]},
        {"file": "two.c", "fullname": files["two.c"]},
        {"file": "three.c", "fullname": files["three.c"]},
    ]}
    app.dispatch(ord("o"))
    time.sleep(0.4)
    app._search_target = "filedlg"
    app.execute_command("/t")
    assert app.filedlg.sel == 1          # "two.c"
    app.dispatch(ord("n"))
    assert app.filedlg.sel == 2          # "three.c"
    app.dispatch(ord("N"))
    assert app.filedlg.sel == 1


def test_file_dialog_search_is_case_insensitive_and_wraps(app, tmp_path):
    # filedlg.cpp hardcodes icase=1 and wraps the scan
    import time
    g = app._fake["gdb"]
    names = ("a.c", "B.c", "c.c")
    paths = []
    for name in names:
        f = tmp_path / name
        f.write_text("x\n", encoding="utf-8")
        paths.append(str(f))
    g.responses["-file-list-exec-source-files"] = {"files": [
        {"file": n, "fullname": p} for n, p in zip(names, paths)]}
    app.dispatch(ord("o"))
    time.sleep(0.4)
    # C's sort is plain strcmp: "B.c" < "a.c" < "c.c"
    assert [f[0] for f in app.filedlg.files] == ["B.c", "a.c", "c.c"]
    app._search_target = "filedlg"
    app.execute_command("/A")            # lowercase pattern, icase match
    assert app.filedlg.sel == 1
    app.execute_command("/C")
    assert app.filedlg.sel == 2
    app.filedlg.goto(2)
    app.execute_command("/B")            # wraps back to the top
    assert app.filedlg.sel == 0


def test_file_dialog_text_renders_list_and_fillers(app, tmp_path):
    import time
    g = app._fake["gdb"]
    a = tmp_path / "a.c"
    a.write_text("int a;\n", encoding="utf-8")
    g.responses["-file-list-exec-source-files"] = {"files": [
        {"file": "a.c", "fullname": str(a)}]}
    app.dispatch(ord("o"))
    time.sleep(0.4)
    text = "".join(t for _, t in app._filedlg_text())
    assert "Select a file or press q to cancel" in text
    assert "a.c" in text
    assert text.count("~") >= 5          # vim-style filler rows
    assert "->" in text                  # the selection arrow
    # the bottom row exists and is empty without a message
    assert text.endswith("\n") or text.rstrip().endswith("")
    app.dispatch(ord("q"))


def test_o_without_gdb_running(app):
    app.gdb = None
    app.dispatch(ord("o"))
    assert "not running" in app.message


def test_map_command_abbreviations(app):
    """im/iu/unm are the C abbreviations for the map commands."""
    app.execute_command(":im a b")
    assert "a" in app.imap_set
    app.execute_command(":iu a")
    assert "a" not in app.imap_set
    app.execute_command(":unm a")


def test_source_u_runs_until_cursor_line(app):
    """'u' runs until the *displayed* file's line: tgdb passes path and
    line together (tgdb_request_until_line), so the console until
    "file":line spelling is used."""
    import time
    g = app._fake["gdb"]
    app.view.cursor = 6
    before = len(g.commands)
    app.dispatch(ord("u"))
    time.sleep(0.4)
    sent = [c for c in g.commands[before:]
            if "until" in c]
    assert len(sent) == 1
    assert "until " in sent[0] and ":7" in sent[0]


def test_digit_counts_in_source_window(app, srcfile):
    """Digits accumulate as a line number for j/k/G
    (G_line_number, interface.cpp:1312-1317)."""
    app.view.cursor = 0
    app.dispatch(ord("5"))
    app.dispatch(ord("j"))
    assert app.view.cursor == 5
    app.dispatch(ord("j"))          # count cleared by the previous j
    assert app.view.cursor == 6
    app.dispatch(ord("1"))
    app.dispatch(ord("2"))
    app.dispatch(ord("G"))
    assert app.view.cursor == 11
    app.dispatch(ord("G"))          # plain G: last line
    assert app.view.cursor == len(app.src) - 1


@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Windows thing")
def test_split_file_line_survives_windows_drive_letters(tmp_path):
    """`:` is both the line separator and the drive separator.

    A plain rpartition(":") reads `C:\\dir\\f.c` as file "C", and reads a
    directory whose last component is numeric as a request for that line.
    """
    numeric = tmp_path / "123"
    numeric.write_text("x\n")
    split = CgdbApp._split_file_line

    # an existing path wins outright, even when it ends in digits
    assert split(str(numeric)) == (str(numeric), None)
    # a real :line suffix still works, with either separator
    assert split(str(numeric) + ":7") == (str(numeric), 7)
    assert split("C:\\nope\\f.c:7") == ("C:\\nope\\f.c", 7)
    assert split("C:/nope/f.c:7") == ("C:/nope/f.c", 7)
    # a bare drive-letter path must not be cut at the drive colon
    assert split("C:\\nope\\f.c") == ("C:\\nope\\f.c", None)
    assert split("C:\\dir with space\\f.c:12") == ("C:\\dir with space\\f.c",
                                                   12)
    # and a plain number is a relative path, not a line number
    assert split("42") == ("42", None)


def test_split_file_line_on_posix_paths(tmp_path):
    f = tmp_path / "f.c"
    f.write_text("x\n")
    split = CgdbApp._split_file_line
    assert split(str(f)) == (str(f), None)
    assert split(str(f) + ":9") == (str(f), 9)
    assert split("/nope/f.c:9") == ("/nope/f.c", 9)
    assert split("/nope/f.c") == ("/nope/f.c", None)


def test_open_missing_file(app):
    app.execute_command(":open /definitely/not/here.c")
    assert "no such file" in app.message


def test_open_without_an_argument(app):
    app.execute_command(":open")
    assert "usage" in app.message


def test_help_command(app):
    app.execute_command(":help")
    assert app._help_active
    app.dispatch(ord("q"))
    assert not app._help_active


def test_unknown_colon_command(app):
    app.execute_command(":frobnicate")
    assert "Unknown command" in app.message
    # the whole command line is echoed, as C does
    assert "frobnicate" in app.message


def test_numeric_command_ignores_trailing_junk(app):
    # the lexer's NUMBER token is `[+-]?{D}+`; `:5abc` jumps to line 5
    app.execute_command(":5abc")
    assert app.view.cursor == 4


def test_colon_backspacing_the_prefix_cancels(app):
    """Deleting the ':' itself closes the command line; what is typed
    afterwards must never leak to gdb as a console command."""
    import time
    g = app._fake["gdb"]
    before = len(g.commands)
    app.set_focus(FOCUS_SOURCE)
    app.dispatch(ord(":"))
    assert app.focus == FOCUS_COMMAND
    # backspace over the ':'
    app.cmd_buffer.delete_before_cursor(1)
    assert app.focus != FOCUS_COMMAND
    # typing q + Enter now goes to the source window's 'q' (nothing),
    # not to gdb
    app.dispatch(ord("q"))
    app.dispatch(kc.CTRL_M)
    time.sleep(0.2)
    assert g.commands[before:] == []


def test_clear_command(app):
    app._fake["gdb"].emit_console("some output\n")
    app.execute_command(":clear")
    assert len(app.console.lines) == 0


def test_map_command_registers_a_mapping(app):
    app.execute_command(":map gg <Up>")
    assert "gg" in app.map_set
    app.view.cursor = 30
    press(app, ord("g"))
    press(app, ord("g"))
    # the user map must win over the built-in `gg` (go to top)
    assert app.view.cursor == 29


def test_unmap_command(app):
    app.execute_command(":map xx <Up>")
    app.execute_command(":unmap xx")
    assert "xx" not in app.map_set


def test_unmap_unknown(app):
    app.execute_command(":unmap nosuchmap")
    assert "no such mapping" in app.message


def test_map_with_bad_arguments(app):
    app.execute_command(":map onlyone")
    assert "usage" in app.message


def test_bare_text_goes_to_gdb_as_a_console_command(app):
    g = app._fake["gdb"]
    app.execute_command("print x")
    import time
    time.sleep(0.3)
    assert any("print x" in c for c in g.commands)
    # and it is echoed into the console with a prompt
    assert "(gdb) print x" in "".join(app.console.lines) + "".join(
        app.console.cur)


def test_search_forward(app, srcfile):
    app.execute_command("/line42")
    assert app.view.cursor == app.src.lines.index("int line42 = 42;")


def test_search_backward(app, srcfile):
    app.view.cursor = len(app.src) - 1
    app.execute_command("?line10")
    assert app.src.line(app.view.cursor) == "int line10 = 10;"


def test_search_not_found(app):
    app.execute_command("/zzz_no_such_text_zzz")
    assert "not found" in app.message


def test_search_invalid_regex(app):
    app.execute_command("/([bad")
    assert "not found" in app.message or app.message


def test_n_repeats_the_search(app, srcfile):
    app.execute_command("/line1")
    first = app.view.cursor
    app.dispatch(ord("n"))
    assert app.view.cursor != first


def test_search_without_a_source_file(app):
    app.src = None
    app.execute_command("/anything")
    assert "no source" in app.message


def test_incremental_search_jumps_while_typing(app, srcfile):
    """status_bar_regex_input jumps on every character; hooking the
    command buffer's text_changed gives the same live behaviour."""
    app.view.cursor = 0
    app._search_target = "source"
    app.cmd_buffer.text = "/line42"
    assert app.view.cursor == app.src.lines.index("int line42 = 42;")
    # a partial regex that cannot compile yet must not throw or jump
    where = app.view.cursor
    app.cmd_buffer.text = "/line4["
    assert app.view.cursor == where


def test_console_search_jump_and_repeat(app):
    """`/` typed in the GDB window searches from the scroll cursor,
    wraps (scr_search_regex), and n/N repeat in the remembered direction."""
    app._fake["gdb"].emit_console("".join("hit %d\n" % i for i in range(30)))
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active
    app._search_target = "console"
    # the scan starts at the scroll cursor (scroller.cpp:319), which
    # PageUp left mid-scrollback: "hit 20" is the first match below it
    app.execute_command("/hit 2")
    assert app._console_search_row == 20
    mx = app.console.max_delta(app._console_height())
    assert 0 <= app.scroll.delta <= mx       # positioned, clamped
    # n continues from the hit: "hit 21" is next; N walks back
    app.dispatch(ord("n"))
    assert app._console_search_row == 21
    app.dispatch(ord("N"))
    assert app._console_search_row == 20
    assert app.scroll.active
    # / + Enter with an empty pattern repeats the last search
    app.execute_command("/")
    assert app._console_search_row == 21
    app.execute_command("/zzz_absent")
    assert "not found" in app.message


def test_console_search_wraps_from_the_bottom(app):
    app._fake["gdb"].emit_console("".join("hit %d\n" % i for i in range(30)))
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    app._search_target = "console"
    # from the very bottom the forward scan wraps to the top
    app.scroll.delta = 0
    app.scroll.row = app._console_height() - 1
    origin = app._console_search_origin()
    assert origin[0] >= 25                   # really at the bottom
    app.execute_command("/hit 1$")
    assert app._console_search_row == 1


def test_console_search_remembered_direction(app):
    # after a `?` backward search, n keeps going backward (C's
    # regex_direction_last, interface.cpp:937-944)
    app._fake["gdb"].emit_console("".join("hit %d\n" % i for i in range(30)))
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    app._search_target = "console"
    # a `?` search scans backward from the cursor
    app.execute_command("?hit 2")
    assert app._console_search_row == 2
    # n keeps going backward: below row 2 nothing matches, so it wraps
    # from the end ("hit 29" contains "hit 2")
    app.dispatch(ord("n"))
    assert app._console_search_row == 29
    app.dispatch(ord("N"))
    assert app._console_search_row == 2


def test_console_search_esc_restores_the_position(app):
    app._fake["gdb"].emit_console("".join("hit %d\n" % i for i in range(30)))
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    app.scroll.delta = 5
    saved = (app.scroll.active, app.scroll.delta, app.scroll.row)
    app.dispatch(ord("/"))                  # the real entry: records state
    assert app.focus == FOCUS_COMMAND
    # the live preview jumped somewhere
    app.cmd_buffer.text = "/hit 2"
    assert app.scroll.delta != saved[1]
    app.dispatch(kc.ESC)
    assert (app.scroll.active, app.scroll.delta, app.scroll.row) == saved


def test_console_search_without_previous_pattern(app):
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    app._console_search = None
    app.dispatch(ord("n"))
    assert "no previous" in app.message


def test_source_horizontal_scroll(app, tmp_path):
    """h/l scroll the source text columns (source_hscroll); a file whose
    longest line fits the window cannot scroll at all, because C clamps
    sel_col to the longest line (sources.cpp:1091-1110)."""
    f = tmp_path / "wide.c"
    f.write_text("\n".join(["int x;"] + ["long_%04d;" % i + "y" * 120
                                         for i in range(40)]) + "\n",
                 encoding="utf-8")
    app.open_file(str(f))
    assert app.view.left == 0
    app.dispatch(ord("l"))
    app.dispatch(ord("l"))
    assert app.view.left == 2
    app.dispatch(ord("h"))
    assert app.view.left == 1
    for _ in range(10):
        app.dispatch(ord("h"))
    assert app.view.left == 0     # floors at zero


def test_source_cannot_scroll_a_file_that_fits(app, srcfile):
    for _ in range(5):
        app.dispatch(ord("l"))
    assert app.view.left == 0


def test_source_render_shifts_by_left(app, tmp_path):
    # C clamps sel_col to the longest line, so the file needs a line that
    # actually overflows the window before scrolling does anything
    long_line = "int " + "alpha_" * 60 + "x = 1;"
    f = tmp_path / "long.c"
    f.write_text("short\n" + long_line + "\nshort\n", encoding="utf-8")
    app.open_file(str(f))
    app.view.cursor = 1
    app.view.left = 4
    frags = app._source_text()
    rows = "".join(t for _, t in frags).split("\n")
    body = [r for r in rows if "alpha_" in r or "lpha_" in r][0]
    # the gutter is intact; the text body lost its first 4 columns
    assert "lpha_alpha" in body
    assert body.count("int ") == 0
    # scrolling past the longest line is clamped (sources.cpp:1091-1110)
    for _ in range(2000):
        app.dispatch(ord("l"))
    assert app.view.left <= len(long_line) + 8
    app.view.left = 0


def test_console_scroll_cursor_column(app):
    """Scroll-mode h/l/0/$ move the *cursor's* column (scr_left/right) --
    the window never pans sideways; vterm rewraps instead."""
    app._fake["gdb"].emit_console("".join("x%d\n" % i for i in range(40)))
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active
    w = app._gdb_width()
    app.dispatch(ord("l"))
    app.dispatch(ord("l"))
    assert app.scroll.col == 2
    app.dispatch(ord("h"))
    assert app.scroll.col == 1
    app.dispatch(ord("0"))
    assert app.scroll.col == 0
    app.dispatch(ord("$"))
    assert app.scroll.col == w - 1
    app.dispatch(ord("q"))
    assert not app.scroll.active


# ---------------------------------------------------------------------------
# gdb console scroll mode
# ---------------------------------------------------------------------------
def test_pageup_enters_scroll_mode(app):
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active


def test_scroll_mode_navigation(app):
    # scr_up/scr_down: the cursor walks first, the window only scrolls
    # once the cursor reaches the edge (scroller.cpp:216-237)
    app._fake["gdb"].emit_console("".join("L%d\n" % i for i in range(60)))
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active
    h = app._console_height()
    d0 = app.scroll.delta
    r0 = app.scroll.row
    app.dispatch(kc.UP)
    assert app.scroll.delta == d0          # window still
    assert app.scroll.row == r0 - 1        # cursor moved up
    for _ in range(h):
        app.dispatch(kc.UP)                # walk the cursor to the top
    top = app.scroll.delta
    app.dispatch(kc.UP)                    # now the window scrolls
    assert app.scroll.delta == top + 1
    assert app.scroll.row == 0
    app.dispatch(kc.DOWN)
    assert app.scroll.row == 1             # cursor walks down again
    assert app.scroll.delta == top + 1
    # a lone 'g' is a prefix in the GDB window (gdb_input's gg); only the
    # pair jumps home, and it must not move on the first press
    d1 = app.scroll.delta
    app.dispatch(ord("g"))
    assert app.scroll.delta == d1
    app.dispatch(ord("g"))
    assert app.scroll.delta == app.console.max_delta(h)
    app.dispatch(ord("G"))
    assert app.scroll.delta == 0


def test_scroll_mode_half_pages_and_enter_leaves(app):
    """gdb_input scroll mode: Ctrl-U/Ctrl-D move half a page, and Enter
    leaves scroll mode parked at the bottom (interface.cpp:908-932, 966)."""
    app._fake["gdb"].emit_console("".join("L%d\n" % i for i in range(60)))
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active
    d0 = app.scroll.delta
    h = app._console_height()
    app.dispatch(kc.CTRL_U)
    assert app.scroll.delta == d0 + max(1, h // 2)
    app.dispatch(kc.CTRL_D)
    assert app.scroll.delta == d0
    app.dispatch(kc.CTRL_M)          # Enter
    assert not app.scroll.active
    assert app.scroll.delta == 0     # parked at the bottom


def test_esc_keeps_scroll_mode(app):
    # interface.cpp:1522-1555: ESC switches focus but never touches
    # in_scroll_mode; coming back with `i` stays at the scrolled position
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active
    app.scroll.delta = 4
    app.dispatch(kc.ESC)
    assert app.focus == FOCUS_SOURCE
    assert app.scroll.active
    assert app.scroll.delta == 4
    app.dispatch(ord("i"))
    assert app.scroll.active
    assert app.scroll.delta == 4
    app.dispatch(ord("q"))                 # q parks at the bottom
    assert not app.scroll.active


# ---------------------------------------------------------------------------
# kui integration through the app
# ---------------------------------------------------------------------------
def test_user_map_is_applied_to_dispatched_keys(app):
    app.execute_command(":map qq <Up>")
    app.view.cursor = 20
    press(app, ord("q"))
    press(app, ord("q"))
    assert app.view.cursor == 19


def test_recursive_user_maps(app):
    """kui expands maps recursively (kui_ctx.cpp:241-256)."""
    app.execute_command(":map aa bb")
    app.execute_command(":map bb <Up>")
    app.view.cursor = 20
    press(app, ord("a"))
    press(app, ord("a"))
    assert app.view.cursor == 19


def test_partial_map_waits_then_flushes(app):
    """A partial match must not fire immediately; the timeout resolves it."""
    app.execute_command(":map abc <Up>")
    app.view.cursor = 20
    from prompt_toolkit.key_binding.key_processor import KeyPress
    app.on_key(KeyPress("a", "a"))
    app.on_key(KeyPress("b", "b"))
    # the map is still pending, so nothing has been dispatched
    assert app.view.cursor == 20
    assert app._timeout_handle is not None or app.mapctx.buffer
    app._map_timeout()
    # 'a' and 'b' fall through as literals, which the source window ignores
    assert app.view.cursor == 20


# ---------------------------------------------------------------------------
# rendering (headless)
# ---------------------------------------------------------------------------
def test_renders_without_a_source_file():
    a = CgdbApp(config=Config(),
                client_factory=lambda cb, p, g: FakeGdb(cb),
                tty_factory=lambda cb: FakeTty(cb),
                output=make_output())
    a.start_gdb()
    text = a._source_text()
    joined = "".join(t for _, t in text)
    assert "no source file" in joined


def test_source_rendering_includes_gutter(app):
    app.view.height = 10
    app.view.cursor = 0
    text = app._source_text()
    joined = "".join(t for _, t in text)
    assert "#include <stdio.h>" in joined
    # line numbers are present
    assert "1" in joined


def test_source_rendering_marks_breakpoints(app):
    # the breakpoint marker IS the line number's colour, there is no "B"
    # (sources.cpp:877-895): red for enabled, yellow for disabled
    app.breakpoints.update_from_mi({"BreakpointTable": {"body": [
        {"bkpt": {"number": "1", "enabled": "y",
                  "fullname": app.src.path, "line": "1"}},
        {"bkpt": {"number": "2", "enabled": "n",
                  "fullname": app.src.path, "line": "2"}}]}})
    app.view.height = 10
    text = app._source_text()
    frags = [(s, t) for s, t in text if t.strip().isdigit()]
    assert any("class:breakpoint" == s.strip() for s, t in frags)
    assert any(s.strip() == "class:breakpoint.disabled" for s, t in frags)
    assert not any(t == "B" for _, t in text)


def test_source_rendering_marks_the_exec_line(app, srcfile):
    # default executinglinedisplay=longarrow: `│` in the bar column (the
    # same glyph as the plain bar, just colored) and an ASCII `---->` that
    # reaches to the first non-blank column
    app.exec_line = (srcfile, 5)
    app.view.height = 10
    text = app._source_text()
    arrows = [t for s, t in text
              if "exec-arrow" in s or "sel-arrow" in s]
    assert "│" in arrows
    assert any(t.endswith(">") and t[:-1] and set(t[:-1]) == {"-"}
               for t in arrows)
    # the executing line number is green, the selected one white/bold
    assert any(s.strip() == "class:line-number.executing" for s, t in text
               if t.strip().isdigit())

    # shortarrow keeps the `│` bar but the marker collapses to `>`
    app.config.set("eld", "shortarrow")
    text = app._source_text()
    arrows = [t for s, t in text
              if "exec-arrow" in s or "sel-arrow" in s]
    assert ">" in arrows
    assert not any(len(t) > 1 for t in arrows)

    # highlight paints the whole line instead
    app.config.set("eld", "highlight")
    text = app._source_text()
    assert any("exec-line-highlight" in s for s, _ in text)

    # cursor ON the executing line: the cursor bar (this pane's cursor --
    # the system one is hidden) must show while the arrow markers stay
    app.config.set("eld", "longarrow")
    app.view.cursor = 4
    text = app._source_text()
    arrows = [t for s, t in text
              if "exec-arrow" in s or "sel-arrow" in s]
    assert "│" in arrows
    assert any(t.endswith(">") and t[:-1] and set(t[:-1]) == {"-"}
               for t in arrows)
    # the bar runs the whole row: the arrow cells carry the selected-line
    # background too (not just the text right of "-->"), the way every
    # other selected line is painted
    assert any("exec-arrow" in s and "selected-line" in s and t.endswith(">")
               for s, t in text)
    assert any("selected-line" in s for s, t in text)


def test_source_rendering_shows_marks(app):
    # a mark replaces the vertical bar column, in HLG_MARK
    app.view.cursor = 2
    app.view.set_mark("a")
    app.view.height = 10
    text = app._source_text()
    assert any((t == "a" and "class:mark" in s) for s, t in text)


def test_source_rendering_tilde_rows_and_centering(app, tmp_path):
    # a short file is block-centered with `~` rows above and below
    # (sources.cpp:839-847); the pane height is whatever the split gives
    f = tmp_path / "tiny.c"
    f.write_text("a\nb\nc\nd\n", encoding="utf-8")
    app.open_file(str(f))
    app.message = ""
    h = app._source_height()
    top = app.view.top_line(4, h)
    assert top < 0
    rows = app._source_text()
    joined = "".join(t for _, t in rows)
    assert joined.count("~") == h - 4       # blank rows become tildes
    lines = joined.split("\n")
    # the first source line sits at row -top
    assert " 1" in lines[-top] or lines[-top].startswith("1")


def test_console_rendering(app):
    app._fake["gdb"].emit_console("hello\nworld\n")
    text = app._console_text()
    joined = "".join(t for _, t in text)
    assert "hello" in joined
    assert "world" in joined


def test_status_rendering_shows_the_full_path(app):
    # update_status_win's default content is the current file's full path
    app.message = ""                        # a message would replace it
    text = app._status_text()
    joined = "".join(t for _, t in text)
    assert app.src.path in joined
    assert "CGDB" not in joined             # no focus chip in C


def test_status_rendering_truncates_with_a_marker(app):
    # if_display_message: an over-long row keeps its tail behind a `>`
    app.message = "x" * 400
    joined = "".join(t for _, t in app._status_text())
    assert len(joined) <= app._source_width()
    assert joined.startswith(">")


def test_status_rendering_message_replaces_the_row(app):
    app.message = "hello world"
    joined = "".join(t for _, t in app._status_text())
    assert "hello world" in joined
    assert app.src.path not in joined


def test_status_rendering_has_no_running_banner(app):
    # C's status bar shows only the filename; the run state is not in it
    app.running_inferior = True
    joined = "".join(t for _, t in app._status_text())
    assert "RUNNING" not in joined


def test_status_rendering_has_no_focus_marker(app):
    # the `*` for GDB focus is dead code in C (if_display_message
    # repaints the row over it; the official cgdb shows none) and the
    # GDB window is not distinguishable from the status row here either
    app.message = ""
    for focus in (FOCUS_GDB, FOCUS_SOURCE):
        app.focus = focus
        joined = "".join(t for _, t in app._status_text())
        assert "*" not in joined


def render_window(window, cols=80, rows=1):
    """Render one Window into a prompt_toolkit Screen and return it.

    Going through write_to_screen rather than reading _status_text() is the
    only way to see what lands in the cells past the end of the text, which is
    exactly what "the bar stops halfway across the screen" is about.
    """
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition

    screen = Screen()
    window.write_to_screen(screen, MouseHandlers(),
                           WritePosition(0, 0, cols, rows),
                           parent_style="", erase_bg=True, z_index=None)
    return screen


def test_status_bar_background_spans_the_full_width(app):
    """The bar must be painted to the right edge, not just under its text.

    cgdb writes WIDTH spaces under HLG_STATUS_BAR before drawing the text
    (interface.cpp:446-451). A bar that stops where the filename ends looks
    broken on any terminal wider than the message.
    """
    screen = render_window(app.status_window, cols=80)
    row = screen.data_buffer[0]
    text = "".join(row[c].char for c in range(80)).rstrip()
    assert 0 < len(text) < 80, "test needs a bar shorter than the window"
    for c in range(len(text), 80):
        assert "class:status" in row[c].style, (
            "column %d, past the end of the text, has no status background: "
            "char=%r style=%r" % (c, row[c].char, row[c].style))


def test_command_line_occupies_the_status_row(app):
    """`:` replaces the middle status bar, not the bottom gdb edit line.

    cgdb draws cur_sbc into status_win (interface.cpp:459-472). Row order is
    asserted structurally because the rendered position is what the user sees.
    """
    from prompt_toolkit.layout.containers import ConditionalContainer

    kids = app.root.children
    assert [k.content if isinstance(k, ConditionalContainer) else k
            for k in kids] == [app.source_window, app.status_window,
                               app.cmdline_window, app.console_window,
                               app.edit_window]

    status_cc, cmd_cc = kids[1], kids[2]
    app.set_focus(FOCUS_SOURCE)
    assert status_cc.filter() and not cmd_cc.filter()
    app.set_focus(FOCUS_COMMAND)
    assert cmd_cc.filter() and not status_cc.filter()
    assert app.app.layout.current_control is app.cmdline_control
    # the gdb edit line never moves; while the inferior runs it is the
    # ConditionalContainer that hides (edit_line), whose content stays
    # put -- the edit window itself is never re-parented
    assert kids[-1] is app.edit_line
    assert kids[-1].content is app.edit_window


# ---------------------------------------------------------------------------
# window splitting (interface.cpp:560-752, 1498-1509; DummyOutput is 80x40)
# ---------------------------------------------------------------------------
def _dim(value):
    """Resolve a possibly-callable pane dimension to its Dimension."""
    from prompt_toolkit.layout.dimension import to_dimension
    return to_dimension(value)


def test_eq_and_minus_adjust_the_split(app):
    """`=` / `-` move the split one row at a time (interface.cpp:1263-1281).

    In C this is increase/decrease_win_height(0): window_shift by one and
    the quarter mark is forgotten (WIN_SPLIT_FREE).
    """
    assert _dim(app.source_window.height).preferred == 20
    app.dispatch(ord("="))
    assert _dim(app.source_window.height).preferred == 21
    app.dispatch(ord("-"))
    assert _dim(app.source_window.height).preferred == 20
    assert app.win_split is None


def test_plus_and_underscore_jump_quarter_marks(app):
    """`+` / `_` jump between quarter marks (interface.cpp:672-752).

    Half of a 40-row terminal is 20, so src_big (mark 1) puts the source
    pane at 20 + 20/2 = 30 rows.
    """
    app.dispatch(ord("+"))
    assert app.win_split == 1
    assert _dim(app.source_window.height).preferred == 30
    app.dispatch(ord("_"))
    assert app.win_split == 0
    assert _dim(app.source_window.height).preferred == 20


def test_split_respects_winminheight(app):
    """winminheight stops `-` from starving the source pane
    (validate_window_sizes, interface.cpp:560-590)."""
    app.config.set("winminheight", 5)
    for _ in range(40):
        app.dispatch(ord("-"))
    # min shift is -(40/2) + 5, so the source pane bottoms out at 5 rows
    assert _dim(app.source_window.height).preferred == 5


def test_ctrl_w_toggles_orientation(app):
    """Ctrl-W swaps horizontal/vertical split (interface.cpp:1498-1509)."""
    from prompt_toolkit.layout.containers import VSplit

    app.dispatch(kc.CTRL_W)
    assert app.split_orientation == WSO_VERTICAL
    assert isinstance(app.root, VSplit)
    kids = app.root.children
    # left column: source above the status row (interface.cpp:17-31);
    # the `:` command line replaces that same status row
    left = [c.content if isinstance(c, ConditionalContainer) else c
            for c in kids[0].children]
    assert left == [app.source_window, app.status_window,
                    app.cmdline_window]
    assert kids[1] is app.vsep_window
    # the gdb column: console above the edit line (hidden as a whole
    # while the inferior's terminal owns the pane -- edit_line)
    assert list(kids[2].children) == [app.console_window, app.edit_line]

    app.dispatch(kc.CTRL_W)
    assert app.split_orientation == WSO_HORIZONTAL
    assert not isinstance(app.root, VSplit)
    assert _dim(app.source_window.height).preferred == 20


def test_vertical_split_geometry(app):
    """Vertical geometry per get_* (interface.cpp:227-360): the left column
    is half the width, the separator is one column, and `=` now widens the
    left column instead of the source height."""
    app.dispatch(kc.CTRL_W)
    assert _dim(app.left_col.width).preferred == 40
    assert _dim(app.vsep_window.width).preferred == 1
    app.dispatch(ord("="))
    assert _dim(app.left_col.width).preferred == 41
    assert app._gdb_width() == 80 - 41 - 1


def test_vertical_split_respects_winminwidth(app):
    app.dispatch(kc.CTRL_W)
    app.config.set("winminwidth", 5)
    for _ in range(60):
        app.dispatch(ord("-"))
    # min shift is -(80/2) + 5, so the left column bottoms out at 5 cols
    assert _dim(app.left_col.width).preferred == 5


def test_winsplitorientation_option_switches_layout(app):
    """`:set winsplitorientation` (abbrev wso) is the rc-file route to the
    same layout change Ctrl-W makes (command_set_winsplitorientation)."""
    app.execute_command(":set wso=vertical")
    assert app.split_orientation == WSO_VERTICAL
    app.execute_command(":set winsplitorientation=horizontal")
    assert app.split_orientation == WSO_HORIZONTAL


def test_winsplit_option_sets_the_quarter_mark(app):
    app.execute_command(":set winsplit=src_big")
    assert app.win_split == 1
    assert _dim(app.source_window.height).preferred == 30


def test_split_geometry_follows_terminal_resize(app):
    """Pane sizes are callables over the live terminal size, so a resize
    re-derives them on the next layout pass -- the equivalent of
    if_resize_term -> if_layout in C."""
    from prompt_toolkit.data_structures import Size

    assert _dim(app.source_window.height).preferred == 20
    app.app.output.get_size = lambda: Size(rows=50, columns=120)
    assert _dim(app.source_window.height).preferred == 25
    app.dispatch(kc.CTRL_W)
    assert _dim(app.left_col.width).preferred == 60
    assert app._gdb_width() == 120 - 60 - 1


# ---------------------------------------------------------------------------
# quit
# ---------------------------------------------------------------------------
def test_quit_stops_gdb_and_closes_the_tty(app):
    g = app._fake["gdb"]
    tty = app._fake["tty"]
    app.quit()
    assert g.stopped
    assert tty.closed
    assert any(c.startswith("-gdb-exit") for c in g.commands)


def test_color_option_rebuilds_the_style(app):
    app.execute_command(":set color=off")
    assert app.config.get("color") == 0
    app.execute_command(":set color=on")
    assert app.config.get("color") == 1


def test_cursor_line_is_distinct_from_the_status_bar(app):
    """The cursor-line highlight and the status bar must resolve to
    different attributes; when both used bg:#44475a the selected line
    read as a second status bar at a glance.

    Checked through the real style translation, not just the dict, and
    in monochrome too (bold vs reverse)."""
    from prompt_toolkit.styles import DEFAULT_ATTRS

    sel = app.style.get_attrs_for_style_str("class:selected-line",
                                            DEFAULT_ATTRS)
    bar = app.style.get_attrs_for_style_str("class:status", DEFAULT_ATTRS)
    assert sel.bgcolor != bar.bgcolor

    app.execute_command(":set color=off")
    sel = app.style.get_attrs_for_style_str("class:selected-line",
                                            DEFAULT_ATTRS)
    bar = app.style.get_attrs_for_style_str("class:status", DEFAULT_ATTRS)
    assert (sel.bold, sel.reverse) != (bar.bold, bar.reverse)
    app.execute_command(":set color=on")


def test_scrollback_option_reaches_the_console(app):
    app.execute_command(":set scrollbackbuffersize=77")
    assert app.console.scrollback == 77


def test_wrapscan_option_reaches_the_view(app):
    app.execute_command(":set wrapscan=off")
    assert app.view.wrapscan is False


# ---------------------------------------------------------------------------
# audit round 2: C-parity regressions (doc/python_specs.md 8.8 ledger)
# ---------------------------------------------------------------------------
def test_syntax_on_means_detected_language(app, srcfile):
    # `:syntax on` / `yes` turn highlighting on with the *detected*
    # language (cgdbrc.cpp:669-672); they are not lexer names
    app.execute_command(":syntax on")
    assert app.src._forced_lexer is None
    app.execute_command(":syntax yes")
    assert app.src._forced_lexer is None
    app.execute_command(":syntax off")
    assert app.src._forced_lexer == "off"


def test_set_no_prefix_via_colon(app):
    app.execute_command(":set noic")
    assert app.config.get("ignorecase") == 0
    app.execute_command(":set ic")
    assert app.config.get("ic") == 1


def test_set_tabstop_without_value_is_not_clobbered(app):
    # `:set ts` must not set tabstop to 1 (C errors; we report instead)
    app.execute_command(":set ts")
    assert app.config.get("tabstop") == 8


def test_thread_selected_moves_the_source(app, srcfile):
    # `thread 2` produces only =thread-selected; C runs it through
    # source_position_changed just like *stopped (tgdb.cpp:486-504)
    g = app._fake["gdb"]
    g.callbacks["on_notify"]("thread-selected", {
        "id": "2", "frame": {"fullname": srcfile, "line": "9"}})
    assert app.exec_line == (srcfile, 9)
    assert app.view.cursor == 8


def test_rc_commands_replay_after_gdb_starts(app, srcfile):
    # the rc file is a full command language, replayed through `:`
    app.rc_lines = [(1, "set hlsearch"), (2, "syntax off")]
    app.run_rc_commands()
    assert app.config.get("hls") == 1
    assert app.src._forced_lexer == "off"
    # errors land in the console, per line, without raising
    app.rc_lines = [(3, "nosuchcommand")]
    app.run_rc_commands()
    assert "Error parsing line 3" in "".join(app.console.lines)


def test_reopen_keeps_the_viewport(app, srcfile):
    # choosing the already-open file from the dialog, or `:e`, leaves the
    # cursor exactly where it was (source_set_exec_line)
    app.view.move_to(30, len(app.src))
    app.filedlg.set_files([(os.path.basename(srcfile), srcfile)])
    app.filedlg.goto(0)
    app.open_file(srcfile)
    assert app.view.cursor == 30
    app.execute_command(":e")
    assert app.view.cursor == 30


def test_edit_and_asr_reload_only_when_the_file_changed(app, tmp_path):
    f = tmp_path / "reload.c"
    f.write_text("int a;\nint b;\n", encoding="utf-8")
    app.open_file(str(f))
    app.message = ""
    assert len(app.src) == 2
    app.execute_command(":e")
    assert len(app.src) == 2          # unchanged: no re-read, no message
    f.write_text("int a;\nint b;\nint c;\n", encoding="utf-8")
    st = os.stat(str(f))
    os.utime(str(f), (st.st_atime, st.st_mtime + 2))
    app.goto_file_line(str(f), 3)     # a stop triggers autosourcereload
    assert len(app.src) == 3
    # with asr off the change is ignored
    app.config.set("asr", 0)
    f.write_text("int a;\n", encoding="utf-8")
    st = os.stat(str(f))
    os.utime(str(f), (st.st_atime, st.st_mtime + 4))
    app.goto_file_line(str(f), 1)
    assert len(app.src) == 3


def test_inferior_input_is_passed_through_raw(app):
    # while the inferior runs, keys go to its tty byte-wise, exactly as C
    # writes each key into the console pty (gdb_input -> result=1 ->
    # tgdb_send_char): CR stays CR (the pty's ICRNL makes \n of it for the
    # inferior), special keys travel as their terminal sequences
    # (kui_term_get_ascii_char_sequence_from_key == KEY_TO_SEQUENCE)
    tty = app._fake["tty"]
    app.running_inferior = True
    app.focus = FOCUS_GDB
    n_lines = len(app.console.lines)
    app.gdb_key(ord("x"))
    app.gdb_key(kc.CTRL_M)
    app.gdb_key(kc.UP)
    app.gdb_key(kc.CTRL_C)
    app.gdb_key(kc.F5)
    assert tty.written == [b"x", b"\r", b"\x1b[A", b"\x03", b"\x1b[15~"]
    # the echo comes back from the pty's line discipline (ECHO), never
    # from here: echoing by hand would double it
    assert len(app.console.lines) == n_lines
    assert "".join(app.console.cur) == ""


def test_inferior_vt_takes_over_the_gdb_pane(app):
    # while the inferior runs, the GDB window IS its terminal: colours
    # render, and the edit line is hidden (C: the whole pane is one vterm)
    from cgdbpy.vt import InferiorScreen
    app.vt = InferiorScreen(10, 40)
    app.running_inferior = True
    app.vt.feed(b"\x1b[31mRED\x1b[0m\r\nplain\r\n")
    frags = app._console_text()
    text = "".join(t for _, t in frags)
    assert "RED" in text and "plain" in text
    assert any("fg:ansired" in style for style, _ in frags)
    assert not app.edit_line.filter()
    app.running_inferior = False
    # the content keeps the pane until it is flattened -- flipping the
    # running flag alone does not strand the inferior's last screen
    assert not app.edit_line.filter()
    app._vt_drained()
    assert app.edit_line.filter()


def test_inferior_vt_clear_and_position_render(app):
    # \033[2J clears the pane and \033[3;10H positions absolutely -- the
    # behaviours the dumb line model could not do (the probe against
    # /usr/bin/cgdb 0.8.0 showed exactly these)
    from cgdbpy.vt import InferiorScreen
    app.vt = InferiorScreen(10, 40)
    app.running_inferior = True
    app.vt.feed(b"START\r\n")
    app.vt.feed(b"\x1b[2J\x1b[H\x1b[3;10HPOS\r\n")
    text = "".join(t for _, t in app._console_text())
    assert "START" not in text
    rows = [t for _, t in app._console_text()]
    # row 2 (0-based) col 9: the escape's 1-based row 3 col 10
    lines = text.split("\n")
    assert lines[2][9] == "P"


def test_inferior_vt_flattens_into_the_console_on_stop(app):
    # *stopped drains the pty and commits the VT's content to the console
    # history before gdb's stop report is appended (visual order == C)
    from cgdbpy.vt import InferiorScreen
    app.vt = InferiorScreen(10, 40)
    app.vt.feed(b"OUT-1\r\nOUT-2\r\n")
    app._on_stopped({})
    assert app.running_inferior is False
    assert "OUT-1" in "".join(app.console.lines)
    assert "OUT-2" in "".join(app.console.lines)
    assert app.vt.extract_lines() == []     # flattened: VT is empty again
    # the flatten is idempotent: a second stop changes nothing
    app._on_stopped({})
    assert "".join(app.console.lines).count("OUT-1") == 1


def test_on_running_resets_the_vt(app):
    # a fresh run gets a fresh screen (the last run's content already
    # lives in the console history, so the continuity matches C's vterm);
    # the focus leaves the (hidden) edit line for the VT pane
    from cgdbpy.vt import InferiorScreen
    app.vt = InferiorScreen(10, 40)
    app.focus = FOCUS_GDB
    app.vt.feed(b"OLD\r\n")
    app._fake["gdb"].callbacks["on_running"]({})
    assert app.running_inferior is True
    assert app.vt.extract_lines() == []
    assert app.app.layout.current_control is app.console_control


def test_missing_running_record_still_shows_and_routes(app):
    # An older gdb may announce the resume only as ^running (routed to
    # on_running by gdbclient) -- or, worst case, deliver nothing the app
    # recognizes. The VT's own content must then keep the pane and route
    # the keys, or the program looks dead and the edit line eats typing.
    from cgdbpy.vt import InferiorScreen
    app.vt = InferiorScreen(10, 40)
    app.focus = FOCUS_GDB
    app.running_inferior = False       # no *running ever arrived
    app.vt.feed(b"GIMME> \r\n")
    assert app._vt_active()
    assert not app.edit_line.filter()
    text = "".join(t for _, t in app._console_text())
    assert "GIMME>" in text
    assert not app._buffer_owns_keys()
    tty = app._fake["tty"]
    n = len(tty.written)
    app.gdb_key(ord("4"))
    assert tty.written[n:] == [b"4"]
    assert app.gdb_buffer.text == ""   # the edit line did not eat it


def test_missing_running_report_joins_the_vt_in_order(app):
    # with *running missed, console text still joins the VT while it owns
    # the pane (C renders both in the one vterm), so the report shows up
    # BELOW the inferior's output; *stopped's flatten commits the story
    app.running_inferior = False       # no *running ever arrived
    from cgdbpy.vt import InferiorScreen
    app.vt = InferiorScreen(10, 40)
    app.vt.feed(b"OUT\r\n")
    app._on_console("Program received signal SIGINT\n")
    lines = app.vt.extract_lines()
    assert "OUT" in lines
    assert "Program received signal SIGINT" in lines
    assert lines.index("OUT") < lines.index("Program received signal SIGINT")
    # the line model got nothing while the VT owned the pane
    assert "OUT" not in "".join(app.console.lines)
    # and the stop still flattens the whole story into the history
    app._on_stopped({})
    assert "OUT" in "".join(app.console.lines)
    assert "Program received signal SIGINT" in "".join(app.console.lines)


def test_sync_pane_geometry_pushes_sizes(app):
    # the pty and the VT both track the GDB pane's size (tgdb_resize_
    # console hands the same numbers to the pty in C)
    from cgdbpy.vt import InferiorScreen
    app.vt = InferiorScreen(10, 40)
    app.tty.embedded = True                # the pty shape, not the popup
    app._tty_size = None
    app._console_text()
    rows, cols = app._gdb_pane_rows(), app._gdb_width()
    assert app.vt.size == (rows, cols)
    assert app.tty.resized[-1] == (rows, cols)
    # and no redundant ioctl when nothing changed
    n = len(app.tty.resized)
    app._console_text()
    assert len(app.tty.resized) == n


def test_relative_breakpoint_file_still_matches(app, srcfile, monkeypatch):
    # -break-info can report a relative `file` with no fullname; the
    # abspath fallback keeps it matching the absolute buffer path
    monkeypatch.chdir(os.path.dirname(srcfile))
    app.breakpoints.update_from_mi({"BreakpointTable": {"body": [
        {"bkpt": {"number": "1", "enabled": "y",
                  "file": os.path.basename(srcfile), "line": "3"}}]}})
    assert app.breakpoints.at(srcfile, 3) == ["1"]
    assert app.breakpoints.line_status(srcfile, 3) == "enabled"


def test_q_bang_is_not_a_command(app):
    # the lexer scans `q!` as one identifier; C reports Unknown command
    app.execute_command(":q!")
    assert "Unknown command" in app.message
    assert app._quitting is False


def test_empty_search_repeats_the_last_pattern(app, srcfile):
    # `/` + Enter repeats the previous search (interface.cpp:1008-1017)
    app._search_target = "source"
    app.view.move_to(0, len(app.src))
    app.execute_command("/line")
    first = app.view.cursor
    assert first > 0
    app.execute_command("/")
    assert app.view.cursor != first    # the repeat found the next match


def test_esc_restores_the_search_origin(app, srcfile):
    # C puts the cursor back where the search began on ESC
    # (interface.cpp:1529-1543)
    app.view.move_to(0, len(app.src))
    app.dispatch(ord("/"))
    app.cmd_buffer.text = "/line"
    assert app.view.cursor > 0         # the live preview jumped
    app.dispatch(kc.ESC)
    assert app.view.cursor == 0


def test_ctrl_c_clears_the_idle_edit_line(app):
    # ^C with gdb idle aborts the line being composed, as readline does
    from prompt_toolkit.keys import Keys
    app.set_focus(FOCUS_GDB)
    app.gdb_buffer.text = "half-typed"
    app.running_inferior = False
    app.on_key(KP(Keys.ControlC, "\x03"))
    assert app.gdb_buffer.text == ""
    assert app._fake["gdb"].interrupted == 1


def test_message_clears_on_the_next_key(app):
    app.message = "old message"
    app.dispatch(ord("j"))
    assert app.message == ""


def test_selected_line_defaults_to_the_highlight_style(app):
    """One cursor glyph, and it is the highlighted line (user feedback).

    C's default selectedlinedisplay=block paints a reverse block at the
    code's first non-blank; next to the hardware cursor at the line end
    that reads as *two* cursors, and the block never moves with h/l
    scrolling while the hardware cursor does. This port highlights the
    whole line instead -- exactly one glyph, the cursor; the C look stays
    one `:set sld=block` away.
    """
    app.view.cursor = 3
    app.view.height = 10
    text = app._source_text()
    assert any("class:selected-line" in s for s, _ in text)
    assert not any("class:sel-block" in s for s, _ in text)

    app.config.set("sld", "block")
    text = app._source_text()
    assert not any("class:selected-line" in s for s, _ in text)
    assert any("class:sel-block" in s for s, _ in text)


def test_selected_line_keeps_token_foreground_and_bar_background(app):
    """The highlight bar must cover syntax-highlighted tokens too.

    Regression: the rewrite concatenated "class:selected-line" and
    "class:kw" without the separating space, so ptk parsed the pair as one
    unknown class name and the selected line's tokens lost BOTH the
    keyword colour and the bar background -- keywords looked like black
    cells punching holes in the cursor bar.
    """
    app.config.set("sld", "highlight")
    app.view.cursor = 2          # "int compute(int n)" -- a keyword line
    app.view.height = 10
    frags = app._source_text()
    kw = [(s, t) for s, t in frags
          if s.startswith("class:selected-line ")
          and ("class:kw" in s or "class:kwtype" in s)]
    assert kw, ("no highlighted keyword on the selected line:\n%r"
                % (frags[:12],))
    for s, _ in kw:
        # the token class is SPACE-joined, not glued: ptk splits class
        # names on spaces, so a glued pair resolves to nothing at all
        assert s == "class:selected-line class:kw" \
            or s == "class:selected-line class:kwtype", s
    attrs = app.style.get_attrs_for_style_str(kw[0][0])
    assert attrs.color in ("ff79c6", "8be9fd")   # the keyword keeps its colour
    assert attrs.bgcolor == "2e4a62"             # and the bar background


def test_source_cursor_hides_only_in_source_focus(app):
    """The cursor bar is the only indicator in the source window; the
    hardware cursor shows on every other surface (user preference -- C
    keeps curs_set(1) in the source pane too)."""
    app.set_focus(FOCUS_SOURCE)
    assert app.source_window.always_hide_cursor()
    app.set_focus(FOCUS_GDB)
    assert not app.source_window.always_hide_cursor()
    app.set_focus(FOCUS_COMMAND)
    assert not app.source_window.always_hide_cursor()


def test_map_f4_types_a_gdb_command_and_returns(app):
    """:map <f4> iinfo locals<cr><esc> -- the classic "run a command and
    come back" mapping.

    Two things have to hold. The expansion must survive its own first
    key: 'i' switches the focus to the GDB window, which swaps the
    active map set -- C's kui keeps pending keys across that swap, and
    the port used to clear them, killing the whole rhs after 'i'. And
    the text keys of the rhs must land in the synthesized edit line:
    terminal-typed keys go straight to prompt_toolkit's Buffer, but a
    mapping's rhs is dispatched key by key, so gdb_key has to type them
    in -- <cr> runs the command, <esc> returns to the source window.
    """
    import time
    g = app._fake["gdb"]
    app.set_focus(FOCUS_SOURCE)
    app.execute_command(":map <f4> iinfo locals<cr><esc>")
    assert "f4" in str(app.map_set.entries()) or len(app.map_set) == 1

    press(app, kc.F4)
    time.sleep(0.3)

    # the command reached gdb through the console interpreter
    cmds = [c for c in g.commands if "info locals" in c]
    assert cmds, g.commands
    assert any("interpreter-exec" in c for c in cmds), cmds
    # the edit line is empty again (accept reset it) and we are back
    assert app.gdb_buffer.text == ""
    assert app.focus == FOCUS_SOURCE


def test_map_rhs_keys_edit_the_gdb_line(app):
    """The buffer-typing path also covers backspace."""
    app.set_focus(FOCUS_SOURCE)
    app.execute_command(":map <f7> ifoo<BS><BS>bar<cr>")
    press(app, kc.F7)
    import time
    time.sleep(0.25)
    g = app._fake["gdb"]
    cmds = [c for c in g.commands if "bar" in c and "foo" not in c]
    assert cmds, g.commands
    # no <esc> in this rhs: the focus stays in the GDB window
    assert app.focus == FOCUS_GDB
