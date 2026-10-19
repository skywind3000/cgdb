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
from cgdbpy.app import (FOCUS_COMMAND, FOCUS_GDB, FOCUS_SOURCE, CgdbApp,
                        translate_key)
from cgdbpy.cgdbrc import Config
from cgdbpy.gdbclient import GdbError
from cgdbpy import mi

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
    def __init__(self, on_output):
        self.on_output = on_output
        self.written = []
        self.started = False
        self.closed = False

    def start(self):
        self.started = True
        return "\\\\.\\pipe\\faketty"

    def gdb_argument(self):
        return "\\\\.\\pipe\\faketty"

    def mi_command(self):
        return "-gdb-set inferior-tty \\\\.\\pipe\\faketty"

    def write(self, data):
        self.written.append(data)
        return True

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

    def tty_factory(on_output):
        fake["tty"] = FakeTty(on_output)
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
    app.view.height = 10
    app.view.cursor = 30
    app.dispatch(kc.CTRL_D)
    assert app.view.cursor == 35
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


def test_unknown_mark_reports_a_message(app):
    app.dispatch(ord("'"))
    app.dispatch(ord("z"))
    assert "not set" in app.message


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


def test_F5_continues_when_already_running(app):
    g = app._fake["gdb"]
    app.running_inferior = True
    before = len(g.commands)
    app.dispatch(kc.F5)
    import time
    time.sleep(0.3)
    assert "-exec-continue" in g.commands[before:]
    assert "-exec-run" not in g.commands[before:]


def test_ctrl_c_interrupts(app):
    from prompt_toolkit.keys import Keys
    g = app._fake["gdb"]
    app.on_key(KP(Keys.ControlC, "\x03"))
    assert g.interrupted == 1


def test_F1_shows_help(app):
    app.dispatch(kc.F1)
    assert "F5" in app.message and "ESC" in app.message


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
    assert "breakpoint-hit" in app.message


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
    assert "F5" in app.message


def test_unknown_colon_command(app):
    app.execute_command(":frobnicate")
    assert "unknown command" in app.message


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


# ---------------------------------------------------------------------------
# gdb console scroll mode
# ---------------------------------------------------------------------------
def test_pageup_enters_scroll_mode(app):
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active


def test_scroll_mode_navigation(app):
    app._fake["gdb"].emit_console("".join("L%d\n" % i for i in range(60)))
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active
    d0 = app.scroll.delta
    app.dispatch(kc.UP)
    assert app.scroll.delta == d0 + 1
    app.dispatch(kc.DOWN)
    assert app.scroll.delta == d0
    app.dispatch(ord("g"))
    assert app.scroll.delta == app.console.max_delta(app._console_height())
    app.dispatch(ord("G"))
    assert app.scroll.delta == 0


def test_esc_leaves_scroll_mode(app):
    app.dispatch(ord("i"))
    app.dispatch(kc.PPAGE)
    assert app.scroll.active
    app.dispatch(kc.ESC)
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
    app.breakpoints.update_from_mi({"BreakpointTable": {"body": [
        {"bkpt": {"number": "1", "enabled": "y",
                  "fullname": app.src.path, "line": "1"}}]}})
    app.view.height = 10
    app.view.top = 0
    text = app._source_text()
    styles = [s for s, _ in text]
    assert any("breakpoint" in s for s in styles)
    assert any(t == "B" for _, t in text)


def test_source_rendering_marks_the_exec_line(app, srcfile):
    app.exec_line = (srcfile, 5)
    app.view.height = 10
    app.view.top = 0
    text = app._source_text()
    assert any(t == "=>" for _, t in text)


def test_source_rendering_shows_marks(app):
    app.view.cursor = 2
    app.view.set_mark("a")
    app.view.height = 10
    app.view.top = 0
    text = app._source_text()
    assert any(t == "a" for _, t in text)


def test_console_rendering(app):
    app._fake["gdb"].emit_console("hello\nworld\n")
    text = app._console_text()
    joined = "".join(t for _, t in text)
    assert "hello" in joined
    assert "world" in joined


def test_status_rendering_shows_focus_and_file(app):
    text = app._status_text()
    joined = "".join(t for _, t in text)
    assert "CGDB" in joined
    assert os.path.basename(app.src.path) in joined


def test_status_rendering_shows_running(app):
    app.running_inferior = True
    joined = "".join(t for _, t in app._status_text())
    assert "RUNNING" in joined


def test_status_rendering_shows_gdb_focus(app):
    app.focus = FOCUS_GDB
    joined = "".join(t for _, t in app._status_text())
    assert "GDB" in joined


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
    # the gdb edit line never moves
    assert kids[-1] is app.edit_window


# ---------------------------------------------------------------------------
# window splitting
# ---------------------------------------------------------------------------
def test_plus_and_minus_adjust_the_split(app):
    h0 = app.source_window.height
    app.source_window.height = 10
    app.dispatch(ord("+"))
    assert app.source_window.height == 11
    app.dispatch(ord("-"))
    assert app.source_window.height == 10
    app.source_window.height = h0 if isinstance(h0, int) else 10


def test_split_respects_winminheight(app):
    app.config.set("winminheight", 5)
    app.source_window.height = 5
    app.dispatch(ord("-"))
    assert app.source_window.height == 5


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


def test_scrollback_option_reaches_the_console(app):
    app.execute_command(":set scrollbackbuffersize=77")
    assert app.console.scrollback == 77


def test_wrapscan_option_reaches_the_view(app):
    app.execute_command(":set wrapscan=off")
    assert app.view.wrapscan is False
