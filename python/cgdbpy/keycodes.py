"""Key constants and text-notation tables.

Ported from lib/kui/kui_cgdb_key.h and lib/kui/kui_term.cpp. The numeric
layout matters: byte-valued keys are 0..255 and special keys start at 10000,
so `key >= KEY_SPECIAL_BASE` is the equivalent of kui_term_is_cgdb_key().
"""

KEY_SPECIAL_BASE = 10000

ESC = 10000
UP = 10001
DOWN = 10002
LEFT = 10003
RIGHT = 10004
HOME = 10005
END = 10006
PPAGE = 10007
NPAGE = 10008
DC = 10009
IC = 10010

F1 = 10011
F2 = 10012
F3 = 10013
F4 = 10014
F5 = 10015
F6 = 10016
F7 = 10017
F8 = 10018
F9 = 10019
F10 = 10020
F11 = 10021
F12 = 10022

CTRL_A = 10023
CTRL_B = 10024
CTRL_C = 10025
CTRL_D = 10026
CTRL_E = 10027
CTRL_F = 10028
CTRL_G = 10029
CTRL_H = 10030
CTRL_I = 10031
CTRL_J = 10032
CTRL_K = 10033
CTRL_L = 10034
CTRL_M = 10035
CTRL_N = 10036
CTRL_O = 10037
CTRL_P = 10038
CTRL_Q = 10039
CTRL_R = 10040
CTRL_S = 10041
CTRL_T = 10042
CTRL_U = 10043
CTRL_V = 10044
CTRL_W = 10045
CTRL_X = 10046
CTRL_Y = 10047
CTRL_Z = 10048

BACKWARD_WORD = 10049
FORWARD_WORD = 10050
BACKWARD_KILL_WORD = 10051
FORWARD_KILL_WORD = 10052

ERROR = 10053

KEY_NAMES = {
    ESC: "CGDB_KEY_ESC",
    UP: "CGDB_KEY_UP",
    DOWN: "CGDB_KEY_DOWN",
    LEFT: "CGDB_KEY_LEFT",
    RIGHT: "CGDB_KEY_RIGHT",
    HOME: "CGDB_KEY_HOME",
    END: "CGDB_KEY_END",
    PPAGE: "CGDB_KEY_PPAGE",
    NPAGE: "CGDB_KEY_NPAGE",
    DC: "CGDB_KEY_DC",
    IC: "CGDB_KEY_IC",
    BACKWARD_WORD: "CGDB_KEY_BACKWARD_WORD",
    FORWARD_WORD: "CGDB_KEY_FORWARD_WORD",
    BACKWARD_KILL_WORD: "CGDB_KEY_BACKWARD_KILL_WORD",
    FORWARD_KILL_WORD: "CGDB_KEY_FORWARD_KILL_WORD",
    ERROR: "CGDB_KEY_ERROR",
}
for _i in range(1, 13):
    KEY_NAMES[globals()["F%d" % _i]] = "CGDB_KEY_F%d" % _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KEY_NAMES[globals()["CTRL_" + _c]] = "CGDB_KEY_CTRL_" + _c


def is_special(key):
    """True for CGDB_KEY_* values, False for raw byte values."""
    return key >= KEY_SPECIAL_BASE


def key_name(key):
    if key in KEY_NAMES:
        return KEY_NAMES[key]
    if 0 <= key < 256:
        return repr(chr(key))
    return "UNKNOWN(%d)" % key


# --- kui_term.cpp hard_coded_bindings (47 entries) -------------------------
#
# Order is significant: a key may have several terminal sequences, and the
# FIRST one is the canonical sequence used when translating a key back to
# bytes. readline's own sequences are deliberately first so that bytes sent
# to gdb are ones readline recognises.
HARD_CODED_BINDINGS = [
    (ESC, "\033"),
    (UP, "\033[A"),
    (DOWN, "\033[B"),
    (RIGHT, "\033[C"),
    (LEFT, "\033[D"),
    (HOME, "\033[H"),
    (END, "\033[F"),
    # MSDOS arrows. Note the rotation: [0A is UP but [0B is LEFT.
    (UP, "\033[0A"),
    (LEFT, "\033[0B"),
    (RIGHT, "\033[0C"),
    (DOWN, "\033[0D"),
    (UP, "\033OA"),
    (DOWN, "\033OB"),
    (RIGHT, "\033OC"),
    (LEFT, "\033OD"),
    (HOME, "\033OH"),
    (END, "\033OF"),
    # passed through to readline
    (BACKWARD_WORD, "\033b"),
    (FORWARD_WORD, "\033f"),
    (BACKWARD_KILL_WORD, "\033\b"),
    (FORWARD_KILL_WORD, "\033d"),
]
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    HARD_CODED_BINDINGS.append(
        (globals()["CTRL_" + _c], chr(ord(_c.upper()) - ord("A") + 1)))

# Sequences that depend on terminfo in the C version. There is no terminfo on
# Windows, so these are the common xterm/VT values; win_specs.md §2 records the
# same decision for the C port ("kui 有 47 条硬编码 VT 绑定兜底").
TERM_SEQUENCE_DEFAULTS = [
    (PPAGE, "\033[5~"),
    (NPAGE, "\033[6~"),
    (DC, "\033[3~"),
    (IC, "\033[2~"),
    (F1, "\033OP"),
    (F2, "\033OQ"),
    (F3, "\033OR"),
    (F4, "\033OS"),
    (F5, "\033[15~"),
    (F6, "\033[17~"),
    (F7, "\033[18~"),
    (F8, "\033[19~"),
    (F9, "\033[20~"),
    (F10, "\033[21~"),
    (F11, "\033[23~"),
    (F12, "\033[24~"),
]

# sequence bytes -> key. Later duplicates must not overwrite the first, but
# every sequence must be recognised on input.
SEQUENCE_TO_KEY = {}
for _key, _seq in HARD_CODED_BINDINGS + TERM_SEQUENCE_DEFAULTS:
    SEQUENCE_TO_KEY.setdefault(_seq, _key)

# key -> canonical (first) sequence
KEY_TO_SEQUENCE = {}
for _key, _seq in HARD_CODED_BINDINGS + TERM_SEQUENCE_DEFAULTS:
    KEY_TO_SEQUENCE.setdefault(_key, _seq)


# --- kui_term.cpp cgdb_keycodes (92 entries) -------------------------------
#
# Text notation accepted inside `<...>` in a :map command. Lookup is
# case-insensitive and the FIRST match wins, which is why `<Del>` resolves to
# CGDB_KEY_DC and not to byte 127.
KEYCODES = []
for _c in "abcdefghijklmnopqrstuvwxyz":
    KEYCODES.append((ord(_c.upper()), "<S-%s>" % _c))
KEYCODES += [
    (ESC, "<Esc>"),
    (UP, "<Up>"),
    (DOWN, "<Down>"),
    (LEFT, "<Left>"),
    (RIGHT, "<Right>"),
    (HOME, "<Home>"),
    (END, "<End>"),
    (PPAGE, "<PageUp>"),
    (NPAGE, "<PageDown>"),
    (DC, "<Del>"),
    (IC, "<Insert>"),
]
for _i in range(1, 13):
    KEYCODES.append((globals()["F%d" % _i], "<F%d>" % _i))
KEYCODES += [
    (BACKWARD_WORD, "<BACKWARD-WORD>"),
    (FORWARD_WORD, "<FORWARD-WORD>"),
    (BACKWARD_KILL_WORD, "<BACKWARD-KILL_WORD>"),
    (FORWARD_KILL_WORD, "<FORWARD-KILL_WORD>"),
]
for _c in "abcdefghijklmnopqrstuvwxyz":
    KEYCODES.append((globals()["CTRL_" + _c.upper()], "<C-%s>" % _c))
KEYCODES += [
    (0, "<Nul>"),
    (CTRL_H, "<BS>"),
    (CTRL_I, "<Tab>"),
    (CTRL_J, "<NL>"),
    (CTRL_L, "<FF>"),
    (CTRL_M, "<CR>"),
    (CTRL_M, "<Return>"),
    (CTRL_M, "<Enter>"),
    (32, "<Space>"),
    (60, "<lt>"),
    (92, "<Bslash>"),
    (124, "<Bar>"),
    (127, "<Del>"),
]

# case-insensitive text -> key, first match wins
KEYCODE_TO_KEY = {}
for _key, _text in KEYCODES:
    KEYCODE_TO_KEY.setdefault(_text.lower(), _key)

# key -> canonical text (first entry for that key)
KEY_TO_KEYCODE = {}
for _key, _text in KEYCODES:
    KEY_TO_KEYCODE.setdefault(_key, _text)


def keycode_to_key(text):
    """`<C-a>` -> CTRL_A. Returns ERROR when the notation is unknown.

    Mirrors kui_term_get_cgdb_key_from_keycode(): case-insensitive, first
    match wins, unknown notation yields CGDB_KEY_ERROR so the caller can fall
    back to emitting the literal characters.
    """
    return KEYCODE_TO_KEY.get(text.lower(), ERROR)


def string_to_keys(text):
    """Parse a :map argument into a list of key ints.

    Port of kui_term_string_to_key_array(). Rules that the dejagnu suite
    depends on:
      * `<X>` is looked up case-insensitively; an unknown `<X>` degrades to
        its literal characters rather than failing
      * a `<` inside a pending macro flushes the pending text literally and
        starts a new macro
      * a `<` never closed by `>` is emitted literally
    """
    out = []
    macro = []
    in_macro = False
    for ch in text:
        if not in_macro:
            if ch == "<":
                in_macro = True
                macro = [ch]
            else:
                out.append(ord(ch))
            continue
        if ch == "<":
            out.extend(ord(c) for c in macro)
            macro = [ch]
        else:
            macro.append(ch)
        if ch == ">":
            in_macro = False
            token = "".join(macro)
            key = keycode_to_key(token)
            if key == ERROR:
                out.extend(ord(c) for c in macro)
            else:
                out.append(key)
            macro = []
    if in_macro:
        out.extend(ord(c) for c in macro)
    return out


def keys_to_string(keys):
    """Inverse of string_to_keys, using each key's canonical notation."""
    parts = []
    for k in keys:
        if k in KEY_TO_KEYCODE:
            parts.append(KEY_TO_KEYCODE[k])
        elif 0 <= k < 256:
            parts.append(chr(k))
        else:
            parts.append("<%d>" % k)
    return "".join(parts)


def keys_to_bytes(keys):
    """Translate keys back to the byte sequence a terminal/readline expects.

    Port of kui_term_get_ascii_char_sequence_from_key(): special keys become
    their canonical escape sequence, byte keys stay as-is.
    """
    out = bytearray()
    for k in keys:
        seq = KEY_TO_SEQUENCE.get(k)
        if seq is not None and is_special(k):
            out.extend(seq.encode("ascii", "replace"))
        elif 0 <= k < 256:
            out.append(k)
        elif seq is not None:
            out.extend(seq.encode("ascii", "replace"))
    return bytes(out)


def keys_to_display(keys):
    """`CGDB_KEY_UP[27][91][65]` -- the format kui_driver prints."""
    if not keys:
        return ""
    k = keys[0] if len(keys) == 1 else None
    if k is not None and is_special(k):
        name = KEY_NAMES.get(k, "CGDB_KEY_%d" % k)
        seq = KEY_TO_SEQUENCE.get(k, "")
        return name + "".join("[%d]" % b for b in seq.encode("ascii", "replace"))
    if k is not None and 0 <= k < 256:
        return chr(k)
    return " ".join(key_name(x) for x in keys)
