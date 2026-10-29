"""Pty harness for the POSIX end-to-end tests -- tests/e2e's conpty sibling.

The app is spawned as a session leader on a fresh pty (os.forkpty), so it
believes it owns a real terminal. Keystrokes are written to the master with
real timing; everything the app paints is fed into a pyte Screen, so tests
assert on the *rendered* result (text, position, colour) instead of raw
escape soup -- exactly how the /usr/bin/cgdb 0.8.0 baseline was probed.
"""

import fcntl
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

import pyte


class PtyDriver:
    def __init__(self, argv, rows=40, cols=100, env=None):
        self.rows, self.cols = rows, cols
        self.raw = bytearray()
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._reaped = None
        e = dict(os.environ)
        e["TERM"] = "xterm-256color"
        e.pop("PYTHONHOME", None)
        if env:
            e.update(env)
        self.pid, self.master = self._spawn(argv, e)
        self._set_winsz(rows, cols)

    def _spawn(self, argv, env):
        pid, master = os.forkpty()
        if pid == 0:
            try:
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        return pid, master

    def _set_winsz(self, rows, cols):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))

    def set_size(self, rows, cols):
        self.rows, self.cols = rows, cols
        self._set_winsz(rows, cols)
        self._screen.resize(lines=rows, columns=cols)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        while data:
            n = os.write(self.master, data)
            data = data[n:]

    def pump(self, duration=0.05):
        """Read for `duration` seconds; False when the app closed the tty."""
        end = time.time() + duration
        while True:
            timeout = max(0.0, end - time.time())
            if timeout <= 0:
                return True
            r, _, _ = select.select([self.master], [], [], timeout)
            if not r:
                return True
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return False
            if not chunk:
                return False
            self.raw.extend(chunk)
            self._stream.feed(chunk)

    def wait_for(self, pattern, timeout=10):
        if isinstance(pattern, str):
            pattern = pattern.encode()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pattern in bytes(self.raw):
                return True
            if not self.pump(0.05):
                break
        return pattern in bytes(self.raw)

    def screen_rows(self):
        return [row.rstrip() for row in self._screen.display]

    def screen_text(self):
        return "\n".join(self.screen_rows())

    def char_at(self, y, x):
        return self._screen.buffer[y][x]

    def find(self, text):
        """(y, x) of the first screen occurrence, or None."""
        for y, row in enumerate(self.screen_rows()):
            x = row.find(text)
            if x >= 0:
                return (y, x)
        return None

    def exited(self):
        """True once the child has terminated (reaping it).

        os.kill(pid, 0) succeeds on a ZOMBIE too, which once read as
        "the app never exits" for a whole debugging round -- the waitpid
        here is the honest check, mirroring conpty's exited().
        """
        if self._reaped is not None:
            return True
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._reaped = 0
            return True
        if pid == self.pid:
            self._reaped = status
            return True
        return False

    def exit_status(self):
        return self._reaped

    def alive(self):
        return not self.exited()

    def terminate(self):
        if not self.exited():
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except (ChildProcessError, OSError):
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


def compile_probe():
    """Compile tests/data/terminal_probe.c; skips when no compiler.

    The probe only makes sense on the POSIX pty route -- callers gate on
    sys.platform themselves.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(os.path.dirname(here), "data", "terminal_probe.c")
    cc = next((c for c in ("gcc", "cc", "clang") if shutil.which(c)), None)
    if cc is None:
        import pytest
        pytest.skip("no C compiler")
    d = tempfile.mkdtemp(prefix="cgdbpy-e2e-")
    exe = os.path.join(d, "vtinf")
    r = subprocess.run([cc, "-g", "-O0", src, "-o", exe],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not os.path.exists(exe):
        import pytest
        pytest.skip("could not compile the probe inferior: %s"
                    % r.stderr[:400])
    return exe
