"""The `o` file selection dialog -- Python counterpart of cgdb/filedlg.cpp.

The dialog lists the source files the debuggee knows about, takes over the
whole screen (if_draw()'s FILE_DLG branch draws only the dialog), and speaks
the same vim keys as filedlg_recv_char (filedlg.cpp:584-673):

    j k / arrows      move the selection
    Ctrl-D/U, F/B     half / full page
    h l               horizontal pan
    gg                top; digits + G jump to a numbered row (a bare G does
                      nothing -- that is C's behaviour, kept verbatim)
    / ? and n N       regex search within the list
    Enter             open the selected file; q or ESC cancels

The file list itself arrives from MI `-file-list-exec-source-files`, which
is what C's tgdb_request_inferiors_source_files turns into here.
"""

import re

LABEL = "Select a file or press q to cancel."


class FileDialog:
    """Selection state over the file list (struct file_buffer)."""

    def __init__(self):
        self.files = []          # [(display name, fullname)]
        self.sel = 0
        self.left = 0            # horizontal pan (sel_col / scr offset)
        self.search = None       # compiled regex of the last /-search
        self.search_row = None   # row of the last hit
        self.line_number = ""    # digits typed for G

    # --- population -------------------------------------------------------
    def set_files(self, entries):
        self.files = sorted(entries, key=lambda e: e[0].lower())
        self.sel = 0
        self.left = 0
        self.search = None
        self.search_row = None
        self.line_number = ""

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
    def find(self, pattern, forward, ignorecase=False):
        """Advance to the next row whose display name matches.

        `pattern` compiles a new regex (empty pattern reuses the last);
        returns the row index or None when nothing matches.
        """
        if pattern:
            try:
                self.search = re.compile(
                    pattern, re.IGNORECASE if ignorecase else 0)
            except re.error:
                return None
        rx = self.search
        if rx is None:
            return None
        n = len(self.files)
        if forward:
            first = (self.search_row + 1) if self.search_row is not None \
                else 0
            rng = range(first, n)
        else:
            first = (self.search_row - 1) if self.search_row is not None \
                else n - 1
            rng = range(first, -1, -1)
        for i in rng:
            if rx.search(self.files[i][0]):
                self.search_row = i
                self.sel = i
                return i
        return None
