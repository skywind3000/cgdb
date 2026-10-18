#if HAVE_CONFIG_H
#include "config.h"
#endif /* HAVE_CONFIG_H */

#include "console_win.h"

#include <windows.h>

#include "cgdb_clog.h"

static HANDLE g_console_in;
static HANDLE g_console_out;
static DWORD g_orig_in_mode;
static DWORD g_orig_out_mode;
static bool g_modes_saved = false;
static COORD g_last_size;
static bool g_size_known = false;

#define WIN_CONSOLE_INPUT_MODE \
    (ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_EXTENDED_FLAGS)
#define WIN_CONSOLE_OUTPUT_MODE \
    (ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING | \
     ENABLE_WRAP_AT_EOL_OUTPUT)

int win_console_setup(void)
{
    g_console_in = GetStdHandle(STD_INPUT_HANDLE);
    g_console_out = GetStdHandle(STD_OUTPUT_HANDLE);

    if (g_console_in == INVALID_HANDLE_VALUE ||
            g_console_out == INVALID_HANDLE_VALUE) {
        clog_error(CLOG_CGDB, "no console attached");
        return -1;
    }

    if (!g_modes_saved) {
        if (!GetConsoleMode(g_console_in, &g_orig_in_mode) ||
                !GetConsoleMode(g_console_out, &g_orig_out_mode)) {
            clog_error(CLOG_CGDB, "GetConsoleMode failed: %lu",
                       (unsigned long) GetLastError());
            return -1;
        }
        g_modes_saved = true;
    }

    if (!SetConsoleMode(g_console_in, WIN_CONSOLE_INPUT_MODE))
        clog_error(CLOG_CGDB, "SetConsoleMode(input) failed: %lu",
                   (unsigned long) GetLastError());

    if (!SetConsoleMode(g_console_out, WIN_CONSOLE_OUTPUT_MODE))
        clog_error(CLOG_CGDB, "SetConsoleMode(output) failed: %lu",
                   (unsigned long) GetLastError());

    return 0;
}

void win_console_assert_input_mode(void)
{
    if (g_modes_saved)
        SetConsoleMode(g_console_in, WIN_CONSOLE_INPUT_MODE);
}

int win_console_restore(void)
{
    BOOL ok;

    if (!g_modes_saved)
        return 0;

    ok = SetConsoleMode(g_console_in, g_orig_in_mode) &&
        SetConsoleMode(g_console_out, g_orig_out_mode);

    return ok ? 0 : -1;
}

int win_console_size_changed(int *rows, int *cols)
{
    CONSOLE_SCREEN_BUFFER_INFO info;
    COORD now;

    if (rows)
        *rows = 0;
    if (cols)
        *cols = 0;

    if (g_console_out == NULL || g_console_out == INVALID_HANDLE_VALUE)
        g_console_out = GetStdHandle(STD_OUTPUT_HANDLE);
    if (g_console_out == INVALID_HANDLE_VALUE)
        return 0;

    if (!GetConsoleScreenBufferInfo(g_console_out, &info))
        return 0;

    now.Y = (SHORT) (info.srWindow.Bottom - info.srWindow.Top + 1);
    now.X = (SHORT) (info.srWindow.Right - info.srWindow.Left + 1);

    if (g_size_known && (now.X != g_last_size.X || now.Y != g_last_size.Y)) {
        g_last_size = now;
        if (rows)
            *rows = now.Y;
        if (cols)
            *cols = now.X;
        return 1;
    }

    g_last_size = now;
    g_size_known = true;
    return 0;
}

static HANDLE g_stop_event;

static BOOL WINAPI win_ctrl_handler(DWORD evt)
{
    switch (evt) {
        case CTRL_CLOSE_EVENT:
        case CTRL_LOGOFF_EVENT:
        case CTRL_SHUTDOWN_EVENT:
            if (g_stop_event)
                SetEvent(g_stop_event);
            /* Give the main loop a few seconds to exit cleanly
             * (endwin, restore modes); the OS terminates the process
             * when the handler returns from CLOSE anyway. */
            Sleep(3000);
            return TRUE;
        default:
            /* CTRL_C arrives as raw input (not an event) in cgdb's
             * mode; CTRL_BREAK is only ever targeted at gdb's process
             * group. */
            return FALSE;
    }
}

int win_console_event_setup(void)
{
    g_stop_event = CreateEvent(NULL, TRUE, FALSE, NULL);
    if (!g_stop_event)
        return -1;

    if (!SetConsoleCtrlHandler(win_ctrl_handler, TRUE))
        return -1;

    return 0;
}

void *win_stop_event_handle(void)
{
    return g_stop_event;
}
