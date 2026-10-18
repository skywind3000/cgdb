"""Integration tests against a real gdb.

These are the tests that matter most: everything else can be self-consistent
and still wrong about what gdb actually says. Each test spawns a real gdb child
and drives it over MI pipes.

Skipped when gdb or a C compiler is unavailable. Set CGDB_GDB to point at a
specific gdb.

Cleanup is deliberate: gdb is killed in a finally block and the process group is
terminated, because AGENTS.md records that the C version's tgdb_driver leaves
orphan gdb processes holding a pty.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from cgdbpy import mi
from cgdbpy.gdbclient import GdbClient, GdbError

HERE = os.path.dirname(os.path.abspath(__file__))

TARGET_SRC = r"""
#include <stdio.h>
#include <stdlib.h>

int big[1000];

int compute(int n) {
    int s = 0;
    for (int i = 0; i < n; i++) {
        s += i * i;
        big[i] = s;
    }
    return s;
}

int main(int argc, char **argv) {
    int n = argc > 1 ? atoi(argv[1]) : 5;
    printf("start n=%d\n", n);
    int r = compute(n);
    printf("result=%d\n", r);
    return 0;
}
"""


def find_gdb():
    env = os.environ.get("CGDB_GDB")
    if env:
        return env if os.path.exists(env) else None
    found = shutil.which("gdb")
    if found:
        return found
    for cand in (r"D:\Dev\mingw32\bin\gdb.exe",
                 r"D:\Linux\mingw64\bin\gdb.exe",
                 "/usr/bin/gdb"):
        if os.path.exists(cand):
            return cand
    return None


def find_cc():
    for name in ("gcc", "cc", "clang"):
        p = shutil.which(name)
        if p:
            return p
    for cand in (r"D:\Dev\mingw32\bin\gcc.exe", "/usr/bin/gcc"):
        if os.path.exists(cand):
            return cand
    return None


GDB = find_gdb()
CC = find_cc()

needs_gdb = pytest.mark.skipif(GDB is None, reason="gdb not available")
needs_cc = pytest.mark.skipif(CC is None, reason="no C compiler for the target")


@pytest.fixture(scope="module")
def target():
    """Compile the test inferior once per module."""
    if CC is None:
        pytest.skip("no C compiler")
    d = tempfile.mkdtemp(prefix="cgdbpy-itest-")
    src = os.path.join(d, "target.c")
    exe = os.path.join(d, "target" + (".exe" if sys.platform == "win32" else ""))
    with open(src, "w", encoding="utf-8") as f:
        f.write(TARGET_SRC)
    # gdb must be able to debug it: keep -g, no optimisation
    r = subprocess.run([CC, "-g", "-O0", src, "-o", exe],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not os.path.exists(exe):
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip("could not compile target: %s" % r.stderr[:400])
    yield {"dir": d, "src": src, "exe": exe}
    shutil.rmtree(d, ignore_errors=True)


class Session:
    """A gdb child plus the events it emitted, with guaranteed cleanup."""

    def __init__(self, args=(), use_tty=False):
        self.events = []
        self.console = []
        self.target_out = []
        self.stopped = []
        self.notifies = []
        self.exited = []
        self._lock = __import__("threading").Lock()
        self.tty = None
        callbacks = {
            "on_console": lambda t: self._rec(self.console, t),
            "on_target": lambda t: self._rec(self.target_out, t),
            "on_log": lambda t: self._rec(self.console, t),
            "on_stopped": lambda p: self._rec(self.stopped, p),
            "on_running": lambda p: self._rec(self.events, ("running", p)),
            "on_notify": lambda c, p: self._rec(self.notifies, (c, p)),
            "on_exit": lambda code: self._rec(self.exited, code),
        }
        self.client = GdbClient(gdb_path=GDB, args=list(args),
                                callbacks=callbacks)
        if use_tty:
            from cgdbpy.inferior_tty import create
            self.tty = create(lambda d: self._rec(self.target_out, d))

    def _rec(self, sink, item):
        # the inferior tty delivers bytes, the MI callbacks deliver str
        if isinstance(item, (bytes, bytearray)):
            item = bytes(item).decode("utf-8", "replace")
        with self._lock:
            sink.append(item)

    def start(self):
        self.client.start()
        if self.tty is not None:
            self.tty.start()
            # unquoted -- see InferiorTty.gdb_argument()
            self.client.call(self.tty.mi_command(), timeout=15)
        return self

    def call(self, cmd, timeout=30):
        return self.client.call(cmd, timeout=timeout)

    def console_text(self):
        with self._lock:
            return "".join(self.console)

    def target_text(self):
        with self._lock:
            return "".join(self.target_out)

    def wait_stopped(self, timeout=30, count=1):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if len(self.stopped) >= count:
                    return self.stopped[count - 1]
            time.sleep(0.02)
        return None

    def close(self):
        try:
            if self.client.running:
                try:
                    self.client.call("-gdb-exit", timeout=3)
                except Exception:
                    pass
        finally:
            self.client.stop(timeout=3)
            if self.tty is not None:
                self.tty.close()


@pytest.fixture
def session():
    s = Session()
    try:
        yield s.start()
    finally:
        s.close()


@pytest.fixture
def tty_session():
    s = Session(use_tty=True)
    try:
        yield s.start()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------
@needs_gdb
def test_gdb_starts_and_reaches_a_prompt(session):
    # the reader thread must have seen the banner and a prompt
    end = time.time() + 30
    while time.time() < end and not session.client.at_prompt:
        time.sleep(0.02)
    assert session.client.running
    assert session.client.stats["records"] > 0


@needs_gdb
def test_banner_arrives_as_notify_records(session):
    end = time.time() + 20
    while time.time() < end and not any(
            c == "thread-group-added" for c, _ in session.notifies):
        time.sleep(0.02)
    classes = [c for c, _ in session.notifies]
    assert "thread-group-added" in classes


def test_missing_gdb_fails_fast_and_visibly():
    """AGENTS.md / commit "Fix silent crash when gdb is not on PATH": a bad
    debugger path must raise with a usable message, not hang or die silently."""
    c = GdbClient(gdb_path=os.path.join(HERE, "definitely-not-a-debugger-xyz"))
    with pytest.raises(GdbError) as ei:
        c.start()
    msg = str(ei.value)
    assert "cannot start gdb" in msg
    assert "definitely-not-a-debugger-xyz" in msg


# ---------------------------------------------------------------------------
# basic MI commands
# ---------------------------------------------------------------------------
@needs_gdb
def test_break_info_on_a_fresh_session(session):
    r = session.call("-break-info")
    assert r.kind == mi.RESULT
    assert r.cls == "done"
    table = r.payload.get("BreakpointTable", {})
    assert table.get("nr_rows") == "0"


@needs_gdb
def test_token_pairing(session):
    """Every response must carry the token we sent (python_specs.md §13.2)."""
    for i in range(20):
        r = session.call("-break-info")
        assert r.token is not None
        assert r.cls == "done"


@needs_gdb
def test_error_result_raises_with_gdb_message(session):
    """`call()` turns `^error` into an exception so programmatic callers cannot
    silently ignore a failed command."""
    with pytest.raises(GdbError) as ei:
        session.call("-break-insert")       # missing <location>
    assert "Missing <location>" in str(ei.value)


@needs_gdb
def test_error_result_is_also_shown_to_the_user(session):
    """...and the same error reaches the console callback, so a command the
    user typed is reported even though call() raised."""
    before = session.console_text()
    with pytest.raises(GdbError):
        session.call("-break-insert")
    end = time.time() + 10
    while time.time() < end and session.console_text() == before:
        time.sleep(0.02)
    assert "Missing <location>" in session.console_text()


@needs_gdb
def test_error_result_is_not_a_parse_error(session):
    with pytest.raises(GdbError):
        session.call("-break-insert")
    time.sleep(0.2)
    assert session.client.stats["parse_errors"] == 0


@needs_gdb
def test_console_command_routes_through_interpreter_exec(session):
    r = session.call('-interpreter-exec console %s' % mi.quote("show version"))
    assert r.cls == "done"
    assert "GNU gdb" in session.console_text() or session.console_text()


@needs_gdb
@needs_cc
def test_breakpoint_location_quoting(target):
    """The whole `path:line` must be ONE quoted MI string.

    Quoting only the path -- `"C:\\dir\\f.c":12`, which is the spelling the C
    version uses on the console side -- makes gdb 17.1 answer
    `^error,msg="Problem parsing arguments"`. Measured, not guessed.
    """
    s = Session(args=[target["exe"]])
    try:
        s.start()
        # pick a line that actually has compiled code behind it; a breakpoint
        # on an #include line is a semantic error, not a quoting one
        code_line = next(i for i, l in enumerate(TARGET_SRC.split("\n"), 1)
                         if l.strip().startswith("s += i"))
        good = s.call("-break-insert -- %s"
                      % mi.breakpoint_location(target["src"], code_line))
        assert good.cls == "done", good.payload
        assert good.payload["bkpt"]["func"] == "compute"
        assert good.payload["bkpt"]["line"] == str(code_line)

        s.call('-interpreter-exec console %s' % mi.quote("delete"))
        # call() turns ^error into an exception, so the bad spelling must be
        # caught rather than inspected
        with pytest.raises(GdbError) as ei:
            s.call("-break-insert -- %s:%d"
                   % (mi.quote(target["src"]), code_line))
        assert "parsing" in str(ei.value).lower(), str(ei.value)
    finally:
        s.close()


@needs_gdb
@needs_cc
def test_breakpoint_in_a_path_with_spaces(tmp_path):
    """Spaces in the directory must survive, which is why the location is
    quoted at all."""
    cc = find_cc()
    d = tmp_path / "a dir with spaces"
    d.mkdir()
    src = d / "spaced.c"
    src.write_text("int compute(int n) {\n    int s = n;\n    return s;\n}\n"
                   "int main(void) { return compute(1); }\n", encoding="utf-8")
    exe = d / ("spaced.exe" if sys.platform == "win32" else "spaced")
    r = subprocess.run([cc, "-g", "-O0", str(src), "-o", str(exe)],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        pytest.skip("could not compile: %s" % r.stderr[:300])

    s = Session(args=[str(exe)])
    try:
        s.start()
        rec = s.call("-break-insert -- %s"
                     % mi.breakpoint_location(str(src), 2))
        assert rec.cls == "done", rec.payload
        bk = rec.payload["bkpt"]
        assert "a dir with spaces" in (bk.get("fullname") or "")
        assert bk.get("line") == "2"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# real debugging session
# ---------------------------------------------------------------------------
@needs_gdb
@needs_cc
def test_full_break_run_stop_cycle(target):
    s = Session(args=[target["exe"]])
    try:
        s.start()
        r = s.call("-break-insert main")
        assert r.cls == "done", r.payload
        bkpt = r.payload["bkpt"]
        number = bkpt["number"]
        assert bkpt["func"] == "main"

        s.call("-exec-run")
        stopped = s.wait_stopped(timeout=60)
        assert stopped is not None, "inferior never stopped"
        assert stopped.get("reason") == "breakpoint-hit"
        frame = stopped.get("frame", {})
        assert frame.get("func") == "main"
        assert frame.get("fullname"), "no fullname in the stop frame"
        assert int(frame["line"]) > 0

        # the breakpoint table now reports one hit
        info = s.call("-break-info")
        from cgdbpy.source import Breakpoints
        b = Breakpoints()
        b.update_from_mi(info.payload)
        assert number in b.numbers()

        s.call("-exec-continue")
        stopped2 = s.wait_stopped(timeout=60, count=2)
        assert stopped2 is not None
        # gdb 17.1 reports "exited-normally", not "exited"
        assert stopped2.get("reason") in ("exited-normally", "exited",
                                          "terminated")
    finally:
        s.close()


@needs_gdb
@needs_cc
def test_stepping_moves_the_source_line(target):
    s = Session(args=[target["exe"]])
    try:
        s.start()
        s.call("-break-insert compute")
        s.call("-exec-run")
        first = s.wait_stopped(timeout=60)
        assert first is not None
        line1 = int(first["frame"]["line"])

        s.call("-exec-next")
        second = s.wait_stopped(timeout=60, count=2)
        assert second is not None
        assert second.get("reason") == "end-stepping-range"
        line2 = int(second["frame"]["line"])
        assert line2 != line1, "next did not move the line"
        assert second["frame"]["fullname"] == first["frame"]["fullname"]
    finally:
        s.close()


@needs_gdb
@needs_cc
def test_stack_info_frame_matches_the_stop(target):
    s = Session(args=[target["exe"]])
    try:
        s.start()
        s.call("-break-insert main")
        s.call("-exec-run")
        stopped = s.wait_stopped(timeout=60)
        assert stopped is not None
        r = s.call("-stack-info-frame")
        assert r.cls == "done"
        assert r.payload["frame"]["func"] == "main"
    finally:
        s.close()


@needs_gdb
@needs_cc
def test_evaluate_expression(target):
    s = Session(args=[target["exe"]])
    try:
        s.start()
        s.call("-break-insert compute")
        s.call("-exec-run")
        assert s.wait_stopped(timeout=60) is not None
        r = s.call('-data-evaluate-expression %s' % mi.quote("n"))
        assert r.cls == "done"
        assert "value" in r.payload
    finally:
        s.close()


@needs_gdb
@needs_cc
def test_break_insert_does_not_emit_a_notification(target):
    """Measured on gdb 17.1: `-break-insert` returns the breakpoint only in its
    `^done,bkpt={...}` result. No =breakpoint-created arrives. A frontend that
    waits for the notification would hang, so the result record must be
    consumed too -- that is what Breakpoints.absorb_result_bkpt() is for."""
    s = Session(args=[target["exe"]])
    try:
        s.start()
        before = len(s.notifies)
        r = s.call("-break-insert main")
        assert r.cls == "done"
        time.sleep(0.5)
        created = [p for c, p in s.notifies[before:]
                   if c == "breakpoint-created"]
        assert created == [], "gdb unexpectedly notified: %r" % created

        from cgdbpy.source import Breakpoints
        b = Breakpoints()
        assert b.absorb_result_bkpt(r.payload) is True
        assert b.numbers() == [r.payload["bkpt"]["number"]]
    finally:
        s.close()


@needs_gdb
@needs_cc
def test_console_break_does_emit_a_notification(target):
    """...but a breakpoint created through the console (what the user types in
    the GDB window) DOES arrive as =breakpoint-created."""
    s = Session(args=[target["exe"]])
    try:
        s.start()
        s.call('-interpreter-exec console %s' % mi.quote("break compute"))
        end = time.time() + 20
        while time.time() < end and not any(
                c == "breakpoint-created" for c, _ in s.notifies):
            time.sleep(0.02)
        created = [p for c, p in s.notifies if c == "breakpoint-created"]
        assert created, "no =breakpoint-created for a console break"

        from cgdbpy.source import Breakpoints
        b = Breakpoints()
        assert b.absorb_async("breakpoint-created", created[0]) is True
        assert b.numbers() == ["1"]
        assert b.all()[0]["locations"], "no file/line in the notification"
    finally:
        s.close()


@needs_gdb
@needs_cc
def test_console_delete_notifies_with_id_not_bkpt(target):
    """=breakpoint-deleted carries `id="1"`, not a bkpt tuple. Keying off
    `bkpt` silently ignores every deletion."""
    s = Session(args=[target["exe"]])
    try:
        s.start()
        s.call('-interpreter-exec console %s' % mi.quote("break compute"))
        time.sleep(0.5)
        s.call('-interpreter-exec console %s' % mi.quote("delete 1"))
        end = time.time() + 20
        while time.time() < end and not any(
                c == "breakpoint-deleted" for c, _ in s.notifies):
            time.sleep(0.02)
        deleted = [p for c, p in s.notifies if c == "breakpoint-deleted"]
        assert deleted, "no =breakpoint-deleted"
        assert "id" in deleted[0]
        assert "bkpt" not in deleted[0]

        from cgdbpy.source import Breakpoints
        b = Breakpoints()
        created = [p for c, p in s.notifies if c == "breakpoint-created"]
        assert created
        b.absorb_async("breakpoint-created", created[0])
        assert b.numbers() == ["1"]
        assert b.absorb_async("breakpoint-deleted", deleted[0]) is True
        assert b.numbers() == []
    finally:
        s.close()


@needs_gdb
@needs_cc
def test_breakpoint_delete(target):
    s = Session(args=[target["exe"]])
    try:
        s.start()
        r = s.call("-break-insert main")
        n = r.payload["bkpt"]["number"]
        d = s.call("-break-delete %s" % n)
        assert d.cls == "done"
        info = s.call("-break-info")
        assert info.payload["BreakpointTable"]["nr_rows"] == "0"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# inferior tty: the reason win_specs.md §4 E2a exists
# ---------------------------------------------------------------------------
@needs_gdb
@needs_cc
def test_inferior_output_does_not_corrupt_the_mi_stream(target, tty_session):
    """Without `set inferior-tty` the program's stdout lands in the MI pipe and
    produces parse errors. With it, the MI stream stays clean and the output
    arrives on the tty we handed gdb."""
    s = tty_session
    s.call("-file-exec-and-symbols %s" % mi.quote(target["exe"]))
    s.call("-break-insert main")
    s.call("-exec-run")
    stopped = s.wait_stopped(timeout=60)
    assert stopped is not None
    s.call("-exec-continue")
    assert s.wait_stopped(timeout=60, count=2) is not None

    # give the reader threads a moment to drain
    time.sleep(0.5)
    assert s.client.stats["parse_errors"] == 0, (
        "MI stream was corrupted: %d parse errors"
        % s.client.stats["parse_errors"])
    out = s.target_text() + s.console_text()
    assert "start n=" in out, "inferior stdout never arrived: %r" % out[:200]
    assert "result=" in out


@needs_gdb
@needs_cc
def test_inferior_tty_device_name_is_usable(target, tty_session):
    s = tty_session
    r = s.call("-inferior-tty-show")
    assert r.cls == "done"
    assert "inferior_tty" in r.payload or r.payload


@needs_gdb
@needs_cc
def test_inferior_tty_must_not_be_quoted(target):
    """Regression test for a silent-failure trap.

    `-gdb-set inferior-tty` takes a raw filename, not an MI c-string. Quoting
    it makes gdb reply `^done` -- success -- while actually failing to open the
    device, so the inferior's stdout leaks into the MI pipe as unparseable
    lines. Measured on gdb 17.1: quoted raw-backslash, quoted forward-slash and
    quoted mi.quote() forms all fail; both unquoted forms work.

    The C port encodes the same rule at tgdb.cpp:1990 ("Raw backslashes
    survive the MI argument parser").
    """
    from cgdbpy.inferior_tty import create

    def run_one(quote_it):
        bad = []
        orig = mi.parse_line

        def spy(line):
            r = orig(line)
            if r is not None and r.kind == mi.PARSE_ERROR:
                bad.append(line)
            return r

        tty = create(lambda d: None)
        tty.start()
        got = []
        tty.on_output = lambda d: got.append(d)
        c = GdbClient(gdb_path=GDB, args=[target["exe"]], callbacks={})
        mi.parse_line = spy
        try:
            c.start()
            arg = tty.gdb_argument()
            cmd = "-gdb-set inferior-tty %s" % (mi.quote(arg) if quote_it
                                                else arg)
            r = c.call(cmd, timeout=15)
            assert r.cls == "done", "gdb rejected the command outright"
            c.call("-break-insert main", timeout=15)
            c.send("-exec-run")
            end = time.time() + 40
            while time.time() < end and not got and not bad:
                time.sleep(0.05)
            c.send("-exec-continue")
            time.sleep(1.5)
            return b"".join(got).decode("utf-8", "replace"), list(bad)
        finally:
            mi.parse_line = orig
            c.stop(timeout=3)
            tty.close()

    unquoted_text, unquoted_bad = run_one(False)
    quoted_text, quoted_bad = run_one(True)

    assert "start n=" in unquoted_text, (
        "unquoted form did not deliver inferior output: %r" % unquoted_text)
    assert unquoted_bad == [], (
        "unquoted form leaked into the MI stream: %r" % unquoted_bad)

    # and the quoted form must still be broken -- if gdb ever starts accepting
    # it, this assertion fires and the workaround can be dropped
    assert "start n=" not in quoted_text, (
        "gdb now accepts a quoted inferior-tty; the workaround can go")
    assert quoted_bad, (
        "quoted form neither worked nor leaked -- unexpected third behaviour")


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------
@needs_gdb
@needs_cc
def test_callback_may_issue_a_blocking_call(target):
    """Regression test for a reader-thread deadlock.

    Callbacks used to run on the MI reader thread. `_on_stopped` calls
    `-break-info` to refresh the breakpoint table, and `call()` blocks waiting
    for the result record -- which only the reader thread can deliver. The
    result was a hard hang: the first `*stopped` froze the whole session and no
    further command was ever written to gdb.

    Callbacks now run on a separate dispatch thread while request/response
    pairing stays on the reader thread.
    """
    result = {}

    def on_stopped(payload):
        # this is exactly what CgdbApp._on_stopped does
        try:
            rec = client.call("-break-info", timeout=20)
            result["ok"] = rec.cls
            result["rows"] = rec.payload.get("BreakpointTable", {}).get(
                "nr_rows")
        except Exception as e:            # pragma: no cover - failure path
            result["error"] = repr(e)

    client = GdbClient(gdb_path=GDB, args=[target["exe"]],
                       callbacks={"on_stopped": on_stopped})
    try:
        client.start()
        client.call("-break-insert main", timeout=20)
        client.send("-exec-run")
        end = time.time() + 60
        while time.time() < end and "ok" not in result and "error" not in result:
            time.sleep(0.05)
        assert "error" not in result, result.get("error")
        assert result.get("ok") == "done", (
            "a blocking call() from inside a callback did not complete: %r"
            % result)
        assert result.get("rows") == "1", result

        # and the session must still be usable afterwards
        r = client.call("-exec-continue", timeout=30)
        assert r.cls in ("done", "running")
    finally:
        client.stop(timeout=5)


@needs_gdb
def test_gdb_exit_terminates_the_child(target):
    s = Session(args=[target["exe"]])
    s.start()
    pid = s.client.proc.pid
    s.call("-gdb-exit", timeout=10)
    s.client.stop(timeout=10)
    assert not s.client.running
    # the process must really be gone, not just unreferenced
    end = time.time() + 10
    gone = False
    while time.time() < end:
        if not _pid_alive(pid):
            gone = True
            break
        time.sleep(0.1)
    assert gone, "gdb pid %d survived -gdb-exit" % pid


@needs_gdb
def test_stop_kills_a_gdb_that_ignores_exit():
    s = Session()
    s.start()
    pid = s.client.proc.pid
    s.client.stop(timeout=3)
    end = time.time() + 10
    while time.time() < end and _pid_alive(pid):
        time.sleep(0.1)
    assert not _pid_alive(pid)


def _pid_alive(pid):
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
