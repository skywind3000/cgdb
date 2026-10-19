"""ConPTY driver for end-to-end tests of python/cgdb.py.

Uses the same wiring recipe as the C port's E2E harness (.tmp/conpty_run.py,
doc/porting-feasibility.md §3.1): CreatePseudoConsole plus a proc-thread
attribute list carrying the pseudoconsole handle, STARTF_USESTDHANDLES with
NULL std handles, and ClosePseudoConsole deferred to its own thread because it
blocks until the client detaches.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

k32 = ctypes.windll.kernel32

ROWS = int(os.environ.get("E2E_ROWS", "40"))
COLS = int(os.environ.get("E2E_COLS", "110"))


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW),
                ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


k32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE,
                                    wintypes.DWORD,
                                    ctypes.POINTER(wintypes.HANDLE)]
k32.CreatePseudoConsole.restype = ctypes.HRESULT


class VTScreen:
    """Minimal VT100 screen model: enough to assert on what the user sees."""

    def __init__(self, rows=ROWS, cols=COLS):
        self.rows, self.cols = rows, cols
        self.reset()

    def reset(self):
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        self.r = self.c = 0
        self.buf = b""

    def feed(self, data):
        self.buf += data
        while self.buf:
            b = self.buf[0]
            if b == 0x1b:
                if not self._esc():
                    return
            elif b == 0x0d:
                self.c = 0
                self.buf = self.buf[1:]
            elif b == 0x0a:
                self.nl()
                self.buf = self.buf[1:]
            elif b == 0x08:
                self.c = max(0, self.c - 1)
                self.buf = self.buf[1:]
            elif b == 0x09:
                self.c = min(self.cols - 1, (self.c // 8 + 1) * 8)
                self.buf = self.buf[1:]
            elif b < 0x20:
                self.buf = self.buf[1:]
            else:
                # decode one UTF-8 character
                n = 1
                if b >= 0xF0:
                    n = 4
                elif b >= 0xE0:
                    n = 3
                elif b >= 0xC0:
                    n = 2
                if len(self.buf) < n:
                    return
                try:
                    ch = self.buf[:n].decode("utf-8")
                except UnicodeDecodeError:
                    ch = "?"
                    n = 1
                self.buf = self.buf[n:]
                self.put(ch)

    def put(self, ch):
        if self.c >= self.cols:
            self.c = 0
            self.nl()
        for i, c in enumerate(ch):
            if self.c + i < self.cols:
                self.grid[self.r][self.c + i] = c
        self.c += max(1, len(ch))

    def nl(self):
        self.r += 1
        if self.r >= self.rows:
            self.r = self.rows - 1
            self.grid.pop(0)
            self.grid.append([" "] * self.cols)

    def _esc(self):
        b = self.buf
        if len(b) < 2:
            return False
        if b[1:2] == b"[":
            i = 2
            params = b""
            while i < len(b) and (0x30 <= b[i] <= 0x3f):
                params += b[i:i + 1]
                i += 1
            while i < len(b) and (0x20 <= b[i] <= 0x2f):
                i += 1
            if i >= len(b):
                return False
            final = b[i:i + 1]
            self.csi(params.decode("ascii", "replace"),
                    final.decode("ascii", "replace"))
            self.buf = b[i + 1:]
            return True
        if b[1:2] == b"]":
            j = b.find(b"\x07", 2)
            if j < 0:
                j = b.find(b"\x1b\\", 2)
                if j < 0:
                    return False
                self.buf = b[j + 2:]
            else:
                self.buf = b[j + 1:]
            return True
        self.buf = b[2:]
        return True

    def csi(self, params, final):
        # params may carry a private prefix such as '?' (\x1b[?25l) or '>'
        # (\x1b[>1u); strip anything that is not a digit or ';' before parsing
        cleaned = "".join(ch for ch in params if ch.isdigit() or ch == ";")
        ps = [int(x) if x else 0 for x in cleaned.split(";")] if cleaned else []
        private = bool(params) and not params[0].isdigit()
        if private:
            # mode set/reset and friends do not move the cursor or paint cells
            return
        if final == "H" or final == "f":
            self.r = min(self.rows - 1, max(0, (ps[0] if ps else 1) - 1))
            self.c = min(self.cols - 1, max(0, (ps[1] if len(ps) > 1 else 1) - 1))
        elif final == "A":
            self.r = max(0, self.r - (ps[0] if ps else 1))
        elif final == "B":
            self.r = min(self.rows - 1, self.r + (ps[0] if ps else 1))
        elif final == "C":
            self.c = min(self.cols - 1, self.c + (ps[0] if ps else 1))
        elif final == "D":
            self.c = max(0, self.c - (ps[0] if ps else 1))
        elif final == "J":
            mode = ps[0] if ps else 0
            if mode == 2 or mode == 3:
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
            elif mode == 0:
                for cc in range(self.c, self.cols):
                    self.grid[self.r][cc] = " "
                for rr in range(self.r + 1, self.rows):
                    self.grid[rr] = [" "] * self.cols
        elif final == "K":
            mode = ps[0] if ps else 0
            if mode == 0:
                for cc in range(self.c, self.cols):
                    self.grid[self.r][cc] = " "
            elif mode == 1:
                for cc in range(0, self.c + 1):
                    self.grid[self.r][cc] = " "
            else:
                self.grid[self.r] = [" "] * self.cols
        elif final == "X":
            # ECH: erase `n` characters at the cursor, cursor does not move.
            # prompt_toolkit emits this instead of writing spaces when a row
            # gets shorter -- e.g. when the status bar is replaced by a shorter
            # `:` command line. Dropping it left the old text on screen and made
            # the swap look like a rendering bug in cgdb.
            n = ps[0] if ps and ps[0] else 1
            for cc in range(self.c, min(self.cols, self.c + n)):
                self.grid[self.r][cc] = " "
        elif final == "L":
            n = ps[0] if ps else 1
            for _ in range(n):
                if self.r < self.rows:
                    self.grid.pop()
                    self.grid.insert(self.r, [" "] * self.cols)
        elif final == "M":
            n = ps[0] if ps else 1
            for _ in range(n):
                if self.r < self.rows:
                    del self.grid[self.r]
                    self.grid.append([" "] * self.cols)

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.grid)

    def line(self, i):
        return "".join(self.grid[i]).rstrip() if 0 <= i < self.rows else ""

    def find(self, needle):
        t = self.text()
        return needle in t

    def row_of(self, needle):
        for i, row in enumerate(self.grid):
            if needle in "".join(row):
                return i
        return -1


class ConPtySession:
    def __init__(self, argv, rows=ROWS, cols=COLS, cwd=None, env=None):
        self.argv = argv
        self.rows, self.cols = rows, cols
        self.cwd = cwd
        self.env = env
        self.screen = VTScreen(rows, cols)
        self.raw = bytearray()
        self.parse_errors = []
        self._alive = True
        self._lock = threading.Lock()

    def __enter__(self):
        hInR, hInW = wintypes.HANDLE(), wintypes.HANDLE()
        hOutR, hOutW = wintypes.HANDLE(), wintypes.HANDLE()
        assert k32.CreatePipe(ctypes.byref(hInR), ctypes.byref(hInW), None, 0)
        assert k32.CreatePipe(ctypes.byref(hOutR), ctypes.byref(hOutW), None, 0)
        self.hPC = wintypes.HANDLE()
        hr = k32.CreatePseudoConsole(COORD(self.cols, self.rows), hInR, hOutW,
                                     0, ctypes.byref(self.hPC))
        assert hr == 0, "CreatePseudoConsole failed: %#x" % hr

        al_size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(al_size))
        al_buf = ctypes.create_string_buffer(al_size.value)
        assert k32.InitializeProcThreadAttributeList(
            al_buf, 1, 0, ctypes.byref(al_size))
        assert k32.UpdateProcThreadAttribute(
            al_buf, 0, 0x00020016, self.hPC.value,
            ctypes.sizeof(wintypes.HANDLE), None, None)

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.lpAttributeList = ctypes.cast(al_buf, ctypes.c_void_p)
        si.StartupInfo.dwFlags = 0x100
        si.StartupInfo.hStdInput = wintypes.HANDLE(0)
        si.StartupInfo.hStdOutput = wintypes.HANDLE(0)
        si.StartupInfo.hStdError = wintypes.HANDLE(0)

        cmdline = subprocess.list2cmdline(self.argv)
        pi = PROCESS_INFORMATION()
        ok = k32.CreateProcessW(None, ctypes.create_unicode_buffer(cmdline),
                                None, None, False, 0x00080000, None,
                                self.cwd, ctypes.byref(si.StartupInfo),
                                ctypes.byref(pi))
        assert ok, "CreateProcessW failed: %d" % k32.GetLastError()
        self.pi = pi
        self.hInW = hInW
        self.hOutR = hOutR
        k32.CloseHandle(hInR)
        k32.CloseHandle(hOutW)
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def _reader(self):
        got = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(65536)
        while self._alive:
            if not k32.ReadFile(self.hOutR, buf, 65536, ctypes.byref(got),
                                None):
                break
            if got.value == 0:
                break
            chunk = buf.raw[:got.value]
            with self._lock:
                self.raw.extend(chunk)
                try:
                    self.screen.feed(chunk)
                except Exception as e:      # a parser bug must not blank the
                    self.parse_errors.append(repr(e))   # screen silently

    def send(self, text):
        data = text.encode("utf-8") if isinstance(text, str) else text
        n = wintypes.DWORD(0)
        k32.WriteFile(self.hInW, data, len(data), ctypes.byref(n), None)

    def send_key(self, name):
        self.send(KEYS[name])

    def wait_for(self, needle, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if self.screen.find(needle):
                    return True
            time.sleep(0.05)
        return False

    def settle(self, seconds=0.6):
        time.sleep(seconds)

    def snapshot(self):
        with self._lock:
            return self.screen.text()

    def exited(self):
        STILL_ACTIVE = 259
        code = wintypes.DWORD(0)
        if not k32.GetExitCodeProcess(self.pi.hProcess, ctypes.byref(code)):
            return True
        return code.value != STILL_ACTIVE

    def __exit__(self, *exc):
        self._alive = False
        try:
            k32.TerminateProcess(self.pi.hProcess, 0)
        except Exception:
            pass
        # ClosePseudoConsole blocks until the client detaches, so defer it
        hPC, hInW, hOutR = self.hPC, self.hInW, self.hOutR
        pi = self.pi

        def cleanup():
            time.sleep(0.3)
            try:
                k32.ClosePseudoConsole(hPC)
            except Exception:
                pass
            try:
                k32.CloseHandle(hInW)
                k32.CloseHandle(hOutR)
                k32.CloseHandle(pi.hProcess)
                k32.CloseHandle(pi.hThread)
            except Exception:
                pass

        threading.Thread(target=cleanup, daemon=True).start()
        time.sleep(0.5)
        return False


KEYS = {
    "esc": "\x1b",
    "enter": "\r",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "backspace": "\x7f",
    "tab": "\t",
    "ctrl-c": "\x03",
    "ctrl-l": "\x0c",
    "ctrl-u": "\x15",
    "ctrl-d": "\x04",
}
_FKEYS = {
    1: "\x1bOP", 2: "\x1bOQ", 3: "\x1bOR", 4: "\x1bOS",
    5: "\x1b[15~", 6: "\x1b[17~", 7: "\x1b[18~", 8: "\x1b[19~",
    9: "\x1b[20~", 10: "\x1b[21~", 11: "\x1b[23~", 12: "\x1b[24~",
}
for _i, _seq in _FKEYS.items():
    KEYS["f%d" % _i] = _seq
