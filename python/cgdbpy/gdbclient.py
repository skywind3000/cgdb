"""GDB subprocess management over MI pipes.

Python counterpart of lib/tgdb/tgdb.cpp. Architecture follows the Windows C
port (doc/win_specs.md §7): a single `gdb --interpreter=mi` child on anonymous
pipes, token-based request/response pairing, and a reader thread feeding
callbacks.

Two rules learned the hard way by the C port and honoured here:

  * Pair responses by MI **token**, never by waiting for the `(gdb)` prompt.
    Prompt-waiting measured 15.9 ms/command vs 117 us with tokens
    (doc/python_specs.md §13.2).
  * The reader thread must never block the caller. Requests are queued and a
    single command is in flight at a time, matching tgdb's
    `is_gdb_ready_for_next_command`.
"""

import os
import queue
import subprocess
import sys
import threading
import time

from . import mi

DEFAULT_GDB = "gdb"

# Set CGDBPY_MILOG=<path> to append every MI line sent and received. Diagnosing
# a stuck command queue is otherwise guesswork: the UI shows nothing about
# which request is in flight.
_MILOG = os.environ.get("CGDBPY_MILOG")


def _milog(direction, text):
    if not _MILOG:
        return
    try:
        with open(_MILOG, "a", encoding="utf-8", errors="replace") as f:
            f.write("%.6f %s %s\n" % (time.time(), direction,
                                      text.rstrip("\n")[:400]))
    except OSError:
        pass

# GDB startup flags, identical to the C Windows port (win_specs.md §2)
GDB_BASE_ARGS = ["--nw", "-q",
                 "-ex", "set pagination off",
                 "-ex", "set confirm off",
                 "--interpreter=mi"]


class GdbError(Exception):
    pass


class GdbNotRunning(GdbError):
    pass


# sentinel pushed onto the callback queue when the reader thread finishes
_EOF = object()


class Request:
    """One queued MI command."""

    __slots__ = ("token", "command", "kind", "on_result", "sent_at", "done",
                 "record", "error")

    def __init__(self, token, command, kind="generic", on_result=None):
        self.token = token
        self.command = command
        self.kind = kind
        self.on_result = on_result
        self.sent_at = None
        self.done = threading.Event()
        self.record = None
        self.error = None


class GdbClient:
    """Drives one gdb child process.

    Callbacks (all optional, all invoked on the `gdb-mi-dispatch` thread -- see
    doc/python_specs.md §2.2 for why that is not the reader thread):
      on_console(text)     ~ stream records: text meant for the user
      on_log(text)         & stream records: gdb's own echo
      on_target(text)      @ stream records: the inferior's output
      on_stopped(payload)  *stopped async record
      on_running(payload)  *running async record
      on_notify(cls, payload)  = async records (breakpoint-created, ...)
      on_exit(code)        the gdb process ended
    """

    def __init__(self, gdb_path=None, args=(), callbacks=None, cwd=None,
                 env=None):
        self.gdb_path = gdb_path or os.environ.get("CGDB_GDB") or DEFAULT_GDB
        self.extra_args = list(args)
        self.cb = callbacks or {}
        self.cwd = cwd
        self.env = env

        self.proc = None
        self._reader = None
        self._alive = False
        self._lock = threading.Lock()
        self._queue = []
        self._inflight = {}
        self._wakeup = threading.Event()
        self._next_token = 1
        self._splitter = mi.LineSplitter()
        self._pump = None
        self._events = queue.Queue()
        self._dispatcher = None
        self.at_prompt = False
        self.exit_code = None
        self.spawn_error = None
        self.stats = {"bytes": 0, "lines": 0, "records": 0, "parse_errors": 0}

    # --- lifecycle --------------------------------------------------------
    def _child_env(self):
        """Environment for the gdb child, with PYTHONHOME removed.

        A vendor gdb usually embeds its own CPython and resolves that
        interpreter's stdlib from PYTHONHOME. When PYTHONHOME points at an
        unrelated Python -- easy to arrange, since cgdb.py is itself a Python
        program and its users tend to have one configured -- gdb dies during
        startup with

            Python initialization failed: Failed to import encodings module
            <gdb>: warning: Python failed to initialize with PYTHONHOME set.
            <gdb>/../etc/gdbinit:1: Error in sourced command file:
            Python not initialized

        and every command after that fails. Reproduced byte-for-byte against
        mingw32 gdb 17.1 with PYTHONHOME=C:\\Share\\Python311. gdb's own advice
        is `set python ignore-environment on`, but dropping the variable works
        on every gdb version and cannot break anything: the child has its own
        prefix, so PYTHONHOME is never the right answer for it.
        """
        env = dict(os.environ if self.env is None else self.env)
        env.pop("PYTHONHOME", None)
        return env

    def start(self):
        """Spawn gdb. Raises GdbError with a usable message on failure.

        Fail-fast: if gdb cannot be spawned the caller learns immediately
        rather than through a mysteriously dead UI later.
        """
        argv = [self.gdb_path] + GDB_BASE_ARGS + self.extra_args
        creationflags = 0
        if sys.platform == "win32":
            # own process group so CTRL_BREAK can reach gdb alone
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                cwd=self.cwd,
                env=self._child_env(),
                creationflags=creationflags,
            )
        except OSError as e:
            self.spawn_error = e
            raise GdbError(
                "cannot start gdb (%s): %s\n"
                "Is gdb installed and on PATH? Use -d <path> to point at it."
                % (self.gdb_path, e))
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="gdb-mi-reader")
        self._reader.start()
        self._dispatcher = threading.Thread(target=self._dispatch_loop,
                                            daemon=True,
                                            name="gdb-mi-dispatch")
        self._dispatcher.start()
        self._pump = threading.Thread(target=self._pump_loop, daemon=True,
                                      name="gdb-mi-pump")
        self._pump.start()
        return self

    def stop(self, timeout=2.0):
        self._alive = False
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout)
        except Exception:
            try:
                self.proc.kill()
            except OSError:
                pass

    @property
    def running(self):
        return self._alive and self.proc is not None and \
            self.proc.poll() is None

    def interrupt(self):
        """Send CTRL_BREAK to gdb (Windows) or SIGINT (POSIX).

        Mirrors win_specs.md §2: gdb shares our console and lives in its own
        process group, so GenerateConsoleCtrlEvent(CTRL_BREAK) reaches it and
        gdb injects a break into the inferior.
        """
        if self.proc is None:
            return False
        if sys.platform == "win32":
            import ctypes
            CTRL_BREAK_EVENT = 1
            return bool(ctypes.windll.kernel32.GenerateConsoleCtrlEvent(
                CTRL_BREAK_EVENT, self.proc.pid))
        try:
            self.proc.send_signal(2)      # SIGINT
            return True
        except OSError:
            return False

    # --- sending ----------------------------------------------------------
    def _alloc_token(self):
        with self._lock:
            t = self._next_token
            self._next_token += 1
        return t

    def send(self, command, kind="generic", on_result=None):
        """Queue a tokenised MI command. Returns the Request."""
        if not self.running:
            raise GdbNotRunning("gdb is not running")
        req = Request(self._alloc_token(), command, kind, on_result)
        with self._lock:
            self._queue.append(req)
        self._wakeup.set()
        return req

    def call(self, command, timeout=30.0, kind="generic"):
        """Send and block until the result record arrives."""
        req = self.send(command, kind=kind)
        if not req.done.wait(timeout):
            raise GdbError("timeout waiting for gdb response to %r" % command)
        if req.error:
            raise GdbError(req.error)
        return req.record

    def console_command(self, text, timeout=30.0):
        """Run a human gdb command through the console interpreter.

        This is how the synthesised GDB window executes what the user typed
        (win_specs.md §8.2): everything goes through -interpreter-exec so the
        reply comes back as MI records we can route.
        """
        return self.call('-interpreter-exec console %s' % mi.quote(text),
                         timeout=timeout, kind="console")

    # --- threads ----------------------------------------------------------
    def _pump_loop(self):
        """Write queued commands, one in flight at a time.

        Blocks on an event rather than polling: a 1 ms poll loop wakes a
        thousand times a second, and on CPython that contends for the GIL with
        the UI's asyncio loop -- measured as a 0.3-0.7 s lag before a focus
        change repainted.
        """
        while self._alive:
            req = None
            with self._lock:
                if self._queue and not self._inflight:
                    req = self._queue.pop(0)
                    self._inflight[req.token] = req
            if req is None:
                self._wakeup.wait(0.2)
                self._wakeup.clear()
                continue
            line = "%d%s\n" % (req.token, req.command)
            try:
                _milog("->", line)
                self.proc.stdin.write(line.encode("utf-8", "replace"))
                self.proc.stdin.flush()
                req.sent_at = time.perf_counter()
            except (OSError, ValueError) as e:
                with self._lock:
                    self._inflight.pop(req.token, None)
                req.error = "write to gdb failed: %s" % e
                req.done.set()
        # drain anything left so waiters do not hang forever
        with self._lock:
            pending = self._queue[:]
            self._queue.clear()
            inflight = list(self._inflight.values())
        for r in pending + inflight:
            r.error = r.error or "gdb exited"
            r.done.set()

    def _read_loop(self):
        while True:
            try:
                chunk = self.proc.stdout.read(65536)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            self.stats["bytes"] += len(chunk)
            for line in self._splitter.feed(chunk):
                self.stats["lines"] += 1
                _milog("<-", line)
                rec = mi.parse_line(line)
                if rec is None:
                    continue
                # Resolve request/response pairing HERE, on the reader thread.
                # Callbacks run on a separate thread (see _dispatch_loop), so a
                # callback that issues a blocking call() cannot deadlock the
                # reader that would have to deliver its answer.
                if rec.kind in (mi.RESULT, mi.PROMPT) and rec.token is not None:
                    self._complete(rec)
                self._events.put(rec)
        self._alive = False
        code = None
        if self.proc is not None:
            try:
                code = self.proc.wait()
            except Exception:
                code = None
        self.exit_code = code
        self._events.put(_EOF)
        cb = self.cb.get("on_exit")
        if cb:
            try:
                cb(code)
            except Exception:
                pass

    def _dispatch_loop(self):
        """Deliver records to user callbacks, off the reader thread."""
        while True:
            rec = self._events.get()
            if rec is _EOF:
                return
            self._dispatch(rec)

    def _dispatch(self, rec):
        """Deliver one record to the user callbacks.

        Runs on the dispatch thread. Request/response pairing already happened
        on the reader thread, so a callback may safely block in call().
        """
        if rec is None:
            return
        self.stats["records"] += 1
        if rec.kind == mi.PARSE_ERROR:
            self.stats["parse_errors"] += 1
            self._fire("on_log", rec.raw + "\n")
            return
        if rec.kind == mi.PROMPT:
            self.at_prompt = True
            return
        if rec.kind == mi.STREAM:
            self.at_prompt = False
            if rec.stream_kind == mi.STREAM_CONSOLE:
                self._fire("on_console", rec.payload)
            elif rec.stream_kind == mi.STREAM_TARGET:
                self._fire("on_target", rec.payload)
            else:
                self._fire("on_log", rec.payload)
            return
        if rec.kind == mi.ASYNC:
            self.at_prompt = False
            if rec.cls == "stopped":
                self._fire("on_stopped", rec.payload)
            elif rec.cls == "running":
                self._fire("on_running", rec.payload)
            else:
                self._fire("on_notify", rec.cls, rec.payload)
            return
        if rec.kind == mi.RESULT:
            self.at_prompt = False
            if rec.cls == "error":
                msg = (rec.payload or {}).get("msg", "gdb error")
                self._fire("on_console", "gdb: %s\n" % msg)
            return

    def _complete(self, rec):
        tok = rec.token
        if tok is None:
            return
        with self._lock:
            req = self._inflight.pop(tok, None)
        if req is None:
            return
        req.record = rec
        if rec.kind == mi.RESULT and rec.cls == "error":
            req.error = (rec.payload or {}).get("msg", "gdb error")
        req.done.set()

    def _fire(self, name, *a):
        cb = self.cb.get(name)
        if not cb:
            return
        try:
            cb(*a)
        except Exception:
            # a buggy UI callback must not kill the reader thread
            import traceback
            sys.stderr.write("cgdbpy: %s callback failed:\n%s"
                             % (name, traceback.format_exc()))
