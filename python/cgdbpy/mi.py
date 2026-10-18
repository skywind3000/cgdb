"""GDB/MI record parsing.

Implements the grammar from the GDB manual ("GDB/MI output syntax"):

    output          = ( out-of-band-record )* [ result-record ] "(gdb)"
    result-record   = [ token ] "^" result-class ( "," result )*
    stream-record   = ( "~" | "@" | "&" ) c-string
    async-record    = ( "*" | "+" | "=" ) async-class ( "," result )*
    result          = variable "=" value
    value           = const | tuple | list
    tuple           = "{}" | "{" result ( "," result )* "}"
    list            = "[]" | "[" value ( "," value )* "]"
                    | "[" result ( "," result )* "]"
    const           = c-string

This is the Python counterpart of gdbwire.c (8193 lines of vendored C). It is
deliberately small: MI is line oriented and the whole parser is a few hundred
lines. See doc/python_version.md §3.1 for why we do not use pygdbmi.
"""

RESULT = "result"
STREAM = "stream"
ASYNC = "async"
PROMPT = "prompt"
PARSE_ERROR = "parse_error"

# stream record kinds
STREAM_CONSOLE = "~"
STREAM_TARGET = "@"
STREAM_LOG = "&"

# async record kinds
ASYNC_EXEC = "*"
ASYNC_STATUS = "+"
ASYNC_NOTIFY = "="


class MiParseError(ValueError):
    pass


def unescape_c_string(s):
    """Decode a GDB MI c-string body (the part between the quotes).

    Handles the C escape set plus octal `\\ddd`, which is how gdb emits ESC in
    stream records (`\\033`). Reading `\\033` as NUL followed by "33" corrupts
    every terminal escape sequence gdb passes through.
    """
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            out.append("\\")
            break
        e = s[i]
        if e in "01234567":
            # octal escape, 1 to 3 digits
            j = i
            while j < n and j - i < 3 and s[j] in "01234567":
                j += 1
            out.append(chr(int(s[i:j], 8)))
            i = j
            continue
        if e == "n":
            out.append("\n")
        elif e == "t":
            out.append("\t")
        elif e == "r":
            out.append("\r")
        elif e == "a":
            out.append("\a")
        elif e == "b":
            out.append("\b")
        elif e == "f":
            out.append("\f")
        elif e == "v":
            out.append("\v")
        elif e == "\\":
            out.append("\\")
        elif e == '"':
            out.append('"')
        elif e == "'":
            out.append("'")
        else:
            # unknown escape: keep both characters, matching gdb's tolerance
            out.append("\\")
            out.append(e)
        i += 1
    return "".join(out)


def escape_c_string(s):
    """Encode a Python string as an MI c-string body (no surrounding quotes)."""
    out = []
    for c in s:
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif c == "\a":
            out.append("\\a")
        elif c == "\b":
            out.append("\\b")
        elif c == "\f":
            out.append("\\f")
        elif c == "\v":
            out.append("\\v")
        elif ord(c) < 32:
            out.append("\\%03o" % ord(c))
        else:
            out.append(c)
    return "".join(out)


def quote(s):
    """Wrap a string as a complete MI c-string argument."""
    return '"%s"' % escape_c_string(s)


def breakpoint_location(path, line):
    """Build a `-break-insert` location for an absolute path and line number.

    The whole `path:line` must be ONE quoted MI string. Quoting only the path
    (`"C:\\dir\\f.c":12`) makes gdb 17.1 answer
    `^error,msg="Problem parsing arguments"`, which is what the C version's
    console-side spelling (`break "file":N`, reference.md §5.2) looks like --
    that form goes through gdb's own argument parser and works there, but not
    through MI. Measured on Windows with a drive-letter path; the unquoted
    `path:line` form also works but breaks on paths containing spaces.
    """
    return quote("%s:%d" % (path, line))


class _ValueParser:
    """Recursive-descent parser for the `value` production."""

    def __init__(self, text, pos=0):
        self.s = text
        self.i = pos

    def error(self, msg):
        raise MiParseError("%s at offset %d in %r" % (msg, self.i, self.s))

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def parse_c_string(self):
        if self.peek() != '"':
            self.error("expected '\"'")
        self.i += 1
        start = self.i
        buf = []
        while True:
            if self.i >= len(self.s):
                self.error("unterminated c-string")
            c = self.s[self.i]
            if c == "\\":
                buf.append(self.s[start:self.i])
                if self.i + 1 >= len(self.s):
                    self.error("unterminated escape")
                buf.append(self.s[self.i:self.i + 2])
                self.i += 2
                start = self.i
                continue
            if c == '"':
                buf.append(self.s[start:self.i])
                self.i += 1
                return unescape_c_string("".join(buf))
            self.i += 1

    def parse_variable(self):
        start = self.i
        while self.i < len(self.s):
            c = self.s[self.i]
            if c.isalnum() or c in "-_":
                self.i += 1
            else:
                break
        if self.i == start:
            self.error("expected variable name")
        return self.s[start:self.i]

    def parse_value(self):
        c = self.peek()
        if c == '"':
            return self.parse_c_string()
        if c == "{":
            return self.parse_tuple()
        if c == "[":
            return self.parse_list()
        self.error("unexpected character %r" % c)

    def parse_tuple(self):
        self.i += 1                     # consume '{'
        d = {}
        if self.peek() == "}":
            self.i += 1
            return d
        while True:
            k, v = self.parse_result()
            # duplicate keys: keep a list, matching how gdb can emit repeats
            if k in d:
                if not isinstance(d[k], list) or not d.get("__multi__" + k):
                    d[k] = [d[k]]
                d[k].append(v)
            else:
                d[k] = v
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() == "}":
                self.i += 1
                return d
            self.error("expected ',' or '}' in tuple")

    def parse_list(self):
        self.i += 1                     # consume '['
        if self.peek() == "]":
            self.i += 1
            return []
        # A list is either all values or all results; decide from the first item
        if self._looks_like_result():
            out = {}
            while True:
                k, v = self.parse_result()
                if k in out:
                    if not isinstance(out[k], list):
                        out[k] = [out[k]]
                    out[k].append(v)
                else:
                    out[k] = v
                if self.peek() == ",":
                    self.i += 1
                    continue
                if self.peek() == "]":
                    self.i += 1
                    return out
                self.error("expected ',' or ']' in result list")
        out = []
        while True:
            out.append(self.parse_value())
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() == "]":
                self.i += 1
                return out
            self.error("expected ',' or ']' in value list")

    def _looks_like_result(self):
        """Lookahead: is the next element `variable "="`?"""
        j = self.i
        n = len(self.s)
        if j >= n or not (self.s[j].isalpha() or self.s[j] == "_"):
            return False
        while j < n and (self.s[j].isalnum() or self.s[j] in "-_"):
            j += 1
        return j < n and self.s[j] == "="

    def parse_result(self):
        k = self.parse_variable()
        if self.peek() != "=":
            self.error("expected '=' after variable %r" % k)
        self.i += 1
        return k, self.parse_value()


def parse_value(text, pos=0):
    """Parse a standalone MI value. Returns (value, next_pos)."""
    p = _ValueParser(text, pos)
    v = p.parse_value()
    return v, p.i


def parse_results(text, pos):
    """Parse a trailing `,var=value,...` chain. Returns (dict, next_pos)."""
    p = _ValueParser(text, pos)
    out = {}
    while p.peek() == ",":
        p.i += 1
        k, v = p.parse_result()
        if k in out:
            if not isinstance(out[k], list):
                out[k] = [out[k]]
            out[k].append(v)
        else:
            out[k] = v
    return out, p.i


class Record:
    """One MI output record."""

    __slots__ = ("kind", "token", "cls", "payload", "stream_kind",
                 "async_kind", "raw")

    def __init__(self, kind, token=None, cls=None, payload=None,
                 stream_kind=None, async_kind=None, raw=""):
        self.kind = kind
        self.token = token          # int or None
        self.cls = cls              # result-class / async-class
        self.payload = payload      # dict for result/async, str for stream
        self.stream_kind = stream_kind
        self.async_kind = async_kind
        self.raw = raw

    def __repr__(self):
        if self.kind == STREAM:
            return "Record(STREAM %s %r)" % (self.stream_kind, self.payload)
        if self.kind == PROMPT:
            return "Record(PROMPT)"
        return "Record(%s tok=%r cls=%r payload=%r)" % (
            self.kind.upper(), self.token, self.cls, self.payload)


def parse_line(line):
    """Parse one MI output line into a Record.

    Never raises: unparseable lines become PARSE_ERROR records so a malformed
    gdb build degrades instead of killing the frontend (this matches
    gdbwire_parse_error_callback, which only logs).
    """
    if not line:
        return None
    if line == "(gdb)" or line == "(gdb) ":
        return Record(PROMPT, raw=line)

    i = 0
    n = len(line)
    token = None
    while i < n and line[i].isdigit():
        i += 1
    if i > 0:
        token = int(line[:i])
    if i >= n:
        return Record(PARSE_ERROR, raw=line, payload=line)

    marker = line[i]
    rest = line[i + 1:]
    try:
        if marker == "^":
            j = 0
            while j < len(rest) and (rest[j].isalnum() or rest[j] == "-"):
                j += 1
            cls = rest[:j]
            payload, _ = parse_results(rest, j)
            return Record(RESULT, token=token, cls=cls, payload=payload,
                          raw=line)
        if marker in ("~", "@", "&"):
            p = _ValueParser(rest, 0)
            s = p.parse_c_string()
            return Record(STREAM, token=token, payload=s, stream_kind=marker,
                          raw=line)
        if marker in ("*", "+", "="):
            j = 0
            while j < len(rest) and (rest[j].isalnum() or rest[j] == "-"):
                j += 1
            cls = rest[:j]
            payload, _ = parse_results(rest, j)
            return Record(ASYNC, token=token, cls=cls, payload=payload,
                          async_kind=marker, raw=line)
    except MiParseError:
        return Record(PARSE_ERROR, token=token, raw=line, payload=line)
    return Record(PARSE_ERROR, token=token, raw=line, payload=line)


class LineSplitter:
    """Turns an arbitrary byte stream into MI lines.

    gdb writes records as they happen, so a read() can end mid-line. Feeding
    chunks here yields only complete lines and keeps the remainder buffered.
    Handles a UTF-8 sequence split across chunks by decoding with errors
    deferred until the line completes.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        self._buf.extend(data)
        out = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx])
            del self._buf[:idx + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            line = raw.decode("utf-8", "replace")
            if line:
                out.append(line)
        return out

    @property
    def pending(self):
        return bytes(self._buf)
