"""GDB/MI parser tests.

The grammar cases come from the GDB manual's "GDB/MI output syntax" section.
Payload shapes are cross-checked against what a real gdb 17.1 emits (see
test_gdb_integration.py), so these are not just self-consistent inventions.
"""

import pytest

from cgdbpy import mi


# ---------------------------------------------------------------------------
# c-string escaping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (r'hello', "hello"),
    (r'with \"quotes\"', 'with "quotes"'),
    (r'back\\slash', "back\\slash"),
    (r'new\nline', "new\nline"),
    (r'tab\there', "tab\there"),
    (r'cr\rhere', "cr\rhere"),
    (r'bell\ahere', "bell\ahere"),
    (r'\033[1;1H', "\033[1;1H"),
    (r'trailing\\', "trailing\\"),
])
def test_unescape(raw, expected):
    assert mi.unescape_c_string(raw) == expected


@pytest.mark.parametrize("text", [
    "plain", 'with "quotes"', "back\\slash", "new\nline", "tab\there",
    "\033[1;1H", "", "unicode: 中文", 'mixed \\ and " and \n',
])
def test_escape_roundtrip(text):
    assert mi.unescape_c_string(mi.escape_c_string(text)) == text


def test_quote_wraps_in_double_quotes():
    assert mi.quote("a b") == '"a b"'
    assert mi.quote('say "hi"') == r'"say \"hi\""'


def test_quote_handles_windows_pipe_path():
    # win_specs.md §4 E2b: gdb's buildargv eats backslashes, so the C port
    # hands gdb forward slashes. Quoting must not mangle either form.
    assert mi.quote("//./pipe/cgdb_inf_1") == '"//./pipe/cgdb_inf_1"'


# ---------------------------------------------------------------------------
# record classification
# ---------------------------------------------------------------------------
def test_prompt():
    r = mi.parse_line("(gdb)")
    assert r.kind == mi.PROMPT
    assert mi.parse_line("(gdb) ").kind == mi.PROMPT


@pytest.mark.parametrize("line,cls", [
    ("^done", "done"),
    ("^running", "running"),
    ("^connected", "connected"),
    ("^error,msg=\"bad\"", "error"),
    ("^exit", "exit"),
])
def test_result_classes(line, cls):
    r = mi.parse_line(line)
    assert r.kind == mi.RESULT
    assert r.cls == cls
    assert r.token is None


def test_result_with_token():
    r = mi.parse_line("42^done,a=\"1\"")
    assert r.kind == mi.RESULT
    assert r.token == 42
    assert r.cls == "done"
    assert r.payload == {"a": "1"}


@pytest.mark.parametrize("marker,kind", [
    ("~", mi.STREAM_CONSOLE),
    ("@", mi.STREAM_TARGET),
    ("&", mi.STREAM_LOG),
])
def test_stream_kinds(marker, kind):
    r = mi.parse_line('%s"text\\n"' % marker)
    assert r.kind == mi.STREAM
    assert r.stream_kind == kind
    assert r.payload == "text\n"


@pytest.mark.parametrize("marker,kind", [
    ("*", mi.ASYNC_EXEC),
    ("+", mi.ASYNC_STATUS),
    ("=", mi.ASYNC_NOTIFY),
])
def test_async_kinds(marker, kind):
    r = mi.parse_line('%sstopped,reason="x"' % marker)
    assert r.kind == mi.ASYNC
    assert r.async_kind == kind
    assert r.cls == "stopped"
    assert r.payload == {"reason": "x"}


def test_async_with_token():
    r = mi.parse_line("7*stopped,reason=\"end-stepping-range\"")
    assert r.token == 7
    assert r.cls == "stopped"


# ---------------------------------------------------------------------------
# value grammar
# ---------------------------------------------------------------------------
def test_empty_tuple_and_list():
    r = mi.parse_line('^done,t={},l=[]')
    assert r.payload == {"t": {}, "l": []}


def test_nested_tuple():
    r = mi.parse_line('^done,frame={level="0",func="main",line="12"}')
    assert r.payload["frame"] == {"level": "0", "func": "main", "line": "12"}


def test_list_of_values():
    r = mi.parse_line('^done,xs=["1","2","3"]')
    assert r.payload["xs"] == ["1", "2", "3"]


def test_list_of_tuples_breakpoint_table():
    # the real -break-info shape
    line = ('^done,BreakpointTable={nr_rows="2",nr_cols="6",'
            'hdr=[{width="7",alignment="-1",col_name="number",'
            'colhdr="Num"},{width="7",alignment="-1",col_name="type",'
            'colhdr="Type"}],'
            'body=[bkpt={number="1",type="breakpoint",disp="keep",'
            'enabled="y",addr="0x00401580",func="main",file="target.c",'
            'fullname="/tmp/target.c",line="12",thread-groups=["i1"],'
            'times="0",original-location="main"},'
            'bkpt={number="2",type="breakpoint",disp="keep",enabled="n",'
            'addr="0x00401590",func="compute",file="target.c",'
            'fullname="/tmp/target.c",line="5",thread-groups=["i1"],'
            'times="1",original-location="compute"}]}')
    r = mi.parse_line(line)
    table = r.payload["BreakpointTable"]
    assert table["nr_rows"] == "2"
    assert len(table["hdr"]) == 2
    assert table["hdr"][0]["col_name"] == "number"
    body = table["body"]
    # body is a result-list: repeated `bkpt=` keys collapse to a list
    assert isinstance(body, dict)
    assert len(body["bkpt"]) == 2
    assert body["bkpt"][0]["number"] == "1"
    assert body["bkpt"][1]["enabled"] == "n"
    assert body["bkpt"][1]["line"] == "5"


def test_single_breakpoint_is_a_dict_not_a_list():
    """The classic MI trap: one row comes back as `bkpt={...}`, not a list."""
    line = ('^done,BreakpointTable={nr_rows="1",nr_cols="6",hdr=[],'
            'body=[bkpt={number="1",enabled="y",fullname="/tmp/t.c",'
            'line="12"}]}')
    r = mi.parse_line(line)
    body = r.payload["BreakpointTable"]["body"]
    assert isinstance(body["bkpt"], dict)
    assert body["bkpt"]["number"] == "1"


def test_deeply_nested():
    # MI `const` is always a quoted c-string; gdb never emits bare numbers
    line = '^done,a={b={c=[{d="1"},{d="2"}]},e=[["1","2"],["3","4"]]}'
    r = mi.parse_line(line)
    assert r.payload["a"]["b"]["c"] == [{"d": "1"}, {"d": "2"}]
    assert r.payload["a"]["e"] == [["1", "2"], ["3", "4"]]


def test_bare_numbers_are_not_valid_mi():
    # ...and must degrade to a parse error rather than half-parse
    r = mi.parse_line("^done,e=[[1,2]]")
    assert r.kind == mi.PARSE_ERROR


@pytest.mark.parametrize("raw,expected", [
    (r"\033", "\033"),          # ESC, the common case
    (r"\033[1;1H", "\033[1;1H"),
    (r"\0", "\0"),
    (r"\00", "\0"),
    (r"\000", "\0"),
    (r"\101", "A"),              # 0o101 == 65
    (r"\777", chr(0o777)),
    (r"\0333", "\033" + "3"),    # at most three octal digits
    (r"\08", "\0" + "8"),        # 8 is not octal, so \0 then literal 8
])
def test_octal_escapes(raw, expected):
    assert mi.unescape_c_string(raw) == expected


def test_octal_escape_survives_a_stream_record():
    # gdb passes terminal escapes through ~ records; mis-decoding \033 as
    # NUL+"33" would corrupt every one of them
    r = mi.parse_line(r'~"\033[2J\033[H"')
    assert r.kind == mi.STREAM
    assert r.payload == "\033[2J\033[H"


def test_variable_names_may_contain_dash_and_underscore():
    r = mi.parse_line('^done,thread-id="1",some_var="x"')
    assert r.payload == {"thread-id": "1", "some_var": "x"}


def test_empty_string_value():
    r = mi.parse_line('^done,msg=""')
    assert r.payload == {"msg": ""}


def test_value_containing_braces_inside_string():
    r = mi.parse_line(r'^done,msg="a {b} [c] = d"')
    assert r.payload["msg"] == "a {b} [c] = d"


def test_value_containing_escaped_quote():
    r = mi.parse_line(r'^done,msg="he said \"hi\""')
    assert r.payload["msg"] == 'he said "hi"'


# ---------------------------------------------------------------------------
# malformed input must degrade, not raise
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line", [
    "garbage without a marker",
    "^",
    "~unterminated",
    '^done,broken={',
    "^done,x=",
    "*stopped,",
    "\x00\x01binary junk",
    "^done,a=\"\\",
])
def test_malformed_lines_do_not_raise(line):
    r = mi.parse_line(line)
    assert r is not None
    assert r.kind in (mi.PARSE_ERROR, mi.RESULT, mi.STREAM, mi.ASYNC,
                      mi.PROMPT)


def test_parse_error_keeps_raw_text():
    r = mi.parse_line("this is not MI at all")
    assert r.kind == mi.PARSE_ERROR
    assert r.raw == "this is not MI at all"


def test_empty_line_returns_none():
    assert mi.parse_line("") is None


# ---------------------------------------------------------------------------
# LineSplitter: chunk boundaries
# ---------------------------------------------------------------------------
def test_splitter_complete_lines():
    s = mi.LineSplitter()
    assert s.feed(b"^done\n(gdb)\n") == ["^done", "(gdb)"]


def test_splitter_partial_line_is_buffered():
    s = mi.LineSplitter()
    assert s.feed(b"^do") == []
    assert s.pending == b"^do"
    assert s.feed(b"ne\n") == ["^done"]


def test_splitter_handles_crlf():
    s = mi.LineSplitter()
    assert s.feed(b"^done\r\n(gdb)\r\n") == ["^done", "(gdb)"]


def test_splitter_byte_at_a_time():
    s = mi.LineSplitter()
    out = []
    for b in b'~"hello\\n"\n^done\n':
        out.extend(s.feed(bytes([b])))
    assert out == ['~"hello\\n"', "^done"]


def test_splitter_utf8_split_across_chunks():
    s = mi.LineSplitter()
    data = '~"中文\\n"\n'.encode("utf-8")
    out = []
    # split in the middle of the multi-byte sequence
    cut = data.index("中".encode("utf-8")[0:1]) + 1
    out.extend(s.feed(data[:cut]))
    out.extend(s.feed(data[cut:]))
    assert out == ['~"中文\\n"']
    r = mi.parse_line(out[0])
    assert r.payload == "中文\n"


def test_splitter_accepts_str():
    s = mi.LineSplitter()
    assert s.feed("^done\n") == ["^done"]


def test_splitter_large_payload():
    # 593 KiB / 15094 lines was the measured bulk case (python_specs.md §13.3)
    s = mi.LineSplitter()
    chunk = b'~"line of output text padding padding padding\\n"\n' * 500
    total = 0
    for _ in range(30):
        total += len(s.feed(chunk))
    assert total == 15000
    assert s.pending == b""
