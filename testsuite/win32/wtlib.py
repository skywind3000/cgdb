# -*- coding: utf-8 -*-
"""wtlib.py - infrastructure for the cgdb Windows test suite.

Runs the real .tmp/win-build/cgdb.exe inside a ConPTY, drives it with
VT keystrokes, parses the rendered screen with a small VT100 parser,
and provides the pass/fail bookkeeping. Test case modules only use
Session / check / the inferior factory from here.

Usage: python testsuite/win32/run_tests.py  (from the repo root)
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

k32 = ctypes.windll.kernel32

REPO = os.path.normpath(os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "..", ".."))
CGDB = os.path.join(REPO, ".tmp", "win-build", "cgdb.exe")
WORK = os.path.join(REPO, ".tmp", "win-tests")

GDB = r"D:\Dev\mingw32\bin\gdb.exe"


# ---------------------------------------------------------------- screen

class Screen(object):
    """Minimal VT100 screen: enough to assert on what cgdb drew."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.reset()

    def reset(self):
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        self.attr = [[None] * self.cols for _ in range(self.rows)]
        self.x = self.y = 0
        self.sgr = {"bold": 0, "rev": 0, "ul": 0, "fg": None, "bg": None}

    def put(self, ch):
        if self.y < self.rows and self.x < self.cols:
            self.grid[self.y][self.x] = ch
            self.attr[self.y][self.x] = dict(self.sgr)
        self.x += 1

    def feed(self, data):
        text = data.decode("utf-8", "replace") if isinstance(
                data, (bytes, bytearray)) else data
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c == "\x1b":
                if i + 1 < n and text[i + 1] == "[":
                    j = i + 2
                    while j < n and not ("@" <= text[j] <= "~"):
                        j += 1
                    if j < n:
                        self.csi(text[i + 2:j], text[j])
                        i = j + 1
                        continue
                elif i + 1 < n and text[i + 1] == "]":
                    j = i + 2
                    while j < n and text[j] not in ("\x07",):
                        if text[j] == "\x1b" and j + 1 < n and \
                                text[j + 1] == "\\":
                            j += 1
                            break
                        j += 1
                    i = j + 1
                    continue
                i += 1
                continue
            if c == "\r":
                self.x = 0
            elif c == "\n":
                self.y += 1
            elif c == "\x08":
                self.x = max(0, self.x - 1)
            else:
                self.put(c)
            i += 1

    def csi(self, params, final):
        ps = [int(p) if p.isdigit() else 0
              for p in params.split(";")] or [0]
        if final in ("H", "f"):
            r = ps[0] - 1 if ps[0] > 0 else 0
            c = ps[1] - 1 if len(ps) > 1 and ps[1] > 0 else 0
            self.y, self.x = r, c
        elif final == "J":
            if not ps or ps[0] in (0, 2):
                self.grid = [[" "] * self.cols
                             for _ in range(self.rows)]
                self.attr = [[None] * self.cols
                             for _ in range(self.rows)]
        elif final == "K":
            if self.y < self.rows:
                for c in range(self.x, self.cols):
                    self.grid[self.y][c] = " "
                    self.attr[self.y][c] = None
        elif final == "m":
            self.apply_sgr(ps)

    def apply_sgr(self, ps):
        s = self.sgr
        i = 0
        while i < len(ps):
            p = ps[i]
            if p == 0:
                s.update(bold=0, rev=0, ul=0, fg=None, bg=None)
            elif p == 1:
                s["bold"] = 1
            elif p == 4:
                s["ul"] = 1
            elif p == 7:
                s["rev"] = 1
            elif 30 <= p <= 37:
                s["fg"] = p - 30
            elif p == 39:
                s["fg"] = None
            elif 40 <= p <= 47:
                s["bg"] = p - 40
            elif p == 49:
                s["bg"] = None
            elif p in (38, 48):
                i += 4 if (i + 1 < len(ps) and ps[i + 1] == 2) else 2
            i += 1

    def dump(self, rows=None):
        lines = ["".join(r).rstrip() for r in self.grid]
        return "\n".join(lines[:rows or self.rows])

    def text_rows(self):
        return ["".join(r).rstrip() for r in self.grid]

    def find_row(self, needle):
        for i, line in enumerate(self.text_rows()):
            if needle in line:
                return i
        return -1


# ---------------------------------------------------------------- conpty

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
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
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
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


k32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE,
                                    wintypes.DWORD,
                                    ctypes.POINTER(wintypes.HANDLE)]
k32.CreatePseudoConsole.restype = ctypes.HRESULT
k32.CreateProcessW.argtypes = [wintypes.LPWSTR, wintypes.LPWSTR,
                               ctypes.c_void_p, ctypes.c_void_p,
                               wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.LPWSTR,
                               ctypes.POINTER(STARTUPINFOW),
                               ctypes.POINTER(PROCESS_INFORMATION)]
k32.ResizePseudoConsole.argtypes = [wintypes.HANDLE, COORD]
k32.ResizePseudoConsole.restype = ctypes.HRESULT

ROWS, COLS = 40, 110


class Session(object):
    """One cgdb.exe instance inside a ConPTY."""

    def __init__(self, args="", exe=None, rows=ROWS, cols=COLS):
        self.rows, self.cols = rows, cols
        hInR = wintypes.HANDLE()
        hInW = wintypes.HANDLE()
        hOutR = wintypes.HANDLE()
        hOutW = wintypes.HANDLE()
        assert k32.CreatePipe(ctypes.byref(hInR), ctypes.byref(hInW), None, 0)
        assert k32.CreatePipe(ctypes.byref(hOutR), ctypes.byref(hOutW), None, 0)
        self.hInW = hInW
        hPC = wintypes.HANDLE()
        hr = k32.CreatePseudoConsole(COORD(cols, rows), hInR, hOutW, 0,
                                     ctypes.byref(hPC))
        assert hr == 0, hr
        self.hPC = hPC
        al_size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0,
                                              ctypes.byref(al_size))
        al_buf = ctypes.create_string_buffer(al_size.value)
        assert k32.InitializeProcThreadAttributeList(al_buf, 1, 0,
                                                     ctypes.byref(al_size))
        assert k32.UpdateProcThreadAttribute(al_buf, 0, 0x00020016, hPC.value,
                                             ctypes.sizeof(wintypes.HANDLE),
                                             None, None)
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.lpAttributeList = ctypes.cast(al_buf, ctypes.c_void_p)
        si.StartupInfo.dwFlags = 0x100
        si.StartupInfo.hStdInput = wintypes.HANDLE(0)
        si.StartupInfo.hStdOutput = wintypes.HANDLE(0)
        si.StartupInfo.hStdError = wintypes.HANDLE(0)
        pi = PROCESS_INFORMATION()
        cmdline = '"%s" %s' % (exe or CGDB, args)
        cmd = ctypes.create_unicode_buffer(cmdline)
        ok = k32.CreateProcessW(None, cmd, None, None, False,
                                0x00080000, None, REPO,
                                ctypes.byref(si.StartupInfo),
                                ctypes.byref(pi))
        assert ok, "CreateProcessW failed %d" % k32.GetLastError()
        k32.CloseHandle(hInR)
        k32.CloseHandle(hOutW)
        self.pi = pi
        self.chunks = []
        self.exit_code = None
        self.exited = False

        def reader():
            got = wintypes.DWORD(0)
            buf = ctypes.create_string_buffer(4096)
            while True:
                if not k32.ReadFile(hOutR, buf, 4096, ctypes.byref(got),
                                    None):
                    break
                if got.value == 0:
                    break
                self.chunks.append(buf.raw[:got.value])

        threading.Thread(target=reader, daemon=True).start()

    # ---- driving

    def write(self, data):
        got = wintypes.DWORD(0)
        return bool(k32.WriteFile(self.hInW, data, len(data),
                                  ctypes.byref(got), None))

    def keys(self, s, delay=0.15):
        """Send a string as keystrokes with small gaps (key-up records
        arrive like real typing - the timing that broke the port)."""
        for ch in s.encode("utf-8"):
            self.write(bytes([ch]))
            time.sleep(delay)

    def raw(self):
        return b"".join(self.chunks)

    def screen(self):
        sc = Screen(self.rows, self.cols)
        sc.feed(self.raw())
        return sc

    # ---- waiting

    def wait_text(self, needle, timeout=12.0, poll=0.2):
        deadline = time.time() + timeout
        nb = needle.encode("utf-8")
        while time.time() < deadline:
            if nb in self.raw():
                return True
            if self.check_exit():
                return nb in self.raw()
            time.sleep(poll)
        return False

    def check_exit(self):
        if self.exited:
            return True
        ec = wintypes.DWORD(0)
        k32.GetExitCodeProcess(self.pi.hProcess, ctypes.byref(ec))
        if ec.value != 259:
            self.exit_code = ec.value
            self.exited = True
            time.sleep(0.3)
            return True
        return False

    def wait_exit(self, timeout=12.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.check_exit():
                return self.exit_code
            time.sleep(0.2)
        return None

    def alive(self, secs=0.5):
        return self.wait_exit(secs) is None

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        return k32.ResizePseudoConsole(self.hPC, COORD(cols, rows))

    def kill(self):
        k32.TerminateProcess(self.pi.hProcess, 1)
        time.sleep(0.2)

        def teardown():
            time.sleep(0.3)
            k32.ClosePseudoConsole(self.hPC)
            k32.CloseHandle(self.hInW)
            k32.CloseHandle(self.pi.hProcess)
            k32.CloseHandle(self.pi.hThread)

        threading.Thread(target=teardown, daemon=True).start()
        time.sleep(0.4)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.kill()


# ------------------------------------------------------------- checks

_PASS = [0]
_FAIL = [0]
_FAILURES = []


def check(name, ok, detail=""):
    if ok:
        _PASS[0] += 1
        print("  [PASS] %s" % name)
    else:
        _FAIL[0] += 1
        _FAILURES.append(name)
        print("  [FAIL] %s%s" % (name, (" -- " + detail) if detail else ""))
    sys.stdout.flush()
    return ok


def summary():
    print("\n== win32 testsuite: %d passed, %d failed ==" %
          (_PASS[0], _FAIL[0]))
    if _FAILURES:
        print("failed:")
        for name in _FAILURES:
            print("  - %s" % name)
    return 1 if _FAIL[0] else 0


# ------------------------------------------------------------- inferiors

_INFERIORS = {
    # name: (source text, compile flags)
    "t_basic": (u"""#include <stdio.h>

static int add(int a, int b)
{
    return a + b;
}

int main(void)
{
    int v = add(10, 32);

    printf("BASIC: hello %d\\n", v);
    printf("BASIC: line2\\n");
    printf("BASIC: line3\\n");
    return 0;
}
""", ["-g"]),
    "t_sleep": (u"""#include <stdio.h>
#include <windows.h>

int main(void)
{
    printf("SLEEP: started\\n");
    fflush(stdout);
    Sleep(30000);
    printf("SLEEP: done\\n");
    return 0;
}
""", ["-g"]),
    "t_nodebug": (u"""#include <stdio.h>

int main(void)
{
    printf("NODEBUG: ran\\n");
    return 0;
}
""", []),
}

_CRLF_CJK = (u"#include <stdio.h>\r\n"
             u"\r\n"
             u"/* \u4f60\u597d\u4e16\u754c CJK comment */\r\n"
             u"int main(void)\r\n"
             u"{\r\n"
             u"    printf(\"CJK: ok\\n\");\r\n"
             u"    return 0;\r\n"
             u"}\r\n")
_INFERIORS["t_cjk"] = (_CRLF_CJK, ["-g"])


def compile_inferiors(gcc="gcc"):
    """Compile the test inferiors into .tmp/win-tests. Returns the
    directory (or None on failure)."""
    bin_dir = os.path.join(WORK, "bin")
    if not os.path.isdir(bin_dir):
        os.makedirs(bin_dir)
    exes = {}
    for name, (src, extra) in _INFERIORS.items():
        cpath = os.path.join(WORK, name + ".c")
        epath = os.path.join(bin_dir, name + ".exe")
        with open(cpath, "wb") as f:
            f.write(src.encode("utf-8"))
        r = subprocess.run(
            [gcc, "-static", "-O0"] + extra + ["-o", epath, cpath],
            capture_output=True, timeout=60)
        if r.returncode != 0:
            print("inferior compile failed for %s: %s" %
                  (name, r.stderr.decode("utf-8", "replace")[:200]))
            return None
        exes[name] = epath
    return exes


def prerequisites():
    """Verify cgdb.exe, gdb and gcc exist; print and stop otherwise."""
    problems = []
    if not os.path.isfile(CGDB):
        problems.append("cgdb.exe not built (%s) - run "
                        "mingw32-make -f win32/Makefile" % CGDB)
    if not os.path.isfile(GDB):
        problems.append("gdb not found (%s)" % GDB)
    if problems:
        for p in problems:
            print("ERROR: %s" % p)
        return False
    return True
