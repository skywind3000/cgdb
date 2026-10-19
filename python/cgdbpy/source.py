"""Source buffers and syntax highlighting.

Python counterpart of cgdb/sources.cpp + lib/tokenizer.

Highlighting strategy is forced by a measurement, not by taste
(doc/python_specs.md §3.6): lexing only the visible window is WRONG for
C/C++, because a window that starts inside a `/* ... */` block or a raw string
gets coloured as code. On lib/tgdb/tgdb.cpp, 6 of 11 sampled 40-line windows
diverged from the full-file lex, two of them on 35-36 lines out of 40.

So: lex the whole file once, cache a per-line token list, and do it on a
background thread for anything large. Until the cache is ready the view shows
plain text -- correct, just uncoloured.
"""

import os
import re
import threading

from pygments import lex
from pygments.lexers import (get_lexer_by_name, get_lexer_for_filename,
                             guess_lexer, TextLexer)
from pygments.token import Token
from pygments.util import ClassNotFound

# Above this many lines, lexing moves to a background thread. Measured cost is
# ~28 us/line, so 20k lines is ~0.6 s -- too long to block a keypress.
ASYNC_LEX_THRESHOLD = 4000


class SourceFile:
    """One loaded source file."""

    def __init__(self, path, text=None, encoding=None):
        self.path = path
        self.name = os.path.basename(path) if path else "<unknown>"
        self.encoding = encoding
        if text is None:
            text, self.encoding = self._read(path)
        # keepends=False but remember whether the file was CRLF so that
        # re-display and column maths stay consistent (testsuite cases_files
        # exercises CRLF + CJK sources)
        self.text = text
        self.lines = text.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.crlf = "\r\n" in text
        if self.crlf:
            self.lines = [l[:-1] if l.endswith("\r") else l for l in self.lines]
        self._tokens = None       # list[list[(token, str)]] per line
        self._lex_state = "none"  # none | pending | ready | failed
        self._lock = threading.Lock()

    @staticmethod
    def _read(path):
        """Read a source file, tolerating mixed/unknown encodings.

        Tries UTF-8 first (with BOM), then the locale/ANSI code page, and
        finally falls back to latin-1 so that a byte is never lost.
        """
        with open(path, "rb") as f:
            raw = f.read()
        for enc in ("utf-8-sig", "utf-8"):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                continue
        try:
            import locale
            enc = locale.getpreferredencoding(False) or "cp1252"
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            pass
        return raw.decode("latin-1"), "latin-1"

    def __len__(self):
        return len(self.lines)

    def line(self, i):
        if 0 <= i < len(self.lines):
            return self.lines[i]
        return ""

    # --- highlighting -----------------------------------------------------
    @property
    def lex_state(self):
        return self._lex_state

    def lexer(self):
        forced = getattr(self, "_forced_lexer", None)
        if forced is not None:
            # :syntax <name> -- force a lexer ("" means plain text, "off")
            if forced in ("", "off"):
                return TextLexer()
            try:
                return get_lexer_by_name(forced, stripnl=False,
                                         stripall=False, ensurenl=False)
            except Exception:
                pass              # unknown name: fall through to detection
        try:
            return get_lexer_for_filename(self.path, stripnl=False,
                                          stripall=False, ensurenl=False)
        except ClassNotFound:
            pass
        try:
            return guess_lexer(self.text[:4096])
        except (ClassNotFound, Exception):
            return TextLexer()

    def force_lexer(self, name):
        """Override the lexer (``:syntax``), then re-highlight.

        `name` is a Pygments lexer alias; "" or "off" disables colour;
        None returns to filename/content detection. Raises ValueError for
        an alias Pygments does not know.
        """
        if name not in (None, "", "off"):
            try:
                get_lexer_by_name(name)
            except Exception:
                raise ValueError("unknown language: %s" % name)
        self._forced_lexer = name
        with self._lock:
            self._tokens = None
            self._lex_state = "none"
        self.ensure_highlighted()

    def highlight_sync(self):
        """Lex the whole file now and cache per-line tokens."""
        lx = self.lexer()
        per_line = [[] for _ in self.lines]
        line = 0
        for tok, val in lex(self.text, lx):
            if line >= len(per_line):
                break
            parts = val.split("\n")
            for j, part in enumerate(parts):
                target = line + j
                if target >= len(per_line):
                    break
                if part:
                    per_line[target].append((tok, part))
            line += len(parts) - 1
        with self._lock:
            self._tokens = per_line
            self._lex_state = "ready"
        return per_line

    def highlight_async(self, on_done=None):
        """Lex on a background thread. Returns immediately."""
        with self._lock:
            if self._lex_state in ("pending", "ready"):
                return
            self._lex_state = "pending"

        def work():
            try:
                self.highlight_sync()
            except Exception:
                with self._lock:
                    self._lex_state = "failed"
            if on_done:
                on_done(self)

        t = threading.Thread(target=work, daemon=True,
                             name="lex-%s" % self.name)
        t.start()
        return t

    def ensure_highlighted(self):
        """Small files lex inline; large ones go async."""
        with self._lock:
            if self._lex_state in ("pending", "ready"):
                return
        if len(self.lines) <= ASYNC_LEX_THRESHOLD:
            self.highlight_sync()
        else:
            self.highlight_async()

    def tokens_for_line(self, i):
        """[(token, text)] for line i, or None if not highlighted yet."""
        with self._lock:
            if self._tokens is None or i >= len(self._tokens):
                return None
            return self._tokens[i]


class Breakpoints:
    """Breakpoint table, refreshed from -break-info / async notifications."""

    def __init__(self):
        self._by_id = {}

    def update_from_mi(self, payload):
        """Replace the table from a `-break-info` result record.

        gdb returns BreakpointTable={body=[bkpt={...}, ...]}. A single
        breakpoint comes back as a dict rather than a one-element list, which
        is the classic MI parsing trap.
        """
        table = (payload or {}).get("BreakpointTable") or {}
        body = table.get("body")
        if body is None:
            body = []
        if isinstance(body, dict):
            body = [body]
        new = {}
        for entry in body:
            bk = entry.get("bkpt") if isinstance(entry, dict) else None
            if bk is None:
                continue
            if isinstance(bk, list):
                bk = bk[0]
            self._absorb(new, bk)
        self._by_id = new

    def _absorb(self, into, bk):
        number = bk.get("number")
        if number is None:
            return
        # "1.1" style locations belong to parent breakpoint "1"
        base = str(number).split(".")[0]
        enabled = bk.get("enabled", "y") == "y"
        fullname = bk.get("fullname") or bk.get("file")
        line = bk.get("line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        rec = into.setdefault(base, {"number": base, "enabled": enabled,
                                     "locations": []})
        rec["enabled"] = rec["enabled"] or enabled
        if fullname and line:
            rec["locations"].append((fullname, line))

    def absorb_async(self, cls, payload):
        """Handle =breakpoint-created / -modified / -deleted.

        The three do not share a payload shape, which is easy to get wrong:
          =breakpoint-created,bkpt={number="1",...}
          =breakpoint-modified,bkpt={number="1",...}
          =breakpoint-deleted,id="1"          <-- an id, NOT a bkpt tuple
        Verified against gdb 17.1.
        """
        payload = payload or {}
        if cls == "breakpoint-deleted":
            number = payload.get("id")
            if number is None:
                bk = payload.get("bkpt")
                if isinstance(bk, list):
                    bk = bk[0] if bk else {}
                number = (bk or {}).get("number")
            if number is None:
                return False
            return self._by_id.pop(str(number).split(".")[0], None) is not None

        bk = payload.get("bkpt")
        if bk is None:
            return False
        if isinstance(bk, list):
            bk = bk[0]
        tmp = {}
        self._absorb(tmp, bk)
        changed = False
        for k, v in tmp.items():
            existing = self._by_id.get(k)
            if existing:
                if existing["enabled"] != v["enabled"]:
                    changed = True
                existing["enabled"] = v["enabled"]
                for loc in v["locations"]:
                    if loc not in existing["locations"]:
                        existing["locations"].append(loc)
                        changed = True
            else:
                self._by_id[k] = v
                changed = True
        return changed

    def absorb_result_bkpt(self, payload):
        """Take a breakpoint from a `^done,bkpt={...}` result record.

        `-break-insert` does NOT emit =breakpoint-created; the new breakpoint
        arrives only in the command's result. Verified against gdb 17.1.
        """
        bk = (payload or {}).get("bkpt")
        if bk is None:
            return False
        if isinstance(bk, list):
            bk = bk[0]
        tmp = {}
        self._absorb(tmp, bk)
        changed = False
        for k, v in tmp.items():
            if k not in self._by_id:
                changed = True
            self._by_id[k] = v
        return changed

    def clear(self):
        self._by_id = {}

    def numbers(self):
        return sorted(self._by_id, key=lambda s: int(s) if s.isdigit() else 0)

    def all(self):
        return [self._by_id[n] for n in self.numbers()]

    def at(self, path, line):
        """Breakpoint numbers set on path:line (0-based line -> 1-based gdb)."""
        if not path:
            return []
        want = os.path.normcase(os.path.abspath(path))
        out = []
        for n in self.numbers():
            for (f, l) in self._by_id[n]["locations"]:
                if l == line and os.path.normcase(os.path.abspath(f)) == want:
                    out.append(n)
                    break
        return out


class SourceView:
    """Window state over one SourceFile: top line, cursor, marks, search."""

    def __init__(self, height=20):
        self.height = max(1, height)
        self.top = 0
        self.left = 0          # horizontal scroll of the text columns
        self.cursor = 0          # 0-based line index
        self.marks = {}          # 'a'..'z' -> line index
        self.last_line_col = 0
        self.search_pattern = None
        self.search_matches = []
        self.wrapscan = True

    def resize(self, height):
        self.height = max(1, height)

    def hscroll(self, delta):
        """source_hscroll: shift the visible text columns, floors at 0."""
        self.left = max(0, self.left + delta)
        return self.left

    def ensure_visible(self, n=None):
        """Scroll minimally so the cursor line is on screen."""
        if n is None:
            n = self.cursor
        if n < self.top:
            self.top = n
        elif n >= self.top + self.height:
            self.top = n - self.height + 1
        self.top = max(0, self.top)

    def center(self, total):
        self.top = max(0, min(self.cursor - self.height // 2,
                              max(0, total - self.height)))

    def move_to(self, n, total):
        self.cursor = max(0, min(n, total - 1)) if total else 0
        self.ensure_visible()

    def scroll(self, n, total):
        self.top = max(0, min(self.top + n, max(0, total - self.height)))

    def set_mark(self, ch):
        self.marks[ch] = self.cursor

    def goto_mark(self, ch):
        return self.marks.get(ch)

    # --- search -----------------------------------------------------------
    def search_in(self, src, pattern=None, forward=True, ignorecase=False):
        rx = None
        if pattern:
            try:
                rx = re.compile(pattern, re.IGNORECASE if ignorecase else 0)
            except re.error:
                return None
            self.search_pattern = rx
        rx = rx or self.search_pattern
        if rx is None:
            return None
        total = len(src)
        order = list(range(self.cursor + 1, total)) if forward \
            else list(range(self.cursor - 1, -1, -1))
        if self.wrapscan:
            if forward:
                order += list(range(0, self.cursor + 1))
            else:
                order += list(range(total - 1, self.cursor - 1, -1))
        for i in order:
            if rx.search(src.line(i)):
                self.move_to(i, total)
                return i
        return None
