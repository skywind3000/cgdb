"""`:set` option tests.

Names, abbreviations, types and defaults are cross-checked against the C
version's tables in cgdb/cgdbrc.cpp (cgdbrc_init_config_variables and
cgdbrc_init_config_options), so a cgdbrc file behaves the same here.
"""

import os

import pytest

from cgdbpy.cgdbrc import (BOOL, INT, STRING, Config, ConfigError,
                           LINE_DISPLAY_BLOCK, LINE_DISPLAY_LONG_ARROW,
                           LINE_DISPLAY_SHORT_ARROW, WIN_SPLIT_EVEN,
                           WIN_SPLIT_SRC_BIG, WIN_SPLIT_TOP_BIG,
                           WSO_HORIZONTAL)

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
    ("selectedlinedisplay", "sld", STRING, LINE_DISPLAY_BLOCK),
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


def test_lookup_is_case_insensitive():
    c = Config()
    assert c.resolve("TABSTOP").name == "tabstop"
    assert c.resolve("TS").name == "tabstop"


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


def test_set_with_no_value_defaults_to_one():
    c = Config()
    assert c.set("hlsearch") == 1
    assert c.set("tabstop") == 1


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
def test_execute_set_with_value():
    c = Config()
    c.execute("set tabstop=4")
    assert c.get("tabstop") == 4


def test_execute_set_with_spaces():
    c = Config()
    c.execute("set   tabstop = 6 ")
    assert c.get("tabstop") == 6


def test_execute_set_bare_bool():
    c = Config()
    c.execute("set hlsearch")
    assert c.get("hls") == 1


def test_execute_set_bare_string_returns_current():
    c = Config()
    assert c.execute("set winsplit") == WIN_SPLIT_EVEN


def test_execute_set_all_returns_every_option():
    c = Config()
    out = c.execute("set all")
    assert isinstance(out, list)
    assert len(out) == len(EXPECTED)
    assert "tabstop=8" in out


def test_execute_set_empty_returns_every_option():
    c = Config()
    assert len(c.execute("set")) == len(EXPECTED)


def test_execute_unknown_option():
    c = Config()
    with pytest.raises(ConfigError):
        c.execute("set nosuchoption=1")


def test_execute_non_set_command():
    c = Config()
    with pytest.raises(ConfigError):
        c.execute("map abc def")


def test_execute_blank_is_a_noop():
    c = Config()
    assert c.execute("") is None
    assert c.execute("   ") is None


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
def test_load_file(tmp_path):
    p = tmp_path / ".cgdbrc"
    p.write_text("# a comment\n\nset tabstop=4\nset hlsearch\n"
                 "set winsplit=src_big\n", encoding="utf-8")
    c = Config()
    errors = c.load_file(str(p))
    assert errors == []
    assert c.get("tabstop") == 4
    assert c.get("hls") == 1
    assert c.get("winsplit") == WIN_SPLIT_SRC_BIG


def test_load_file_collects_errors_without_aborting(tmp_path):
    p = tmp_path / ".cgdbrc"
    p.write_text("set tabstop=4\nset bogus=1\nset hlsearch\n", encoding="utf-8")
    c = Config()
    errors = c.load_file(str(p))
    assert len(errors) == 1
    assert "bogus" in errors[0]
    assert ":2:" in errors[0]           # line number is reported
    # the good lines still applied
    assert c.get("tabstop") == 4
    assert c.get("hls") == 1


def test_load_file_ignores_comments_and_blanks(tmp_path):
    p = tmp_path / ".cgdbrc"
    p.write_text("# only a comment\n\n   \n", encoding="utf-8")
    c = Config()
    assert c.load_file(str(p)) == []
