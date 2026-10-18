"""The inferior's terminal.

gdb must be told where the debugged program's tty is, otherwise the inferior's
stdout lands in the MI pipe and corrupts the record stream -- the C port
verified this experimentally (doc/win_specs.md §4, E2a).

POSIX: a pty pair; gdb gets the slave name, we read the master.
Windows: a named pipe; gdb gets `\\\\.\\pipe\\<name>`.

The Windows path uses overlapped I/O on purpose. win_specs.md §7.2 records the
trap: on a *synchronous* named pipe, a reader thread parked in ReadFile makes a
WriteFile on the same handle deadlock. Both directions are therefore issued as
OVERLAPPED operations with their own events.
"""

import os
import sys
import threading


class InferiorTty:
    """Base interface: start() returns the device name to hand to gdb."""

    def start(self):
        raise NotImplementedError

    def gdb_argument(self):
        """The exact text to put on the MI command line.

        This must be inserted **unquoted**. `-gdb-set inferior-tty` takes a raw
        filename, not an MI c-string: wrapping it in double quotes makes gdb
        reply `^done` and then silently fail to open the device, so the
        inferior's stdout leaks into the MI pipe as unparseable lines. Measured
        on gdb 17.1 -- quoted raw-backslash, quoted forward-slash and quoted
        `mi.quote()` forms all fail; both unquoted forms work. The C port says
        the same thing at tgdb.cpp:1990 ("Raw backslashes survive the MI
        argument parser").
        """
        raise NotImplementedError

    def mi_command(self):
        """The full MI command that points gdb at this tty."""
        raise NotImplementedError

    def write(self, data):
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

    def gdb_argument(self):
        return self.name

    def mi_command(self):
        # the C port uses -inferior-tty-set on POSIX (tgdb.cpp:1995)
        return "-inferior-tty-set %s" % self.gdb_argument()

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


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32

    PIPE_ACCESS_DUPLEX = 0x00000003
    FILE_FLAG_OVERLAPPED = 0x40000000
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    ERROR_IO_PENDING = 997
    ERROR_BROKEN_PIPE = 109
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INFINITE = 0xFFFFFFFF

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.POINTER(ctypes.c_ulong)),
            ("InternalHigh", ctypes.POINTER(ctypes.c_ulong)),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    k32.CreateNamedPipeW.restype = wintypes.HANDLE
    k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p]
    k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(OVERLAPPED)]
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD),
                             ctypes.POINTER(OVERLAPPED)]
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD),
                              ctypes.POINTER(OVERLAPPED)]


class PipeInferiorTty(InferiorTty):
    """Windows named-pipe inferior tty with overlapped read and write."""

    BUFSIZE = 65536

    def __init__(self, on_output, name=None):
        self.on_output = on_output
        self.name = name or ("cgdbpy_inf_%d" % os.getpid())
        self.path = "\\\\.\\pipe\\%s" % self.name
        self.handle = None
        self._alive = False
        self._connected = False
        self._thread = None

    def start(self):
        self.handle = k32.CreateNamedPipeW(
            self.path,
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1, self.BUFSIZE, self.BUFSIZE, 0, None)
        if self.handle == wintypes.HANDLE(-1).value or not self.handle:
            raise OSError("CreateNamedPipeW failed: %d" % k32.GetLastError())
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="inferior-pipe")
        self._thread.start()
        return self.path

    def gdb_argument(self):
        # Raw backslashes, unquoted -- see InferiorTty.gdb_argument().
        return self.path

    def mi_command(self):
        # the C port uses -gdb-set inferior-tty on Windows (tgdb.cpp:1992)
        return "-gdb-set inferior-tty %s" % self.gdb_argument()

    def _run(self):
        ov = OVERLAPPED()
        ov.hEvent = k32.CreateEventW(None, True, False, None)
        rc = k32.ConnectNamedPipe(self.handle, ctypes.byref(ov))
        if not rc:
            err = k32.GetLastError()
            if err == ERROR_IO_PENDING:
                k32.WaitForSingleObject(ov.hEvent, INFINITE)
            elif err != 535:        # ERROR_PIPE_CONNECTED
                k32.CloseHandle(ov.hEvent)
                return
        self._connected = True
        k32.ResetEvent(ov.hEvent)
        buf = ctypes.create_string_buffer(self.BUFSIZE)
        while self._alive:
            got = wintypes.DWORD(0)
            ok = k32.ReadFile(self.handle, buf, self.BUFSIZE,
                              ctypes.byref(got), ctypes.byref(ov))
            if not ok:
                err = k32.GetLastError()
                if err != ERROR_IO_PENDING:
                    break
                if k32.WaitForSingleObject(ov.hEvent, 200) != 0:
                    continue
                if not k32.GetOverlappedResult(self.handle,
                                               ctypes.byref(ov),
                                               ctypes.byref(got), False):
                    break
            n = got.value
            if n == 0:
                break
            try:
                self.on_output(buf.raw[:n])
            except Exception:
                pass
        k32.CloseHandle(ov.hEvent)

    def write(self, data):
        if not self._connected or not self._alive:
            return False
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if not data:
            return True
        ov = OVERLAPPED()
        ov.hEvent = k32.CreateEventW(None, True, False, None)
        written = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(data, len(data))
        ok = k32.WriteFile(self.handle, buf, len(data), ctypes.byref(written),
                           ctypes.byref(ov))
        if not ok and k32.GetLastError() == ERROR_IO_PENDING:
            k32.WaitForSingleObject(ov.hEvent, 5000)
            ok = k32.GetOverlappedResult(self.handle, ctypes.byref(ov),
                                         ctypes.byref(written), False)
        k32.CloseHandle(ov.hEvent)
        return bool(ok)

    def close(self):
        self._alive = False
        if self.handle:
            try:
                k32.CancelIo(self.handle)
                k32.DisconnectNamedPipe(self.handle)
                k32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


def create(on_output):
    """Platform-appropriate inferior tty."""
    if sys.platform == "win32":
        return PipeInferiorTty(on_output)
    return PtyInferiorTty(on_output)
