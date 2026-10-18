"""kui engine tests.

Two layers of validation:

1. A direct port of testsuite/kui.base/basic.exp -- the 198 dejagnu cases that
   are the C version's regression baseline (`make check`).
2. Cross-validation against a golden reference captured from the *real* C
   kui_driver (tests/data/kui_oracle.json), produced by driving
   lib/kui/kui_driver under a pty with the same map file and the same input
   sequence. This is an independent oracle, not a restatement of my own
   expectations.

One upstream test bug is deliberately NOT reproduced. basic.exp's
"parent map deleted, child map still matches" sends `unmap_example1_subset`
and expects `b`, but no map produces `b` for that input -- the C driver echoes
it literally. The dejagnu case passes only because the preceding case
("check that unmapping example in the documentation works") times out and
leaves a stale `b\r\n(kui) ` in expect's buffer, which the next regex matches.
Here we assert the real behaviour instead.
"""

import json
import os

import pytest

from cgdbpy import keycodes as kc
from cgdbpy import kui

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ORACLE = os.path.join(DATA, "kui_oracle.json")

# The mapping file, transcribed from basic.exp lines 5-88.
MAP_LINES = ["map gg up", "map abc def", "map abc ghi",
             "map partial_abc     mmm", "map partial_abcdef  nnn",
             "map first second", "map second third",
             "map special_space_cr p<Space>argc<CR>"]
for _v in "abcdefghijklmnopqrstuvwxyz":
    MAP_LINES.append("map special_shift_%s <S-%s>" % (_v.upper(), _v))
    MAP_LINES.append("map special_ctrl_%s <C-%s>" % (_v.upper(), _v))
MAP_LINES += [
    "map special_Esc <Esc>", "map special_Up <Up>", "map special_Down <Down>",
    "map special_Left <Left>", "map special_Right <Right>",
    "map special_Home <Home>", "map special_End <End>",
    "map special_PageUp <PageUp>", "map special_PageDown <PageDown>",
    "map special_Del <Del>", "map special_Insert <Insert>",
]
for _i in range(1, 13):
    MAP_LINES.append("map special_F%d <F%d>" % (_i, _i))
MAP_LINES += [
    "map special_Nul <Nul>", "map special_Bs <Bs>", "map special_Tab <Tab>",
    "map special_Nl <Nl>", "map special_Ff <Ff>", "map special_Cr <Cr>",
    "map special_Return <Return>", "map special_Enter <Enter>",
    "map special_Space <Space>", "map special_Lt <Lt>",
    "map special_Bslash <Bslash>", "map special_Bar <Bar>",
    "unmap does_not_exist",
    "map map_to_unmap bar", "unmap map_to_unmap",
    "map map_to_unmap2 bar", "unmap map_to_unmap2", "unmap map_to_unmap2",
    "map a<Space>b foo", "unmap a<Space>b",
    "map unmap_example1 a", "map unmap_example2_subset b",
    "unmap unmap_example1",
    "map unmap_example2 a", "map unmap_example2_subset b",
    "unmap unmap_example2_subset",
]


def load_map_line(manager, line):
    """Equivalent of kui_driver's load_map()."""
    line = line.strip()
    if not line:
        return
    parts = line.split(None, 2)
    if parts[0] == "map" and len(parts) == 3:
        manager.normal_set.add(parts[1], parts[2])
        manager.refresh()
    elif parts[0] == "unmap" and len(parts) >= 2:
        manager.normal_set.remove(parts[1])
        manager.refresh()


def make_manager():
    """A KeyManager fed from an in-memory byte queue."""
    queue = []

    def read():
        return queue.pop(0) if queue else None

    m = kui.KeyManager(read)
    for line in MAP_LINES:
        load_map_line(m, line)
    return m, queue


def drive(manager, queue, data):
    """Feed bytes and return the list of keys kui produces."""
    queue.extend(ord(c) for c in data)
    out = []
    while True:
        k = manager.getkey()
        if k is None:
            return out
        out.append(k)


def display(keys):
    """Reproduce kui_driver's stderr format for a key list."""
    return "".join(kc.keys_to_display([k]) for k in keys)


# ---------------------------------------------------------------------------
# 1. key notation parsing (kui_term_string_to_key_array)
# ---------------------------------------------------------------------------
def test_string_to_keys_plain():
    assert kc.string_to_keys("abc") == [97, 98, 99]


def test_string_to_keys_ctrl_notation():
    assert kc.string_to_keys("<C-a>") == [kc.CTRL_A]
    assert kc.string_to_keys("<C-z>") == [kc.CTRL_Z]


def test_string_to_keys_shift_notation():
    assert kc.string_to_keys("<S-a>") == [ord("A")]


def test_string_to_keys_named_specials():
    assert kc.string_to_keys("<Esc>") == [kc.ESC]
    assert kc.string_to_keys("<Up>") == [kc.UP]
    assert kc.string_to_keys("<Space>") == [32]
    assert kc.string_to_keys("<lt>") == [60]
    assert kc.string_to_keys("<Bslash>") == [92]
    assert kc.string_to_keys("<Bar>") == [124]


def test_string_to_keys_is_case_insensitive():
    # kui_term_get_cgdb_key_from_keycode uses strcasecmp
    assert kc.string_to_keys("<c-a>") == [kc.CTRL_A]
    assert kc.string_to_keys("<ESC>") == [kc.ESC]
    assert kc.string_to_keys("<Bs>") == [kc.CTRL_H]


def test_del_resolves_to_first_table_entry():
    # `<Del>` appears twice in cgdb_keycodes (CGDB_KEY_DC and byte 127);
    # the C lookup returns the first match.
    assert kc.string_to_keys("<Del>") == [kc.DC]


def test_cr_return_enter_all_map_to_ctrl_m():
    for text in ("<CR>", "<Return>", "<Enter>"):
        assert kc.string_to_keys(text) == [kc.CTRL_M], text


def test_unknown_macro_degrades_to_literal():
    assert kc.string_to_keys("<NoSuchKey>") == [ord(c) for c in "<NoSuchKey>"]


def test_nested_lt_starts_a_new_macro():
    # a '<' inside a pending macro flushes the pending text literally
    assert kc.string_to_keys("<a<C-b>") == [ord("<"), ord("a"), kc.CTRL_B]


def test_unterminated_macro_is_literal():
    assert kc.string_to_keys("ab<cd") == [ord(c) for c in "ab<cd"]


def test_mixed_text_and_macros():
    assert kc.string_to_keys("p<Space>argc<CR>") == \
        [ord("p"), 32, ord("a"), ord("r"), ord("g"), ord("c"), kc.CTRL_M]


def test_keys_to_bytes_uses_canonical_sequence():
    # MSDOS \033[0A also means UP, but translating back must give \033[A
    assert kc.keys_to_bytes([kc.UP]) == b"\033[A"
    assert kc.keys_to_bytes([kc.CTRL_A]) == b"\001"
    assert kc.keys_to_bytes([ord("x")]) == b"x"


def test_display_format_matches_driver():
    assert kc.keys_to_display([kc.UP]) == "CGDB_KEY_UP[27][91][65]"
    assert kc.keys_to_display([kc.CTRL_A]) == "CGDB_KEY_CTRL_A[1]"
    assert kc.keys_to_display([ord("a")]) == "a"
    assert kc.keys_to_display([kc.BACKWARD_WORD]) == \
        "CGDB_KEY_BACKWARD_WORD[27][98]"


# ---------------------------------------------------------------------------
# 2. terminal layer: escape sequences fold into single keys
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seq,key", [
    ("\033", kc.ESC),
    ("\033[A", kc.UP), ("\033[B", kc.DOWN),
    ("\033[C", kc.RIGHT), ("\033[D", kc.LEFT),
    ("\033[H", kc.HOME), ("\033[F", kc.END),
    # MSDOS variants (note the rotation: [0B is LEFT, [0D is DOWN)
    ("\033[0A", kc.UP), ("\033[0B", kc.LEFT),
    ("\033[0C", kc.RIGHT), ("\033[0D", kc.DOWN),
    # application-cursor variants
    ("\033OA", kc.UP), ("\033OB", kc.DOWN),
    ("\033OC", kc.RIGHT), ("\033OD", kc.LEFT),
    ("\033OH", kc.HOME), ("\033OF", kc.END),
    # readline word motions
    ("\033b", kc.BACKWARD_WORD), ("\033f", kc.FORWARD_WORD),
    ("\033\b", kc.BACKWARD_KILL_WORD), ("\033d", kc.FORWARD_KILL_WORD),
])
def test_terminal_sequences(seq, key):
    m, q = make_manager()
    assert drive(m, q, seq) == [key]


@pytest.mark.parametrize("n", range(1, 26))
def test_ctrl_bytes(n):
    m, q = make_manager()
    got = drive(m, q, chr(n))
    assert got == [kc.CTRL_A + n - 1]


def test_esc_followed_by_unknown_char_emits_both():
    # basic.exp: "key not detected until next key hit"
    m, q = make_manager()
    assert drive(m, q, "\033o") == [kc.ESC, ord("o")]


# ---------------------------------------------------------------------------
# 3. user map layer
# ---------------------------------------------------------------------------
def test_simple_map():
    m, q = make_manager()
    assert display(drive(m, q, "gg")) == "up"


def test_map_redefinition_last_wins():
    # basic.exp maps abc->def then abc->ghi
    m, q = make_manager()
    assert display(drive(m, q, "abc")) == "ghi"


def test_partial_match_falls_through_to_literal():
    m, q = make_manager()
    assert display(drive(m, q, "abf")) == "abf"


def test_subset_map():
    m, q = make_manager()
    assert display(drive(m, q, "partial_abc")) == "mmm"


def test_subset_map_plus_one_char():
    m, q = make_manager()
    assert display(drive(m, q, "partial_abcd")) == "mmmd"


def test_subset_map_plus_two_chars():
    m, q = make_manager()
    assert display(drive(m, q, "partial_abcde")) == "mmmde"


def test_superset_map():
    m, q = make_manager()
    assert display(drive(m, q, "partial_abcdef")) == "nnn"


def test_superset_map_plus_char():
    m, q = make_manager()
    assert display(drive(m, q, "partial_abcdefg")) == "nnng"


def test_recursive_maps():
    # map first second; map second third
    m, q = make_manager()
    assert display(drive(m, q, "first")) == "third"


def test_recursive_maps_with_extra_chars():
    m, q = make_manager()
    assert display(drive(m, q, "firsty")) == "thirdy"


def test_longest_match_wins_when_input_stops():
    # `map abc` and `map abcde` coexist: typing abcd then stopping yields abc
    ms = kui.MapSet()
    ms.add("abc", "X")
    ms.add("abcde", "Y")
    ctx = kui.KeyContext(ms, lambda: None)
    ctx.buffer.extend([ord(c) for c in "abcd"])
    got = []
    while True:
        k = ctx.getkey()
        if k is None:
            break
        got.append(k)
    assert "".join(chr(g) for g in got) == "Xd"


@pytest.mark.parametrize("letter", list("abcdefghijklmnopqrstuvwxyz"))
def test_shift_maps(letter):
    m, q = make_manager()
    assert display(drive(m, q, "special_shift_%s" % letter.upper())) == \
        letter.upper()


@pytest.mark.parametrize("letter",
                         [c for c in "abcdefghijklmnopqrstuvwxyz" if c != "z"])
def test_ctrl_maps(letter):
    # basic.exp skips <C-z>: kui_driver treats Ctrl-Z as "enter map editing
    # mode" and swallows everything after it.
    m, q = make_manager()
    got = drive(m, q, "special_ctrl_%s" % letter.upper())
    assert got == [kc.CTRL_A + ord(letter) - ord("a")]


@pytest.mark.parametrize("name,key", [
    ("Esc", kc.ESC), ("Up", kc.UP), ("Down", kc.DOWN), ("Left", kc.LEFT),
    ("Right", kc.RIGHT), ("Home", kc.HOME), ("End", kc.END),
    ("PageUp", kc.PPAGE), ("PageDown", kc.NPAGE), ("Del", kc.DC),
    ("Insert", kc.IC),
])
def test_named_special_maps(name, key):
    m, q = make_manager()
    assert drive(m, q, "special_%s" % name) == [key]


@pytest.mark.parametrize("n", range(1, 13))
def test_function_key_maps(n):
    m, q = make_manager()
    assert drive(m, q, "special_F%d" % n) == [getattr(kc, "F%d" % n)]


@pytest.mark.parametrize("name,expected", [
    ("Bs", kc.CTRL_H), ("Tab", kc.CTRL_I), ("Nl", kc.CTRL_J),
    ("Ff", kc.CTRL_L), ("Cr", kc.CTRL_M), ("Return", kc.CTRL_M),
    ("Enter", kc.CTRL_M), ("Space", 32), ("Lt", 60), ("Bslash", 92),
    ("Bar", 124),
])
def test_control_notation_maps(name, expected):
    m, q = make_manager()
    assert drive(m, q, "special_%s" % name) == [expected]


# ---------------------------------------------------------------------------
# 4. unmapping
# ---------------------------------------------------------------------------
def test_unmapped_text_is_literal():
    # basic.exp: "check that unmapping works" -- `map_to_unmap` was unmapped
    m, q = make_manager()
    assert display(drive(m, q, "map_to_unmap")) == "map_to_unmap"


def test_unmap_with_macro_in_lhs():
    # basic.exp: `map a<Space>b foo` then `unmap a<Space>b`
    m, q = make_manager()
    assert display(drive(m, q, "a b")) == "a b"


def test_parent_unmapped_child_still_matches():
    # The real behaviour: `unmap_example1_subset` has no map, so it echoes.
    # (basic.exp's expectation of "b" is an upstream test bug -- see the
    # module docstring.)
    m, q = make_manager()
    assert display(drive(m, q, "unmap_example1_subset")) == \
        "unmap_example1_subset"


def test_child_unmapped_parent_still_matches():
    m, q = make_manager()
    assert display(drive(m, q, "unmap_example2")) == "a"


def test_unmap_nonexistent_is_harmless():
    ms = kui.MapSet()
    ms.add("abc", "x")
    assert ms.remove("does_not_exist") is False
    assert len(ms) == 1


def test_unmap_twice_is_harmless():
    ms = kui.MapSet()
    ms.add("abc", "x")
    assert ms.remove("abc") is True
    assert ms.remove("abc") is False


def test_unmap_parent_preserves_child():
    ms = kui.MapSet()
    ms.add("abc", "X")
    ms.add("abcdef", "Y")
    assert ms.remove("abc") is True
    ctx = kui.KeyContext(ms, lambda: None)
    ctx.buffer.extend([ord(c) for c in "abcdef"])
    got = []
    while True:
        k = ctx.getkey()
        if k is None:
            break
        got.append(k)
    assert "".join(chr(g) for g in got) == "Y"


def test_unmap_child_preserves_parent():
    ms = kui.MapSet()
    ms.add("abc", "X")
    ms.add("abcdef", "Y")
    assert ms.remove("abcdef") is True
    ctx = kui.KeyContext(ms, lambda: None)
    ctx.buffer.extend([ord(c) for c in "abc"])
    got = []
    while True:
        k = ctx.getkey()
        if k is None:
            break
        got.append(k)
    assert "".join(chr(g) for g in got) == "X"


# ---------------------------------------------------------------------------
# 5. tree mechanics
# ---------------------------------------------------------------------------
def test_tree_prunes_empty_branches():
    t = kui.KeyTree()
    t.add([1, 2, 3], [9])
    assert 1 in t.root.children
    t.remove([1, 2, 3])
    assert t.root.children == {}


def test_tree_keeps_branch_with_other_child():
    t = kui.KeyTree()
    t.add([1, 2], [9])
    t.add([1, 3], [8])
    t.remove([1, 2])
    assert 1 in t.root.children
    assert set(t.root.children[1].children) == {3}


def test_pending_defers_instead_of_finalizing():
    ms = kui.MapSet()
    ms.add("abc", "X")
    ctx = kui.KeyContext(ms, lambda: None, defer_on_empty=True)
    ctx.buffer.extend([ord("a"), ord("b")])
    assert ctx.getkey() is kui.PENDING
    # flush resolves it
    assert ctx.flush() == ord("a")


# ---------------------------------------------------------------------------
# 6. cross-validation against the real C kui_driver
# ---------------------------------------------------------------------------
def _oracle():
    if not os.path.exists(ORACLE):
        pytest.skip("golden reference not present: %s" % ORACLE)
    with open(ORACLE, encoding="utf-8") as f:
        return json.load(f)


def test_oracle_matches_c_driver_over_full_sequence():
    """Feed every oracle case through one engine instance, in order, and
    compare the concatenated key display against the C driver's.

    Comparing the whole stream rather than case-by-case makes the check
    immune to the 1000 ms mapping timeout that smears output across case
    boundaries in the captured reference.
    """
    data = _oracle()
    m, q = make_manager()
    got = []
    for case in data["cases"]:
        got.append(display(drive(m, q, case["input"])))
    mine = "".join(got)

    theirs = "".join(c["output"] for c in data["cases"])
    theirs = theirs.replace("\r\n(kui) ", "").replace("\r\n(kui_map)", "")

    if mine != theirs:
        # report the first divergence with context
        i = 0
        while i < min(len(mine), len(theirs)) and mine[i] == theirs[i]:
            i += 1
        pytest.fail(
            "divergence from the C kui_driver at output char %d\n"
            "  python: ...%r\n"
            "  c:      ...%r\n"
            "  (python %d chars, c %d chars)"
            % (i, mine[max(0, i - 40):i + 40], theirs[max(0, i - 40):i + 40],
               len(mine), len(theirs)))


def test_oracle_case_count_matches_dejagnu_baseline():
    data = _oracle()
    # `make check` baseline is 198 passes (AGENTS.md)
    assert len(data["cases"]) == 198


def _clean_indices():
    """Oracle cases usable as isolated per-case assertions.

    10 of the 198 cases are smeared across case boundaries by kui's 1000 ms
    mapping timeout: a partial match holds a key back, so it lands in the next
    capture window. A case is only individually testable when *it* finished
    (output ends on a prompt, exactly one prompt) and the *previous* case also
    finished -- otherwise the capture starts with the previous case's tail.
    The smeared ones are still covered by the whole-stream comparison above.
    """
    data = _oracle()
    cases = data["cases"]

    def settled(c):
        return c["output"].endswith("\r\n(kui) ") and c["output"].count("(kui)") == 1

    out = []
    for i, c in enumerate(cases):
        if not settled(c):
            continue
        if i > 0 and not settled(cases[i - 1]):
            continue
        out.append(i)
    return out


try:
    CLEAN = _clean_indices()
except Exception:      # oracle file missing -> skip the parametrised test
    CLEAN = []


@pytest.mark.parametrize("index", CLEAN)
def test_oracle_individual_clean_cases(index):
    """Per-case cross-validation against the real C kui_driver."""
    data = _oracle()
    case = data["cases"][index]
    expected = case["output"].replace("\r\n(kui) ", "")
    m, q = make_manager()
    assert display(drive(m, q, case["input"])) == expected, case["name"]


def test_oracle_covers_most_cases_individually():
    # guard against the per-case check silently degrading to a handful of cases
    assert len(CLEAN) >= 180, "only %d clean oracle cases" % len(CLEAN)
