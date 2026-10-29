"""The inferior's terminal.

gdb must be told where the debugged program's terminal is, otherwise the
inferior's stdout lands in the MI pipe and corrupts the record stream -- the
C port verified this experimentally (doc/win_specs.md §4, E2a).

POSIX: a pty pair; gdb gets the slave name, we read the master.

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
    """Base interface: start() prepares the terminal for gdb."""

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

    def close(self):
        pass


class PtyInferiorTty(InferiorTty):
    def __init__(self, on_output):
        import pty
        self._pty = pty
        self.on_output = on_output
        self.master = -1
        self.slave = -1
        self.name = None
        self._alive = False
        self._thread = None

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
        while self._alive:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                break
            if not data:
                break
            self._emit(data)

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

    def __init__(self, on_output):
        self.on_output = on_output  # never fires: output goes to the window

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


def create(on_output):
    """Platform-appropriate inferior terminal."""
    if sys.platform == "win32":
        return NewConsoleTty(on_output)
    return PtyInferiorTty(on_output)
