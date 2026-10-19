"""`:set` option tests.

Names, abbreviations, types and defaults are cross-checked against the C
version's tables in cgdb/cgdbrc.cpp (cgdbrc_init_config_variables and
cgdbrc_init_config_options), so a cgdbrc file behaves the same here.
"""

import os

import pytest

from cgdbpy.cgdbrc import (
    read_rc_commands,
    BOOL, INT, STRING, Config, ConfigError,
    LINE_DISPLAY_BLOCK, LINE_DISPLAY_HIGHLIGHT, LINE_DISPLAY_LONG_ARROW,
    LINE_DISPLAY_SHORT_ARROW, WIN_SPLIT_EVEN,
    WIN_SPLIT_SRC_BIG, WIN_SPLIT_TOP_BIG,
    WSO_HORIZONTAL, WSO_VERTICAL)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CGDBRC_CPP = os.path.join(REPO, "cgdb", "cgdbrc.cpp")


# (name, abbreviation, kind, default) -- transcribed from cgdbrc.cpp
EXPECTED = [
    ("arrowstyle", "as", STRING, LINE_DISPLAY_SHORT_ARROW),
    ("autosourcereload", "asr", BOOL, 1),
    ("cgdbmodekey", "cgdbmodekey", STRING, "esc"),
    ("color", "color", BOOL, 1),
    ("debugwincolor", "dwc", BOOL, 1),
    ("disasm", "dis", BOOL, 0),
    ("executinglinedisplay", "eld", STRING, LINE_DISPLAY_LONG_ARROW),
    ("hlsearch", "hls", BOOL, 0),
    ("ignorecase", "ic", BOOL, 0),
    ("scrollbackbuffersize", "sbbs", INT, 10000),
    # C's default is block; this port prefers the whole-line highlight
    # (the block marker reads as a second cursor next to the hardware one)
    ("selectedlinedisplay", "sld", STRING, LINE_DISPLAY_HIGHLIGHT),
    ("showmarks", "showmarks", BOOL, 1),
    ("syntax", "syn", STRING, ""),
    ("tabstop", "ts", INT, 8),
    ("timeout", "to", BOOL, 1),
    ("timeoutlen", "tm", INT, 1000),
    ("ttimeout", "ttimeout", BOOL, 1),
    ("ttimeoutlen", "ttm", INT, 100),
    ("winminheight", "wmh", INT, 0),
    ("winminwidth", "wmw", INT, 0),
    ("winsplit", "winsplit", STRING, WIN_SPLIT_EVEN),
    ("winsplitorientation", "wso", STRING, WSO_HORIZONTAL),
    ("wrapscan", "ws", BOOL, 1),
]


@pytest.mark.parametrize("name,abbrev,kind,default", EXPECTED)
def test_option_exists_with_right_kind_and_default(name, abbrev, kind, default):
    c = Config()
    o = c.option(name)
    assert o.kind == kind
    assert o.value == default
    assert o.abbrev == abbrev


def test_option_set_matches_the_c_table():
    """Guard against drift: every option in the C table must exist here."""
    c = Config()
    assert set(c.names()) == {e[0] for e in EXPECTED}


@pytest.mark.skipif(not os.path.exists(CGDBRC_CPP),
                    reason="cgdbrc.cpp not available")
def test_every_c_option_name_is_present():
    """Independent cross-check against the C source rather than my own list."""
    import re
    with open(CGDBRC_CPP, encoding="utf-8", errors="replace") as f:
        src = f.read()
    # ConfigVariable("name", "abbrev", CONFIG_TYPE_*, ...)
    pairs = set(re.findall(
        r'ConfigVariable\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*'
        r'CONFIG_TYPE_(\w+)', src))
    assert pairs, "could not scrape any options from cgdbrc.cpp"
    c = Config()
    missing = []
    kind_map = {"BOOL": BOOL, "INT": INT, "FUNC_STRING": STRING,
                "FUNC_INT": INT, "FUNC_BOOL": BOOL, "STRING": STRING}
    for name, abbrev, ctype in sorted(pairs):
        o = c.resolve(name)
        if o is None:
            missing.append(name)
            continue
        assert o.abbrev == abbrev, "%s: abbrev %s != %s" % (name, o.abbrev,
                                                            abbrev)
        assert o.kind == kind_map[ctype], "%s: kind" % name
    assert not missing, "options in cgdbrc.cpp missing here: %r" % missing


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------
def test_lookup_by_abbreviation():
    c = Config()
    assert c.resolve("ts").name == "tabstop"
    assert c.resolve("sbbs").name == "scrollbackbuffersize"
    assert c.resolve("wmh").name == "winminheight"


def test_lookup_is_case_sensitive():
    # C's option lookup is strcmp/bsearch over the table -- case matters
    c = Config()
    assert c.resolve("TABSTOP") is None
    assert c.resolve("TS") is None
    assert c.resolve("tabstop").name == "tabstop"


def test_lookup_by_unambiguous_prefix():
    c = Config()
    assert c.resolve("tabst").name == "tabstop"


def test_lookup_unknown_returns_none():
    c = Config()
    assert c.resolve("nosuchoption") is None


def test_get_unknown_raises():
    c = Config()
    with pytest.raises(ConfigError):
        c.get("nosuchoption")


# ---------------------------------------------------------------------------
# setting values
# ---------------------------------------------------------------------------
def test_set_int():
    c = Config()
    assert c.set("tabstop", "4") == 4
    assert c.get("ts") == 4


def test_set_int_by_abbrev():
    c = Config()
    c.set("sbbs", "500")
    assert c.get("scrollbackbuffersize") == 500


def test_set_int_rejects_garbage():
    c = Config()
    with pytest.raises(ConfigError):
        c.set("tabstop", "wide")


def test_set_bool_on_off():
    c = Config()
    assert c.set("hlsearch", "on") == 1
    assert c.set("hls", "off") == 0
    for truthy in ("1", "true", "yes", "on"):
        assert c.set("ic", truthy) == 1, truthy
    for falsy in ("0", "false", "no", "off"):
        assert c.set("ic", falsy) == 0, falsy


def test_set_bool_rejects_garbage():
    c = Config()
    with pytest.raises(ConfigError):
        c.set("color", "maybe")


def test_set_string_choice_validated():
    c = Config()
    c.set("winsplit", "src_big")
    assert c.get("winsplit") == WIN_SPLIT_SRC_BIG
    # the deprecated spelling C still accepts (cgdbrc.cpp:488-501)
    c.set("winsplit", "top_big")
    assert c.get("winsplit") == WIN_SPLIT_TOP_BIG
    with pytest.raises(ConfigError):
        c.set("winsplit", "diagonal")


def test_set_string_choice_is_case_insensitive():
    c = Config()
    c.set("eld", "ShortArrow")
    assert c.get("eld") == LINE_DISPLAY_SHORT_ARROW


def test_set_string_without_choices_accepts_anything():
    c = Config()
    c.set("syntax", "rust")
    assert c.get("syntax") == "rust"


def test_set_with_no_value():
    # a bare boolean turns on; an int option *reports* its value instead of
    # being clobbered to 1 (C errors there -- `set tabstop` must never
    # break the rendering the way tabstop=1 would)
    c = Config()
    assert c.set("hlsearch") == 1
    assert c.set("tabstop") == 8


def test_set_no_prefix_clears_a_boolean():
    # command_parse_set strips a leading "no" (cgdbrc.cpp:910-915)
    c = Config()
    c.set("hlsearch")
    assert c.set("nohlsearch") == 0
    assert c.get("hls") == 0
    assert c.set("noic") == 0


def test_set_no_prefix_rejected_for_non_boolean():
    c = Config()
    with pytest.raises(ConfigError):
        c.set("notabstop")


def test_set_values_are_case_insensitive():
    # every C value parser uses strcasecmp
    c = Config()
    assert c.set("winsplit", "SRC_BIG") == WIN_SPLIT_SRC_BIG
    assert c.set("wso", "VERTICAL") == WSO_VERTICAL


def test_arrowstyle_writes_executinglinedisplay():
    # command_set_arrow_style is the deprecated alias of eld
    c = Config()
    c.set("arrowstyle", "highlight")
    assert c.get("eld") == "highlight"
    c.set("executinglinedisplay", "shortarrow")
    assert c.get("as") == "shortarrow"
    with pytest.raises(ConfigError):
        c.set("arrowstyle", "block")     # C rejects block for the alias


def test_set_calls_on_change_hook():
    c = Config()
    seen = []
    c.option("tabstop").on_change = lambda n, new, old: seen.append((n, new, old))
    c.set("tabstop", "4")
    assert seen == [("tabstop", 4, 8)]
    c.set("tabstop", "4")           # unchanged -> no second call
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# command lines
# ---------------------------------------------------------------------------
def test_apply_set_with_value():
    c = Config()
    c.apply_set("tabstop=4")
    assert c.get("tabstop") == 4


def test_apply_set_with_spaces():
    c = Config()
    c.apply_set("  tabstop = 6 ")
    assert c.get("tabstop") == 6


def test_apply_set_bare_bool():
    c = Config()
    c.apply_set("hlsearch")
    assert c.get("hls") == 1


def test_apply_set_bare_string_returns_current():
    c = Config()
    assert c.apply_set("winsplit") == WIN_SPLIT_EVEN


def test_apply_set_all_returns_every_option():
    c = Config()
    out = c.apply_set("all")
    assert isinstance(out, list)
    assert len(out) == len(EXPECTED)
    assert "tabstop=8" in out


def test_apply_set_empty_returns_every_option():
    c = Config()
    assert len(c.apply_set("")) == len(EXPECTED)


def test_apply_set_unknown_option():
    c = Config()
    with pytest.raises(ConfigError):
        c.apply_set("nosuchoption=1")


def test_describe_bool_shows_on_off():
    c = Config()
    assert c.describe("hlsearch") == "hlsearch=off"
    c.set("hls", 1)
    assert c.describe("hlsearch") == "hlsearch=on"


def test_describe_int_and_string():
    c = Config()
    assert c.describe("tabstop") == "tabstop=8"
    assert c.describe("winsplit") == "winsplit=even"


# ---------------------------------------------------------------------------
# rc files
# ---------------------------------------------------------------------------
def test_read_rc_commands(tmp_path):
    # the rc file is a full command language (command_parse_file): set
    # lines, map lines, anything a `:` line accepts
    p = tmp_path / ".cgdbrc"
    p.write_text("# a comment\n\nset tabstop=4\nmap a b\n"
                 "set winsplit=src_big\n", encoding="utf-8")
    cmds = read_rc_commands(str(p))
    assert [c for _, c in cmds] == ["set tabstop=4", "map a b",
                                    "set winsplit=src_big"]


def test_read_rc_commands_strips_leading_colon_and_inline_comment(tmp_path):
    p = tmp_path / ".cgdbrc"
    p.write_text(":set hlsearch   # vim style\n", encoding="utf-8")
    assert read_rc_commands(str(p)) == [(1, "set hlsearch")]


def test_read_rc_commands_joins_continuations(tmp_path):
    p = tmp_path / ".cgdbrc"
    p.write_text("set hlsearch \\\n   on # long line\n", encoding="utf-8")
    # a trailing backslash joins lines; the joined line keeps the first
    # line's number
    assert read_rc_commands(str(p)) == [(1, "set hlsearch     on")]


def test_read_rc_commands_ignores_comments_and_blanks(tmp_path):
    p = tmp_path / ".cgdbrc"
    p.write_text("# only a comment\n\n   \n", encoding="utf-8")
    assert read_rc_commands(str(p)) == []
