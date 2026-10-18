#if HAVE_CONFIG_H
#include "config.h"
#endif /* HAVE_CONFIG_H */

#if HAVE_CURSES_H
#include <curses.h>
#elif HAVE_NCURSES_CURSES_H
#include <ncurses/curses.h>
#elif HAVE_PDCURSES_H
#include <pdcurses.h>    /* mingw32 root wrapper: PDC_WIDE + PDC_FORCE_UTF8 */
#endif

#include "cgdb_clog.h"
#include "sys_util.h"
#include "sys_win.h"

#ifdef _WIN32
#include "console_win.h"
#endif

#ifdef _WIN32
/* Encoded attribute values (see sys_win.h): the real PDCurses wide
 * attribute constants are 64-bit and cannot round-trip through the
 * int-based swin API. swin_expand_attr converts at the boundary. */
const int SWIN_A_NORMAL = 0;
const int SWIN_A_STANDOUT = 0x001;
const int SWIN_A_UNDERLINE = 0x002;
const int SWIN_A_REVERSE = 0x004;
const int SWIN_A_BLINK = 0x008;
const int SWIN_A_DIM = 0x010;
const int SWIN_A_BOLD = 0x020;
#else
const int SWIN_A_NORMAL = A_NORMAL;
const int SWIN_A_STANDOUT = A_STANDOUT;
const int SWIN_A_UNDERLINE = A_UNDERLINE;
const int SWIN_A_REVERSE = A_REVERSE;
const int SWIN_A_BLINK = A_BLINK;
const int SWIN_A_DIM = A_DIM;
const int SWIN_A_BOLD = A_BOLD;
#endif

const int SWIN_KEY_BACKSPACE = KEY_BACKSPACE;

SWIN_CHTYPE SWIN_SYM_VLINE;
SWIN_CHTYPE SWIN_SYM_HLINE;
SWIN_CHTYPE SWIN_SYM_LTEE;

static bool initscr_intialized = false;

#ifdef _WIN32
/* bits of an encoded attribute */
#define SWIN_ATTR_CHAR_MASK 0x000000ff
#define SWIN_ATTR_PAIR_SHIFT 20
#define SWIN_ATTR_MAX_PAIRS 4095

static chtype swin_expand_attr(int attrs)
{
    chtype ch = 0;

    if (attrs & SWIN_A_STANDOUT)
        ch |= A_STANDOUT;
    if (attrs & SWIN_A_UNDERLINE)
        ch |= A_UNDERLINE;
    if (attrs & SWIN_A_REVERSE)
        ch |= A_REVERSE;
    if (attrs & SWIN_A_BLINK)
        ch |= A_BLINK;
    if (attrs & SWIN_A_DIM)
        ch |= A_DIM;
    if (attrs & SWIN_A_BOLD)
        ch |= A_BOLD;

    if ((attrs & ~SWIN_ATTR_CHAR_MASK) != 0) {
        int pair = (attrs >> SWIN_ATTR_PAIR_SHIFT) - 1;

        if (pair >= 0)
            ch |= COLOR_PAIR(pair);
    }

    return ch;
}
#endif

/**
 * Set the ESCDELAY environment variable.
 *
 * This environment variable is necessary for ncurses to not delay
 * user input when the ESCAPE key is pressed. CGDB does it's own handling
 * of escape.
 *
 * @return
 * True on success or False on failure
 */
static bool swin_set_escdelay(void)
{
#ifdef _WIN32
    /* ESCDELAY is an ncurses concept; PDCurses does not use it and CGDB
     * does its own escape disambiguation in kui. */
    return true;
#else
    static char escdelay[] = "ESCDELAY=0";
    bool success = putenv(escdelay) == 0;

    if (!success)
        clog_error(CLOG_CGDB, "putenv(\"%s\") failed", escdelay);

    return success;
#endif
}

bool swin_start()
{
    bool success = swin_set_escdelay();
    if (success) {
        success = swin_initscr() != NULL;
        if (success) {
            if (swin_has_colors()) {
                swin_start_color();
                swin_use_default_colors();
            }

            swin_refresh();

#ifdef _WIN32
            /* PDCurses initscr() rewrites the console input mode and
             * clears the VT-input bit (verified); re-assert it so kui
             * keeps reading escape sequences. */
            win_console_assert_input_mode();
#endif
        }
    }

    return success;
}

/* Determines the terminal type and initializes all data structures. */
SWINDOW *swin_initscr()
{
    SWINDOW *win = (SWINDOW *)initscr();

#ifdef HAVE_CYGWIN
    SWIN_SYM_VLINE = ':';
#else
    SWIN_SYM_VLINE = ACS_VLINE;
#endif

    SWIN_SYM_HLINE = ACS_HLINE;
    SWIN_SYM_LTEE = ACS_LTEE;
    
    initscr_intialized = true;

    return win;
}

int swin_endwin()
{
    int result = 0;
    
    if (initscr_intialized)
        result = endwin();

    return result;
}

int swin_lines()
{
    return LINES;
}

int swin_cols()
{
    return COLS;
}

int swin_colors()
{
    return COLORS;
}

int swin_color_pairs()
{
    return COLOR_PAIRS;
}

int swin_has_colors()
{
    return has_colors();
}

int swin_start_color()
{
    int result = start_color();
    if (result == ERR) {
        clog_error(CLOG_CGDB, "start_color failed");
    }
    return result;
}

int swin_use_default_colors()
{
    int result = use_default_colors();
    if (result == ERR) {
        clog_error(CLOG_CGDB, "use_default_colors failed");
    }
    return result;
}

bool swin_supports_default_color_pairs_extension()
{
#ifdef _WIN32
    /* The ncurses -1-color extension probe does not apply to PDCurses;
     * report "no" so callers stay inside the standard pair range. */
    return false;
#else
    int result = init_pair(65, -1, COLOR_BLACK);
    return result != ERR;
#endif
}

int swin_resizeterm(int lines, int columns)
{
#ifdef _WIN32
    return resize_term(lines, columns);
#else
    return resizeterm(lines, columns);
#endif
}

int swin_scrl(int n)
{
    return scrl(n);
}

int swin_keypad(SWINDOW *win, int bf)
{
    return keypad((WINDOW *)win, bf);
}

char *swin_tigetstr(const char *capname)
{
#ifdef _WIN32
    /* PDCurses has no terminfo; kui_term falls back to its built-in
     * escape-sequence tables when this returns NULL. */
    (void) capname;
    return NULL;
#else
    return tigetstr((char*)capname);
#endif
}

int swin_move(int y, int x)
{
    return move(y, x);
}

int swin_wmove(SWINDOW *win, int y, int x)
{
    return wmove((WINDOW *)win, y, x);
}

int swin_wattron(SWINDOW *win, int attrs)
{
#ifdef _WIN32
    return wattron((WINDOW *)win, swin_expand_attr(attrs));
#else
    return wattron((WINDOW *)win, attrs);
#endif
}

int swin_wattroff(SWINDOW *win, int attrs)
{
#ifdef _WIN32
    return wattroff((WINDOW *)win, swin_expand_attr(attrs));
#else
    return wattroff((WINDOW *)win, attrs);
#endif
}

SWINDOW *swin_newwin(int nlines, int ncols, int begin_y, int begin_x)
{
    return (SWINDOW *)newwin(nlines, ncols, begin_y, begin_x);
}

int swin_delwin(SWINDOW *win)
{
    return delwin((WINDOW *)win);
}

int swin_curs_set(int visibility)
{
    return curs_set(visibility);
}

int swin_getcurx(const SWINDOW *win)
{
    return getcurx((WINDOW *)win);
}

int swin_getcury(const SWINDOW *win)
{
    return getcury((WINDOW *)win);
}

int swin_getbegx(const SWINDOW *win)
{
    return getbegx((WINDOW *)win);
}

int swin_getbegy(const SWINDOW *win)
{
    return getbegy((WINDOW *)win);
}

int swin_getmaxx(const SWINDOW *win)
{
    return getmaxx((WINDOW *)win);
}

int swin_getmaxy(const SWINDOW *win)
{
    return getmaxy((WINDOW *)win);
}

int swin_werase(SWINDOW *win)
{
    return werase((WINDOW *)win);
}

int swin_wvline(SWINDOW *win, SWIN_CHTYPE ch, int n)
{
    return wvline((WINDOW *)win, ch, n);
}

int swin_waddch(SWINDOW *win, const SWIN_CHTYPE ch)
{
    return waddch((WINDOW *)win, ch);
}

int swin_wprintw(SWINDOW *win, const char *fmt, ...)
{
    int ret;
    va_list ap;

    va_start(ap, fmt);
    ret = vw_printw((WINDOW *)win, fmt, ap);
    va_end(ap);

    return ret;
}

int swin_waddnstr(SWINDOW *win, const char *str, int n)
{
    return waddnstr((WINDOW *)win, str, n);
}

int swin_wclrtoeol(SWINDOW *win)
{
    return wclrtoeol((WINDOW *)win);
}

int swin_mvwprintw(SWINDOW *win, int y, int x, const char *fmt, ...)
{
    int ret;

    ret = wmove((WINDOW *)win, y, x);
    if (ret != ERR)
    {
        va_list ap;

        va_start(ap, fmt);
        ret = vw_printw((WINDOW *)win, fmt, ap);
        va_end(ap);
    }

    return ret;
}

int swin_refresh()
{
    return refresh();
}

int swin_wnoutrefresh(SWINDOW *win)
{
    return wnoutrefresh((WINDOW *)win);
}

int swin_wrefresh(SWINDOW *win)
{
    return wrefresh((WINDOW *)win);
}

int swin_doupdate()
{
    return doupdate();
}

int swin_init_pair(int pair, int f, int b)
{
    int result = init_pair(pair, f, b);
    if (result == ERR) {
        clog_error(CLOG_CGDB, "init_pair failed pair=%d f=%d b=%d", pair, f, b);
    }

    return result;
}

int swin_pair_content(int pair, int *fin, int *bin)
{
    int ret;
    short f, b;

    ret = pair_content(pair, &f, &b);
    if (ret == ERR) {
        clog_error(CLOG_CGDB, "pair_content failed pair=%d", pair);
    }

    *fin = f;
    *bin = b;
    return ret;
}

int swin_color_pair(int pair)
{
#ifdef _WIN32
    if (pair < 0 || pair > SWIN_ATTR_MAX_PAIRS)
        return 0;
    return (pair + 1) << SWIN_ATTR_PAIR_SHIFT;
#else
    return COLOR_PAIR(pair);
#endif
}

int swin_raw(void)
{
    return raw();
}
