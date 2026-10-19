"""`:set` configuration options.

Python counterpart of cgdb/cgdbrc.cpp. Option names, abbreviations, types and
default values are transcribed from cgdbrc_init_config_variables() and
cgdbrc_init_config_options() so that a cgdbrc file behaves the same.
"""

BOOL = "bool"
INT = "int"
STRING = "string"

# line display styles (cgdbrc.h enum line_display_style)
LINE_DISPLAY_SHORT_ARROW = "shortarrow"
LINE_DISPLAY_LONG_ARROW = "longarrow"
LINE_DISPLAY_HIGHLIGHT = "highlight"
LINE_DISPLAY_BLOCK = "block"

# win_split_val -- the values command_set_winsplit() (cgdbrc.cpp:483-511)
# accepts. The top_*/bottom_* spellings are deprecated aliases that C still
# accepts, so they are kept here too.
WIN_SPLIT_SRC_BIG = "src_big"
WIN_SPLIT_SRC_FULL = "src_full"
WIN_SPLIT_GDB_BIG = "gdb_big"
WIN_SPLIT_GDB_FULL = "gdb_full"
WIN_SPLIT_EVEN = "even"
WIN_SPLIT_TOP_BIG = "top_big"
WIN_SPLIT_TOP_FULL = "top_full"
WIN_SPLIT_BOTTOM_BIG = "bottom_big"
WIN_SPLIT_BOTTOM_FULL = "bottom_full"

# win_split_orientation_val
WSO_HORIZONTAL = "horizontal"
WSO_VERTICAL = "vertical"

# language_support_val -- the C version enumerates the 8 flex lexers; here the
# value is a Pygments lexer name or alias, so any language Pygments knows works.
SYNTAX_UNKNOWN = ""


class Option:
    __slots__ = ("name", "abbrev", "kind", "value", "choices", "on_change")

    def __init__(self, name, abbrev, kind, value, choices=None):
        self.name = name
        self.abbrev = abbrev
        self.kind = kind
        self.value = value
        self.choices = choices
        self.on_change = None


# (name, abbreviation, kind, default, allowed values or None)
# arrowstyle is the deprecated alias of executinglinedisplay: C's
# command_set_arrow_style writes CGDBRC_EXECUTING_LINE_DISPLAY and rejects
# "block" (cgdbrc.cpp:419-435), which the choices here mirror.
_SPEC = [
    ("arrowstyle", "as", STRING, LINE_DISPLAY_SHORT_ARROW,
     (LINE_DISPLAY_SHORT_ARROW, LINE_DISPLAY_LONG_ARROW,
      LINE_DISPLAY_HIGHLIGHT)),
    ("autosourcereload", "asr", BOOL, 1, None),
    ("cgdbmodekey", "cgdbmodekey", STRING, "esc", None),
    ("color", "color", BOOL, 1, None),
    ("debugwincolor", "dwc", BOOL, 1, None),
    ("disasm", "dis", BOOL, 0, None),
    ("executinglinedisplay", "eld", STRING, LINE_DISPLAY_LONG_ARROW,
     (LINE_DISPLAY_SHORT_ARROW, LINE_DISPLAY_LONG_ARROW,
      LINE_DISPLAY_HIGHLIGHT, LINE_DISPLAY_BLOCK)),
    ("hlsearch", "hls", BOOL, 0, None),
    ("ignorecase", "ic", BOOL, 0, None),
    ("scrollbackbuffersize", "sbbs", INT, 10000, None),
    ("selectedlinedisplay", "sld", STRING, LINE_DISPLAY_HIGHLIGHT,
     # C's default is LINE_DISPLAY_BLOCK; this port defaults to the
     # whole-line highlight instead -- the block marker reads as a second
     # cursor next to the terminal one (user feedback), and the C look
     # stays one `:set sld=block` away.
     (LINE_DISPLAY_SHORT_ARROW, LINE_DISPLAY_LONG_ARROW,
      LINE_DISPLAY_HIGHLIGHT, LINE_DISPLAY_BLOCK)),
    ("showmarks", "showmarks", BOOL, 1, None),
    ("syntax", "syn", STRING, SYNTAX_UNKNOWN, None),
    ("tabstop", "ts", INT, 8, None),
    ("timeout", "to", BOOL, 1, None),
    ("timeoutlen", "tm", INT, 1000, None),
    ("ttimeout", "ttimeout", BOOL, 1, None),
    # C's default is 100; this port halves it -- 100ms reads as a laggy
    # ESC on the focus switch (user feedback), and 50ms is still well
    # above a real terminal's escape-sequence arrival gap
    ("ttimeoutlen", "ttm", INT, 50, None),
    ("winminheight", "wmh", INT, 0, None),
    ("winminwidth", "wmw", INT, 0, None),
    ("winsplit", "winsplit", STRING, WIN_SPLIT_EVEN,
     (WIN_SPLIT_EVEN, WIN_SPLIT_SRC_BIG, WIN_SPLIT_SRC_FULL,
      WIN_SPLIT_GDB_BIG, WIN_SPLIT_GDB_FULL, WIN_SPLIT_TOP_BIG,
      WIN_SPLIT_TOP_FULL, WIN_SPLIT_BOTTOM_BIG, WIN_SPLIT_BOTTOM_FULL)),
    ("winsplitorientation", "wso", STRING, WSO_HORIZONTAL,
     (WSO_HORIZONTAL, WSO_VERTICAL)),
    ("wrapscan", "ws", BOOL, 1, None),
]


class ConfigError(Exception):
    pass


class Config:
    def __init__(self):
        self._opts = {}
        self._by_abbrev = {}
        for name, abbrev, kind, default, choices in _SPEC:
            o = Option(name, abbrev, kind, default, choices)
            self._opts[name] = o
            self._by_abbrev[abbrev] = o

    # --- lookup -----------------------------------------------------------
    def resolve(self, token):
        """Find an option by full name or abbreviation.

        Case-sensitive, like C's strcmp/bsearch over the option table; only
        the unambiguous-prefix convenience (vim's `set ignore`) is an
        addition on top.
        """
        if token in self._opts:
            return self._opts[token]
        if token in self._by_abbrev:
            return self._by_abbrev[token]
        # vim allows unambiguous prefixes of the full name
        hits = [o for n, o in self._opts.items() if n.startswith(token)]
        if len(hits) == 1:
            return hits[0]
        return None

    def names(self):
        return sorted(self._opts)

    def get(self, name):
        o = self.resolve(name)
        if o is None:
            raise ConfigError("unknown option: %s" % name)
        return o.value

    def option(self, name):
        o = self.resolve(name)
        if o is None:
            raise ConfigError("unknown option: %s" % name)
        return o

    # --- mutation ---------------------------------------------------------
    def set(self, name, value=None):
        """``:set name`` / ``:set name=value`` / ``:set noname``.

        The `no` prefix is C's way of clearing a boolean (cgdbrc.cpp:910-915)
        and only a boolean -- `set notabstop` is an error there. Values are
        compared case-insensitively, because every C value parser uses
        strcasecmp; option *names* are case-sensitive, because the C table
        lookups use strcmp.
        """
        name = name.strip()
        no_prefix = False
        if name.startswith("no") and len(name) > 2:
            stripped = name[2:]
            if self.resolve(stripped) is not None:
                o_probe = self.resolve(stripped)
                if o_probe.kind != BOOL:
                    raise ConfigError(
                        "cannot use 'no' prefix with non-boolean %s"
                        % o_probe.name)
                name = stripped
                no_prefix = True
        o = self.resolve(name)
        if o is None:
            raise ConfigError("unknown option: %s" % name)
        if no_prefix:
            if o.kind != BOOL:
                raise ConfigError(
                    "cannot use 'no' prefix with non-boolean %s" % o.name)
            value = 0
        if value is None:
            # no value given: a boolean turns on; int/string options report
            # their current value instead of being clobbered (C errors on
            # `set tabstop` -- showing the value is friendlier and cannot
            # break the rendering the way tabstop=1 would)
            if o.kind == BOOL:
                value = 1
            else:
                return o.value
        if o.kind == BOOL:
            new = self._to_bool(value, o)
        elif o.kind == INT:
            new = self._to_int(value, o)
        else:
            new = str(value).strip().lower()
            if o.choices and new not in o.choices:
                raise ConfigError(
                    "invalid value '%s' for %s (expected one of: %s)"
                    % (value, o.name, ", ".join(o.choices)))
        old = o.value
        o.value = new
        if o.on_change and new != old:
            o.on_change(o.name, new, old)
        # arrowstyle is the deprecated alias: writing it also writes
        # executinglinedisplay (command_set_arrow_style)
        if o.name == "arrowstyle" and new != old:
            self._opts["executinglinedisplay"].value = new
        if o.name == "executinglinedisplay":
            self._opts["arrowstyle"].value = new
        return new

    def _to_bool(self, value, o):
        if isinstance(value, bool):
            return 1 if value else 0
        s = str(value).strip().lower()
        if s in ("1", "on", "true", "yes", ""):
            return 1
        if s in ("0", "off", "false", "no"):
            return 0
        try:
            return 1 if int(s) != 0 else 0
        except ValueError:
            raise ConfigError("invalid boolean value '%s' for %s"
                              % (value, o.name))

    def _to_int(self, value, o):
        try:
            return int(str(value).strip())
        except ValueError:
            raise ConfigError("invalid integer value '%s' for %s"
                              % (value, o.name))

    def describe(self, name):
        o = self.option(name)
        if o.kind == BOOL:
            return "%s=%s" % (o.name, "on" if o.value else "off")
        return "%s=%s" % (o.name, o.value)

    def dump(self):
        return [self.describe(n) for n in self.names()]

    # --- rc file ----------------------------------------------------------
    def apply_set(self, arg):
        """Parse and apply one `set` argument (command_parse_set).

        Returns the new/current value, or the option dump for a bare
        `set` / `set all`; raises ConfigError on anything C would reject.
        """
        arg = arg.strip()
        if not arg or arg == "all":
            return self.dump()
        if "=" in arg:
            name, value = arg.split("=", 1)
            return self.set(name.strip(), value.strip())
        return self.set(arg)


def read_rc_commands(path):
    """Read a cgdbrc file into (lineno, command) pairs.

    command_parse_file (cgdbrc.cpp:1101-1134) feeds every line to the full
    command parser, so the rc file is a command language, not a `set`-only
    config: `map`, `imap`, `syntax`, `focus`, line numbers, everything a
    `:` line accepts. The lexer rules carried over here: `#` starts a
    comment anywhere on the line, a trailing backslash joins lines, and a
    leading `:` on a command is optional. Errors are the caller's business
    -- C reports "Error parsing line N" and keeps going.
    """
    commands = []
    pending = ""
    start_line = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
    except OSError:
        return commands
    for lineno, raw in enumerate(raw_lines, 1):
        line = raw.rstrip("\r\n")
        if pending:
            line = pending + line
        if line.rstrip().endswith("\\"):
            pending = line.rstrip()[:-1] + " "
            if not start_line:
                start_line = lineno
            continue
        pending = ""
        start_line = start_line or lineno
        line = line.split("#", 1)[0].strip()
        if not line:
            start_line = 0
            continue
        if line.startswith(":"):
            line = line[1:].strip()
        commands.append((start_line, line))
        start_line = 0
    if pending:
        commands.append((start_line or 1, pending.strip()))
    return commands
