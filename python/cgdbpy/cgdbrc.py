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
_SPEC = [
    ("arrowstyle", "as", STRING, LINE_DISPLAY_SHORT_ARROW,
     (LINE_DISPLAY_SHORT_ARROW, LINE_DISPLAY_LONG_ARROW,
      LINE_DISPLAY_HIGHLIGHT, LINE_DISPLAY_BLOCK)),
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
    ("selectedlinedisplay", "sld", STRING, LINE_DISPLAY_BLOCK,
     (LINE_DISPLAY_SHORT_ARROW, LINE_DISPLAY_LONG_ARROW,
      LINE_DISPLAY_HIGHLIGHT, LINE_DISPLAY_BLOCK)),
    ("showmarks", "showmarks", BOOL, 1, None),
    ("syntax", "syn", STRING, SYNTAX_UNKNOWN, None),
    ("tabstop", "ts", INT, 8, None),
    ("timeout", "to", BOOL, 1, None),
    ("timeoutlen", "tm", INT, 1000, None),
    ("ttimeout", "ttimeout", BOOL, 1, None),
    ("ttimeoutlen", "ttm", INT, 100, None),
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
        """Find an option by full name or abbreviation."""
        t = token.lower()
        if t in self._opts:
            return self._opts[t]
        if t in self._by_abbrev:
            return self._by_abbrev[t]
        # vim allows unambiguous prefixes of the full name
        hits = [o for n, o in self._opts.items() if n.startswith(t)]
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
        """`:set name=value`. A missing value means 1 for bool/int options."""
        o = self.resolve(name)
        if o is None:
            raise ConfigError("unknown option: %s" % name)
        if value is None:
            value = 1 if o.kind in (BOOL, INT) else ""
        if o.kind == BOOL:
            new = self._to_bool(value, o)
        elif o.kind == INT:
            new = self._to_int(value, o)
        else:
            new = str(value)
            if o.choices and new.lower() not in o.choices:
                raise ConfigError(
                    "invalid value '%s' for %s (expected one of: %s)"
                    % (value, o.name, ", ".join(o.choices)))
            new = new.lower()
        old = o.value
        o.value = new
        if o.on_change and new != old:
            o.on_change(o.name, new, old)
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
    def load_file(self, path):
        """Parse a cgdbrc file. Unknown lines raise ConfigError."""
        errors = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    self.execute(line)
                except ConfigError as e:
                    errors.append("%s:%d: %s" % (path, lineno, e))
        return errors

    def execute(self, line):
        """Execute one `:` command line (without the leading colon)."""
        line = line.strip()
        if not line:
            return None
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if cmd == "set":
            return self._do_set(rest)
        raise ConfigError("not a config command: %s" % line)

    def _do_set(self, arg):
        arg = arg.strip()
        if not arg:
            return self.dump()
        if arg == "all":
            return self.dump()
        if "=" in arg:
            name, value = arg.split("=", 1)
            return self.set(name.strip(), value.strip())
        # `:set foo` with no value: bool/int -> 1, string -> show current
        o = self.resolve(arg)
        if o is None:
            raise ConfigError("unknown option: %s" % arg)
        if o.kind == STRING:
            return o.value
        return self.set(arg)
