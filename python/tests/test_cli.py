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
