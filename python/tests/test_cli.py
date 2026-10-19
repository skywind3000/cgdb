"""CLI-level tests for cgdb.py: argument parsing and path resolution.

The end-to-end behaviour of the whole program is covered in tests/e2e; these
cover the pure helpers, which is where Windows path handling actually lives.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cgdb                                                    # noqa: E402


# ---------------------------------------------------------------------------
# -d DEBUGGER resolution
# ---------------------------------------------------------------------------
def test_bare_name_is_looked_up_on_path(tmp_path, monkeypatch):
    exe = tmp_path / "mygdb"
    exe.write_text("")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cgdb._executable_exists("mygdb")
    assert not cgdb._executable_exists("nosuchgdb")


@pytest.mark.skipif(os.name != "nt", reason="PATHEXT is a Windows thing")
def test_bare_name_honours_pathext(tmp_path, monkeypatch):
    exe = tmp_path / "mygdb.exe"
    exe.write_text("")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".EXE")
    assert cgdb._executable_exists("mygdb")


@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Windows thing")
def test_windows_absolute_paths_are_paths_not_path_lookups(tmp_path):
    exe = tmp_path / "gdb.exe"
    exe.write_text("")
    back = str(exe)
    forward = back.replace("\\", "/")
    assert cgdb._executable_exists(back)
    # forward slashes are legal on Windows and os.path.sep is only "\\"
    assert cgdb._executable_exists(forward)
    assert not cgdb._executable_exists(back + ".nope")


def test_relative_path_with_either_separator_is_not_a_path_lookup(tmp_path,
                                                                  monkeypatch):
    """`sub/gdb` must be resolved against the cwd, not searched for on PATH.

    The old test was `os.path.sep in path`, which on Windows is only "\\" -- so
    a forward-slash relative path fell through to the PATH loop and was joined
    onto every PATH entry instead.
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "gdb").write_text("")
    monkeypatch.chdir(tmp_path)
    # put tmp_path on PATH so a spurious PATH lookup would still find it under
    # a different spelling; the assertion is about which branch was taken
    monkeypatch.setenv("PATH", str(tmp_path / "other"))
    assert cgdb._executable_exists("sub/gdb")
    assert cgdb._executable_exists(os.path.join("sub", "gdb"))
    assert not cgdb._executable_exists("nope/gdb")


@pytest.mark.skipif(os.name != "nt", reason="drive-relative paths are Windows")
def test_drive_relative_path_is_a_path(tmp_path, monkeypatch):
    """`C:gdb.exe` (drive-relative, no separator) is a path, not a bare name.

    It contains no os.path.sep, so the old `os.path.sep in path` test sent it
    into the PATH loop.
    """
    monkeypatch.chdir(tmp_path)
    exe = tmp_path / "gdb.exe"
    exe.write_text("")
    drive = os.path.splitdrive(str(tmp_path))[0]      # "C:", colon included
    assert cgdb._executable_exists(drive + "gdb.exe")


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Windows thing")
def test_d_option_keeps_a_windows_path_verbatim():
    opts = cgdb.parse_args(["-d", r"D:\Dev\mingw32\bin\gdb.exe"])
    assert opts["gdb"] == r"D:\Dev\mingw32\bin\gdb.exe"
    opts = cgdb.parse_args([r"-dD:\Dev\mingw32\bin\gdb.exe"])
    assert opts["gdb"] == r"D:\Dev\mingw32\bin\gdb.exe"


def test_everything_after_double_dash_goes_to_gdb():
    opts = cgdb.parse_args(["--", "-q", r"C:\build\prog.exe"])
    assert opts["gdb_args"] == ["-q", r"C:\build\prog.exe"]
    assert opts["gdb"] is None


def test_source_option_keeps_the_path_verbatim():
    opts = cgdb.parse_args(["--source", r"C:\src\a b\main.c"])
    assert opts["source"] == r"C:\src\a b\main.c"
    opts = cgdb.parse_args(["--source=/src/a.c"])
    assert opts["source"] == "/src/a.c"


def test_a_double_dash_argument_survives():
    """A path argument starting with `-` must not be parsed as an option."""
    opts = cgdb.parse_args(["--", "--weird-flag"])
    assert opts["gdb_args"] == ["--weird-flag"]


# ---------------------------------------------------------------------------
# cgdb's options stop at the first non-cgdb argument (parse_long_options)
# ---------------------------------------------------------------------------
def test_bare_program_name_goes_straight_to_gdb():
    """`cgdb my_program` works in the original: the first argument that
    is not a cgdb option ends the scan and the rest belongs to gdb."""
    opts = cgdb.parse_args(["my_program"])
    assert opts["gdb_args"] == ["my_program"]
    assert opts["gdb"] is None


def test_unknown_option_is_forwarded_to_gdb():
    """`cgdb -q my_program` also works in the original: getopt returns
    '?' for gdb's -q and cgdb hands the remainder over verbatim."""
    opts = cgdb.parse_args(["-q", "my_program"])
    assert opts["gdb_args"] == ["-q", "my_program"]


def test_options_after_the_program_stay_with_gdb():
    opts = cgdb.parse_args(["my_program", "-q", "--args"])
    assert opts["gdb_args"] == ["my_program", "-q", "--args"]


def test_cgdb_options_then_program():
    opts = cgdb.parse_args(["-d", "gdb", "-w", "my_program"])
    assert opts["gdb"] == "gdb"
    assert opts["wait"] is True
    assert opts["gdb_args"] == ["my_program"]


def test_no_arguments_at_all():
    opts = cgdb.parse_args([])
    assert opts["gdb_args"] == []
    assert opts["gdb"] is None


# ---------------------------------------------------------------------------
# the rc file: <cgdb home>/cgdbrc, like parse_cgdbrc_file (cgdb.cpp:244)
# ---------------------------------------------------------------------------
def test_rc_is_read_from_the_cgdb_home_dir(tmp_path, monkeypatch):
    home = tmp_path / ".cgdb"
    home.mkdir()
    (home / "cgdbrc").write_text("set wso=vertical\n", encoding="utf-8")
    monkeypatch.setenv("CGDB_DIR", str(home))
    from cgdbpy.cgdbrc import Config
    config = Config()
    # _load_rc returns the full command list; set lines are applied by the
    # caller, the rest are replayed by the app
    lines = cgdb._load_rc()
    for _no, line in lines:
        if line.split(None, 1)[0].lower() == "set":
            config.apply_set(line.split(None, 1)[1])
    assert config.get("winsplitorientation") == "vertical"


def test_rc_home_dir_resolution_order(monkeypatch, tmp_path):
    """$CGDB_DIR wins outright; otherwise ~/.cgdb with HOME ->
    USERPROFILE -> APPDATA on Windows (init_home_dir, cgdb.cpp:353)."""
    monkeypatch.setenv("CGDB_DIR", str(tmp_path / "custom"))
    assert cgdb._cgdb_home_dir() == str(tmp_path / "custom")
    monkeypatch.delenv("CGDB_DIR")
    for var in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(var, str(tmp_path / var))
    assert cgdb._cgdb_home_dir() == str(tmp_path / "HOME" / ".cgdb")
    monkeypatch.delenv("HOME")
    assert cgdb._cgdb_home_dir() == str(tmp_path / "USERPROFILE" / ".cgdb")
    monkeypatch.delenv("USERPROFILE")
    assert cgdb._cgdb_home_dir() == str(tmp_path / "APPDATA" / ".cgdb")


def test_missing_rc_is_silently_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("CGDB_DIR", str(tmp_path / "nowhere"))
    assert cgdb._load_rc() == []      # must not raise
