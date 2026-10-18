/* scroller_win.cpp - Windows GDB window: synthesized dumb-terminal model.
 *
 * On Linux the GDB window is a libvterm terminal fed by the console pty
 * (scroller.cpp + vterminal.cpp). On Windows there is no pty: gdb runs
 * MI-over-pipes, all displayable output arrives as console stream
 * records and the line editor in lib/tgdb echoes by emitting CR/BS/
 * overwrite sequences. This implementation models exactly that subset
 * (a "dumb terminal"): a list of wrapped rows, a current row, and a
 * cursor column, with the same scr_* API so interface.cpp is unchanged.
 */

#if HAVE_CONFIG_H
#include "config.h"
#endif /* HAVE_CONFIG_H */

#if HAVE_CTYPE_H
#include <ctype.h>
#endif

#if HAVE_STDLIB_H
#include <stdlib.h>
#endif /* HAVE_STDLIB_H */

#if HAVE_STRING_H
#include <string.h>
#endif /* HAVE_STRING_H */

#include <algorithm>
#include <deque>
#include <string>

/* Local Includes */
#include "sys_util.h"
#include "sys_win.h"
#include "cgdbrc.h"
#include "highlight_groups.h"
#include "scroller.h"
#include "highlight.h"

struct scroller {
    /* The window the scroller is displayed on. NULL when the height
     * of the scroller is zero (minimized or terminal height of 1);
     * the model keeps working so input can continue. */
    SWINDOW *win;

    /* Model dimensions (at least 1, see scr_new_vterminal note in the
     * Linux scroller). */
    int width;
    int height;

    /* Completed rows (bounded by the scrollback size). The current
     * (in-progress) row is NOT in here; it is `cur`. */
    std::deque<std::string> lines;
    int scrollback_limit;

    /* The row under construction and the cursor column in it. */
    std::string cur;
    int cur_col;

    /* Scroll mode */
    bool in_scroll_mode;
    int scroll_cursor_row, scroll_cursor_col;
    /* Rows scrolled back from the bottom (0 = live view). */
    int delta;

    /* Search mode (can only be entered from scroll mode) */
    bool in_search_mode;
    int delta_init, search_row_init, search_col_init;
    bool forward;
    bool icase;
    struct hl_regex_info *hlregex;
    /* Absolute row and column range of the current match */
    int search_row, search_col_start, search_col_end;
    std::string last_regex;
};

/* ------------------- */
/* Model helpers */
/* ------------------- */

/* Total number of rows: committed lines plus the current row. */
static int scr_total_rows(struct scroller *scr)
{
    return (int) scr->lines.size() + 1;
}

static int scr_max_delta(struct scroller *scr)
{
    int total = scr_total_rows(scr);

    return std::max(0, total - scr->height);
}

/* Content of an absolute row index (last one is the current row). */
static const std::string &scr_row_text(struct scroller *scr, int row)
{
    static const std::string empty;

    if (row < 0 || row >= scr_total_rows(scr))
        return empty;

    if (row < (int) scr->lines.size())
        return scr->lines[row];

    return scr->cur;
}

/* First absolute row visible in the current view. */
static int scr_view_start(struct scroller *scr)
{
    int total = scr_total_rows(scr);

    return std::max(0, total - scr->height - scr->delta);
}

static void scr_commit_row(struct scroller *scr)
{
    scr->lines.push_back(scr->cur);

    /* Bound the scrollback, keeping some headroom above the limit so
     * the view does not jump while rows stream in. */
    while ((int) scr->lines.size() > scr->scrollback_limit + 64)
        scr->lines.pop_front();

    scr->cur.clear();
    scr->cur_col = 0;
}

/* Append one byte to the model, dumb-terminal style: CR returns to
 * column 0, BS moves left, LF commits the row, TAB expands, printable
 * and UTF-8 bytes overwrite at the cursor and advance it. Rows wrap at
 * the width; wrapping never splits a UTF-8 sequence (a continuation
 * byte is allowed to overflow slightly instead). */
static void scr_put_byte(struct scroller *scr, unsigned char c)
{
    switch (c) {
        case '\n':
            scr_commit_row(scr);
            return;
        case '\r':
            scr->cur_col = 0;
            return;
        case '\b':
            if (scr->cur_col > 0)
                scr->cur_col--;
            return;
        case '\t': {
            int next_stop = (scr->cur_col + 8) & ~7;

            while (scr->cur_col < next_stop) {
                if ((size_t) scr->cur_col < scr->cur.size())
                    scr->cur[scr->cur_col] = ' ';
                else
                    scr->cur.push_back(' ');
                scr->cur_col++;
            }
            return;
        }
        case 0x07:                  /* BEL */
            return;
        default:
            break;
    }

    if (c < 0x20)
        return;                     /* other control bytes: ignore */

    /* Wrap when the column reaches the width, but never in the middle
     * of a UTF-8 sequence. */
    if (scr->cur_col >= scr->width && (c & 0xc0) != 0x80) {
        scr_commit_row(scr);
    }

    if ((size_t) scr->cur_col < scr->cur.size())
        scr->cur[scr->cur_col] = (char) c;
    else
        scr->cur.push_back((char) c);
    scr->cur_col++;
}

/* ----------------- */
/* Exposed Functions */
/* ----------------- */

struct scroller *scr_new(SWINDOW *win)
{
    struct scroller *rv = new scroller();

    rv->win = win;
    rv->width = std::max(swin_getmaxx(win), 1);
    rv->height = std::max(swin_getmaxy(win), 1);
    rv->scrollback_limit = cgdbrc_get_int(CGDBRC_SCROLLBACK_BUFFER_SIZE);
    if (rv->scrollback_limit < 64)
        rv->scrollback_limit = 64;

    rv->cur_col = 0;

    rv->in_scroll_mode = false;
    rv->scroll_cursor_row = rv->scroll_cursor_col = 0;
    rv->delta = 0;

    rv->in_search_mode = false;
    rv->hlregex = NULL;
    rv->search_row = rv->search_col_start = rv->search_col_end = 0;
    rv->delta_init = 0;
    rv->search_row_init = rv->search_col_init = 0;
    rv->forward = true;
    rv->icase = false;

    return rv;
}

void scr_free(struct scroller *scr)
{
    hl_regex_free(&scr->hlregex);
    scr->hlregex = NULL;

    delete scr;
}

void scr_set_scroll_mode(struct scroller *scr, bool mode)
{
    if (scr->in_scroll_mode == mode)
        return;

    if (mode) {
        /* Seed the cursor at the live cursor position. */
        scr->scroll_cursor_row = scr->height - 1;
        scr->scroll_cursor_col = std::min(scr->cur_col, scr->width - 1);
    } else {
        /* Leaving scroll mode returns to the live view. */
        scr->delta = 0;
    }

    scr->in_scroll_mode = mode;
}

bool scr_scroll_mode(struct scroller *scr)
{
    return scr->in_scroll_mode;
}

void scr_up(struct scroller *scr, int nlines)
{
    if (!scr->in_scroll_mode)
        return;

    scr->delta = std::min(scr->delta + nlines, scr_max_delta(scr));
}

void scr_down(struct scroller *scr, int nlines)
{
    if (!scr->in_scroll_mode)
        return;

    scr->delta = std::max(scr->delta - nlines, 0);
}

void scr_home(struct scroller *scr)
{
    if (!scr->in_scroll_mode)
        return;

    scr->delta = scr_max_delta(scr);
}

void scr_end(struct scroller *scr)
{
    if (!scr->in_scroll_mode)
        return;

    scr->delta = 0;
}

void scr_left(struct scroller *scr)
{
    if (!scr->in_scroll_mode)
        return;

    if (scr->scroll_cursor_col > 0)
        scr->scroll_cursor_col--;
}

void scr_right(struct scroller *scr)
{
    if (!scr->in_scroll_mode)
        return;

    if (scr->scroll_cursor_col < scr->width - 1)
        scr->scroll_cursor_col++;
}

void scr_beginning_of_row(struct scroller *scr)
{
    if (!scr->in_scroll_mode)
        return;

    scr->scroll_cursor_col = 0;
}

void scr_end_of_row(struct scroller *scr)
{
    if (!scr->in_scroll_mode)
        return;

    scr->scroll_cursor_col = scr->width - 1;
}

void scr_enable_search(struct scroller *scr, bool forward, bool icase)
{
    if (!scr->in_scroll_mode || scr->in_search_mode)
        return;

    scr->in_search_mode = true;
    scr->forward = forward;
    scr->icase = icase;

    scr->delta_init = scr->delta;
    scr->search_row_init = scr_view_start(scr) + scr->scroll_cursor_row;
    scr->search_col_init = scr->scroll_cursor_col;

    scr->search_row = -1;
    scr->search_col_start = scr->search_col_end = 0;
}

void scr_disable_search(struct scroller *scr, bool accept)
{
    if (!scr->in_search_mode)
        return;

    if (accept && scr->search_row >= 0) {
        /* Move the scroll cursor to the match. */
        int row = scr->search_row - scr_view_start(scr);

        if (row >= 0 && row < scr->height) {
            scr->scroll_cursor_row = row;
            scr->scroll_cursor_col =
                    std::min(scr->search_col_start, scr->width - 1);
        }
    } else {
        /* Restore the original position. */
        scr->delta = scr->delta_init;
    }

    scr->in_search_mode = false;
}

bool scr_search_mode(struct scroller *scr)
{
    return scr->in_search_mode;
}

/* Search one row starting at column `from_col`; on match fills
 * start/end (row-relative columns) and returns 1. */
static int scr_search_row(struct scroller *scr, int row, int from_col,
        const char *regex, int *start, int *end)
{
    const std::string &text = scr_row_text(scr, row);
    std::string sub = text.substr(std::min((size_t) from_col,
                                           text.size()));
    int s = 0, e = 0;
    int matched = hl_regex_search(&scr->hlregex, sub.c_str(), regex,
                                  scr->icase, &s, &e);

    if (matched > 0) {
        *start = s + from_col;
        *end = e + from_col;
        return 1;
    }

    return matched;
}

/* Walk the rows in the given direction calling scr_search_row. */
static int scr_search_all(struct scroller *scr, const char *regex,
        bool forward)
{
    int total = scr_total_rows(scr);
    int wrapscan_enabled = cgdbrc_get_int(CGDBRC_WRAPSCAN);
    int row = scr->search_row_init;
    int col = scr->search_col_init;
    int matched = 0;

    /* Start after the saved position. */
    if (forward) {
        if (col < scr->width - 1)
            col++;
        else {
            col = 0;
            row = (row + 1) % total;
        }
    } else {
        if (col > 0) {
            col--;
        } else {
            row = row > 0 ? row - 1 : total - 1;
            col = std::min(scr->width - 1,
                    (int) scr_row_text(scr, row).size());
        }
    }

    for (;;) {
        int start = 0, end = 0;

        matched = scr_search_row(scr, row, col, regex, &start, &end);
        if (matched > 0) {
            /* Scroll the match into view. */
            scr->delta = std::min(
                    std::max(0, total - scr->height - (row - 1)),
                    scr_max_delta(scr));
            scr->search_row = row;
            scr->search_col_start = start;
            scr->search_col_end = end;
            break;
        }

        if (wrapscan_enabled && row == scr->search_row_init && col == 0)
            break;
        if (!wrapscan_enabled && forward && row == total - 1)
            break;
        if (!wrapscan_enabled && !forward && row == 0)
            break;

        if (forward) {
            row = (row + 1) % total;
            col = 0;
        } else {
            row = row > 0 ? row - 1 : total - 1;
            col = 0;
        }
    }

    return matched;
}

int scr_search_regex(struct scroller *scr, const char *regex)
{
    if (!scr || !regex || !scr->in_search_mode)
        return -1;

    scr->last_regex = regex;

    if (scr_search_all(scr, regex, scr->forward) > 0)
        return 1;

    return 0;
}

void scr_search_next(struct scroller *scr, bool forward, bool icase)
{
    if (scr->last_regex.size() > 0) {
        scr_enable_search(scr, forward, icase);
        scr_search_regex(scr, scr->last_regex.c_str());
        scr_disable_search(scr, true);
    }
}

void scr_push_screen_to_scrollback(struct scroller *scr)
{
    /* No terminal emulation on Windows: the visible rows are already
     * part of the model. Committing the current row is the closest
     * equivalent of readline's full-screen repaint. */
    if (!scr->cur.empty())
        scr_commit_row(scr);
}

void scr_add(struct scroller *scr, const char *buf)
{
    if (!scr || !buf)
        return;

    /* Scroll mode pins the view; live output still enters the model
     * (same as the Linux scroller). */
    for (const char *p = buf; *p; ++p)
        scr_put_byte(scr, (unsigned char) *p);
}

void scr_move(struct scroller *scr, SWINDOW *win)
{
    scr->win = win;
    scr->width = std::max(swin_getmaxx(win), 1);
    scr->height = std::max(swin_getmaxy(win), 1);

    /* Committed rows keep their wrap; the current row re-wraps as the
     * editor redraws it. */
    scr->delta = std::min(scr->delta, scr_max_delta(scr));
}

void scr_refresh(struct scroller *scr, int focus, enum win_refresh dorefresh)
{
    int view_start;
    int highlight_attr, search_attr;
    int cursor_row, cursor_col;

    if (!scr->win)
        return;

    highlight_attr = hl_groups_get_attr(hl_groups_instance,
            HLG_SCROLL_MODE_STATUS);
    search_attr = hl_groups_get_attr(hl_groups_instance, HLG_INCSEARCH);

    view_start = scr_view_start(scr);

    for (int r = 0; r < scr->height; ++r) {
        int abs = view_start + r;
        const std::string &text = scr_row_text(scr, abs);
        size_t len = text.size();

        if (len > (size_t) scr->width)
            len = scr->width;

        if (scr->in_search_mode && scr->search_row == abs &&
                scr->search_col_end > 0) {
            /* draw pre / highlighted match / post */
            int s = std::min(scr->search_col_start, (int) len);
            int e = std::min(scr->search_col_end, (int) len);

            swin_wmove(scr->win, r, 0);
            swin_waddnstr(scr->win, text.data(),
                    (int) std::min((size_t) s, text.size()));
            if (e > s) {
                swin_wattron(scr->win, search_attr);
                swin_waddnstr(scr->win, text.data() + s, e - s);
                swin_wattroff(scr->win, search_attr);
            }
            if ((size_t) e < len)
                swin_waddnstr(scr->win, text.data() + e,
                        (int) (len - e));
            swin_wclrtoeol(scr->win);
        } else {
            swin_wmove(scr->win, r, 0);
            if (len > 0)
                swin_waddnstr(scr->win, text.data(), (int) len);
            swin_wclrtoeol(scr->win);
        }

        /* In scroll mode, overlay the scroll position at the top. */
        if (scr->in_scroll_mode && r == 0) {
            char status[64];
            size_t status_len;

            snprintf(status, sizeof(status), "[%d/%d]", scr->delta,
                    scr_max_delta(scr));
            status_len = strlen(status);
            if (status_len < (size_t) scr->width) {
                swin_wattron(scr->win, highlight_attr);
                swin_mvwprintw(scr->win, r, scr->width - (int) status_len,
                        "%s", status);
                swin_wattroff(scr->win, highlight_attr);
            }
        }
    }

    if (scr->in_scroll_mode) {
        cursor_row = scr->scroll_cursor_row;
        cursor_col = scr->scroll_cursor_col;
    } else {
        cursor_row = scr_total_rows(scr) - 1 - view_start;
        cursor_col = std::min(scr->cur_col, scr->width - 1);
    }

    /* Show the cursor when the scroller is in focus */
    if (focus) {
        if (cursor_row >= 0 && cursor_row < scr->height)
            swin_wmove(scr->win, cursor_row, cursor_col);
        swin_curs_set(1);
    } else {
        /* Hide the cursor */
        swin_curs_set(0);
    }

    switch (dorefresh) {
        case WIN_NO_REFRESH:
            swin_wnoutrefresh(scr->win);
            break;
        case WIN_REFRESH:
            swin_wrefresh(scr->win);
            break;
    }
}
