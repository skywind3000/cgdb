"""The inferior's terminal.

gdb must be told where the debugged program's terminal is, otherwise the
inferior's stdout lands in the MI pipe and corrupts the record stream -- the
C port verified this experimentally (doc/win_specs.md §4, E2a).

POSIX: a pty pair; gdb gets the slave name, we read the master. The pty
behaves exactly like the C version's shared console pty (probed on
gdb 15.1): gdb puts the inferior in the slave's foreground process
group, so writing VINTR (0x03) to the master delivers SIGINT to the
inferior, and the cooked line discipline echoes typed input back to the
master -- cgdb never echoes by hand.

Windows: gdb gets `new-console on`. The inferior is not wired to cgdb at
all: console-subsystem programs get their own visible console window
(the Visual Studio convention, adopted from the seer-windows port),
where scanf/ANSI/Ctrl-C are handled natively by conhost;
GUI-subsystem programs get no window. PoC-verified on gdb 17.1
(.tmp/poc-newcon): the MI stream stays clean, the popup works for
interactive stdin, and the CTRL_BREAK interrupt path keeps working when
the inferior owns a separate console.
"""

import os
import sys
import threading


class InferiorTty:
    """Base interface: start() prepares the terminal for gdb.

    ``embedded`` marks where the inferior's terminal lives: True when it
    is *inside* cgdb's own screen (the POSIX pty feeding the VT in the
    GDB window), False when the program runs in its own window and cgdb
    is not part of its I/O at all (Windows new-console). The app branches
    on this instead of on sys.platform.
    """

    embedded = False

    def start(self):
        raise NotImplementedError

    def mi_command(self):
        """The full MI command that configures the inferior's terminal."""
        raise NotImplementedError

    def verify_command(self):
        """The MI command whose ^done payload proves the setting took."""
        raise NotImplementedError

    def verify_ok(self, payload):
        """True when the verify_command() payload confirms the setting."""
        raise NotImplementedError

    def write(self, data):
        """Deliver bytes to the inferior's stdin. Returns success."""
        raise NotImplementedError

    def request_flush(self, timeout=0.5):
        """Drain pending output once the master goes quiet.

        POSIX only: the app calls this when the inferior stops, so the
        reader thread can pull the last bytes out of the pty and hand the
        terminal's content to the caller's ``on_drain`` *before* gdb's
        stop report is appended to the console. Returns True when the
        flush completed (or the reader is gone and the caller must
        consume whatever is left itself).
        """
        return True

    def close(self):
        pass


class PtyInferiorTty(InferiorTty):

    embedded = True

    def __init__(self, on_output, on_drain=None):
        import pty
        self._pty = pty
        self.on_output = on_output
        # runs on the reader thread after a request_flush() drain, see
        # request_flush()
        self.on_drain = on_drain
        self.master = -1
        self.slave = -1
        self.name = None
        self._alive = False
        self._thread = None
        self._flush_request = threading.Event()
        self._flush_done = threading.Event()

    def start(self):
        self.master, self.slave = self._pty.openpty()
        self.name = os.ttyname(self.slave)
        self._alive = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True,
                                        name="inferior-pty")
        self._thread.start()
        return self.name

    def mi_command(self):
        # the C port uses -inferior-tty-set on POSIX (tgdb.cpp). The device
        # path must be inserted **unquoted**: `-inferior-tty-set` takes a
        # raw filename, not an MI c-string (see gdb_argument() history:
        # quoting made gdb reply ^done and silently fail to open the pty).
        return "-inferior-tty-set %s" % self.name

    def verify_command(self):
        return "-inferior-tty-show"

    def verify_ok(self, payload):
        got = (payload or {}).get("inferior_tty", "")
        return self.name in got or got in self.name

    def _read_loop(self):
        # select-based rather than a blocking read: the loop has to notice
        # request_flush() within one timeout, and close() within one too.
        import select
        while self._alive:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.05)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    data = os.read(self.master, 4096)
                except OSError:
                    break
                if not data:
                    break
                self._emit(data)
            elif self._flush_request.is_set():
                # the master is quiet: pull anything left, then let the
                # app move the terminal's content into the console
                self._drain()
                self._flush_request.clear()
                if self.on_drain is not None:
                    try:
                        self.on_drain()
                    except Exception:
                        pass
                self._flush_done.set()

    def _drain(self):
        import select
        while True:
            try:
                ready, _, _ = select.select([self.master], [], [], 0)
            except (OSError, ValueError):
                return
            if not ready:
                return
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            self._emit(data)

    def request_flush(self, timeout=0.5):
        if not self._alive or self._thread is None \
                or not self._thread.is_alive():
            return True
        self._flush_done.clear()
        self._flush_request.set()
        return self._flush_done.wait(timeout)

    def _emit(self, data):
        try:
            self.on_output(data)
        except Exception:
            pass

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        try:
            os.write(self.master, data)
            return True
        except OSError:
            return False

    def resize(self, rows, cols):
        try:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def close(self):
        self._alive = False
        for fd in (self.master, self.slave):
            try:
                if fd >= 0:
                    os.close(fd)
            except OSError:
                pass
        self.master = self.slave = -1


class NewConsoleTty(InferiorTty):
    """Windows: the inferior gets its own console window (gdb
    ``new-console on``); cgdb is not part of its I/O path at all."""

    def __init__(self, on_output, on_drain=None):
        self.on_output = on_output  # never fires: output goes to the window
        self.on_drain = None        # nothing embedded to flatten

    def start(self):
        return ""

    def mi_command(self):
        return "-gdb-set new-console on"

    def verify_command(self):
        return "-gdb-show new-console"

    def verify_ok(self, payload):
        return (payload or {}).get("value") == "on"

    def write(self, data):
        # The inferior reads from its own console window; there is no
        # cgdb-side stdin to feed (keys typed into cgdb while the inferior
        # runs are dropped, like the C port drops them).
        return False

    def resize(self, rows, cols):
        pass

    def close(self):
        pass


def create(on_output, on_drain=None):
    """Platform-appropriate inferior terminal."""
    if sys.platform == "win32":
        return NewConsoleTty(on_output, on_drain)
    return PtyInferiorTty(on_output, on_drain)
