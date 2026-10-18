#ifndef __CONSOLE_WIN_H__
#define __CONSOLE_WIN_H__

/* Windows console glue for cgdb: raw VT-input mode, mode save/restore
 * and resize polling. Replaces the termios tty_* layer on Windows. */

/* Save the original console modes (once) and switch the console to the
 * modes cgdb needs:
 *   input:  ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_EXTENDED_FLAGS
 *           (keys arrive as xterm-style escape sequences, no line
 *           editing, no echo, no quick-edit selection, ^C arrives as
 *           the raw byte 0x03)
 *   output: ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
 *           | ENABLE_WRAP_AT_EOL_OUTPUT
 *
 * Returns 0 on success, -1 on failure (e.g. no console attached).
 */
int win_console_setup(void);

/* Re-apply the cgdb input mode without touching the saved originals.
 * Needed after PDCurses initscr(), which rewrites the input console
 * mode (verified: the VT-input bit is cleared by initscr and only
 * restored by endwin).
 */
void win_console_assert_input_mode(void);

/* Restore the console modes saved by win_console_setup(). */
int win_console_restore(void);

/* Poll the console size. When it differs from the last observed size,
 * store the new dimensions in *rows and *cols and return 1; otherwise
 * return 0. */
int win_console_size_changed(int *rows, int *cols);

/* Create the stop event and install the console ctrl handler that
 * signals it on close/logoff/shutdown. The main loop waits on the
 * handle from win_stop_event_handle() and exits cleanly when it is
 * signaled. Returns 0 on success, -1 on failure. */
int win_console_event_setup(void);

/* The waitable stop event handle (NULL before win_console_event_setup
 * or on failure). */
void *win_stop_event_handle(void);

#endif /* __CONSOLE_WIN_H__ */
