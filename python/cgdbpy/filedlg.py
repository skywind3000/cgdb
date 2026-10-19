"""The `o` file selection dialog -- Python counterpart of cgdb/filedlg.cpp.

The dialog lists the source files the debuggee knows about, takes over the
whole screen (if_draw()'s FILE_DLG branch draws only the dialog), and speaks
the same vim keys as filedlg_recv_char (filedlg.cpp:584-673):

    j k / arrows      move the selection
    Ctrl-D/U, F/B     half / full page
    h l               horizontal pan of long paths
    gg                top; digits + G jump to a numbered row (a bare G does
                      nothing -- that is C's behaviour, kept verbatim)
    / ? and n N       regex search within the list; filedlg.cpp hardcodes
                      case-insensitive matching and wraps the scan
    Enter             open the selected file; q or ESC cancels

The file list itself arrives from MI `-file-list-exec-source-files`, which
is what C's tgdb_request_inferiors_source_files turns into here.
"""

import os
import re

LABEL = "Select a file or press q to cancel."


def _path_group(path):
    """filedlg.cpp's three buckets: relative paths that start with `.` come
    first, absolute paths (`/`-spelled) go last, everything else between."""
    if path.startswith("."):
        return 0
    if path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", path):
        return 2
    return 1


class FileDialog:
    """Selection state over the file list (struct file_buffer)."""

    def __init__(self):
        self.files = []          # [(display name, fullname)]
        self.sel = 0
        self.left = 0            # horizontal pan (sel_col / scr offset)
        self.search = None       # compiled regex of the last /-search
        self.search_row = None   # row of the last hit
        self.search_dir = True   # direction of the last /-search
        self.line_number = ""    # digits typed for G
        self.message = ""        # the dialog's bottom status row

    # --- population -------------------------------------------------------
    def set_files(self, entries):
        """Adopt the source list, the way filedlg.cpp:139-176 builds it.

        Files that no longer exist on disk are skipped (C filters deleted
        compile temporaries the same way), duplicates collapse, and the
        sort is C's: `.`-relative group, plain names, absolute paths --
        strcmp (case-sensitive) inside each group.
        """
        seen = set()
        kept = []
        for name, full in entries:
            path = full or name
            if not os.path.exists(path):
                continue
            key = (name, full)
            if key in seen:
                continue
            seen.add(key)
            kept.append((name, full))
        kept.sort(key=lambda e: (_path_group(e[0]), e[0]))
        self.files = kept
        self.sel = 0
        self.left = 0
        self.search = None
        self.search_row = None
        self.line_number = ""
        self.message = ""

    def count(self):
        return len(self.files)

    def selected(self):
        if 0 <= self.sel < len(self.files):
            return self.files[self.sel]
        return ("", "")

    # --- navigation -------------------------------------------------------
    def vscroll(self, n):
        self.sel = max(0, min(self.sel + n, len(self.files) - 1))

    def goto(self, n):
        self.sel = max(0, min(n, len(self.files) - 1))

    # --- search (filedlg_search_regex) -------------------------------------
    def find(self, pattern, forward, live=False):
        """Advance to the next row whose name matches.

        filedlg.cpp hardcodes ignore-case (filedlg_search_regex is always
        called with icase=1) and wraps the scan at the ends; a new search
        starts below/above the selected row, and `n`/`N` replay the last
        direction. Returns the row index or None when nothing matches.
        """
        if pattern:
            try:
                self.search = re.compile(pattern, re.IGNORECASE)
            except re.error:
                return None
            self.search_dir = forward
        if self.search is None:
            return None
        forward = self.search_dir
        n = len(self.files)
        if live:
            start = self.sel if forward else self.sel - 1
        elif self.search_row is not None:
            start = self.search_row + 1 if forward else self.search_row - 1
        else:
            start = self.sel + 1 if forward else self.sel - 1
        if forward:
            order = list(range(start, n)) + list(range(0, max(0, start)))
        else:
            order = list(range(start, -1, -1)) + \
                list(range(n - 1, max(start, n - 1), -1))
        for i in order:
            if 0 <= i < n and self.search.search(self.files[i][0]):
                self.search_row = i
                self.sel = i
                return i
        return None
