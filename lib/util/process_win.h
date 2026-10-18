#ifndef __PROCESS_WIN_H__
#define __PROCESS_WIN_H__

#include <string>

/* Win32 process glue for the Windows port. Replaces pseudo.cpp/fork_util.cpp
 * (pty + fork) with: gdb over anonymous pipes + inferior-tty over a named
 * pipe with a reader thread. All facts below are M0-verified (see
 * doc/win_specs.md section 4):
 *
 *  - gdb runs "path --nw -q -ex "set pagination off"
 *    -ex "set confirm off" --interpreter=mi <args>" with stdin/stdout/stderr
 *    bound to two anonymous pipes (stderr merged into the stdout pipe);
 *  - gdb shares cgdb's console and is created with
 *    CREATE_NEW_PROCESS_GROUP so GenerateConsoleCtrlEvent(CTRL_BREAK,
 *    gdb_pid) interrupts a running inferior (gdb injects DbgBreakPoint
 *    and reports *stopped);
 *  - the inferior's stdio is redirected with
 *    "-gdb-set inferior-tty \\\\.\\pipe\\cgdb_inf_<pid>" (raw backslashes
 *    survive the MI parser); gdb connects as the pipe client when the
 *    inferior starts; our server side is a duplex pipe drained by a
 *    reader thread.
 */

/* State of the spawned debugger. */
struct win_process {
    int stdin_fd;        /* CRT fd: write MI commands here */
    int stdout_fd;       /* CRT fd: read MI records here */
    unsigned long pid;   /* debugger process id */
    void *h_process;     /* HANDLE to the debugger process */
};

/* Spawn the debugger (argv[0] = path to gdb, remaining entries are
 * user-supplied gdb arguments, argv is NULL terminated).
 *
 * On success returns 0 and fills *wp. The caller owns the fds and the
 * process handle; close them with win_process_destroy().
 * On failure returns -1.
 */
int win_spawn_debugger(char *const argv[], struct win_process *wp);

/* Send a CTRL_BREAK_EVENT to the debugger's process group: interrupts a
 * running inferior. Returns 0 on success, -1 on failure. */
int win_interrupt_debugger(const struct win_process *wp);

/* Returns 1 while the debugger process is still running, 0 once it has
 * terminated. */
int win_process_alive(const struct win_process *wp);

/* Close the pipe fds and the process handle. */
void win_process_destroy(struct win_process *wp);

/* State of the inferior's tty pipe. */
struct win_inferior_tty {
    char name[128];      /* \\.\pipe\cgdb_inf_<pid> */
    void *h_pipe;        /* HANDLE of the server end */
    void *h_event;       /* auto-reset event, set when data arrived */
    void *h_thread;      /* reader thread */
    int running;         /* thread alive */
    std::string buffer;  /* bytes read from the inferior (drained by main
                          * loop under the internal lock) */
};

/* Create the inferior tty pipe (server side) with its reader thread.
 * Returns 0 on success, -1 on failure. The name to pass to gdb is
 * tty->name. */
int win_inferior_tty_create(struct win_inferior_tty *tty,
        unsigned long owner_pid);

/* 1 once gdb (on behalf of the inferior) has connected to the pipe,
 * which happens when the inferior actually starts. */
int win_inferior_tty_connected(struct win_inferior_tty *tty);

/* Waitable event handle for the main loop (data available). */
void *win_inferior_tty_event(struct win_inferior_tty *tty);

/* Drain bytes the inferior has written; appends them to *out and clears
 * the internal buffer. May be called from the main loop after the event
 * was signaled; also safe to poll when the inferior is not running.
 * Returns the number of bytes appended. */
int win_inferior_tty_drain(struct win_inferior_tty *tty, std::string *out);

/* Write bytes to the inferior's stdin (when the inferior reads input
 * while gdb is busy running it). Returns count written or -1. */
int win_inferior_tty_write(struct win_inferior_tty *tty,
        const char *buf, int count);

/* Stop the reader thread and close the pipe. */
void win_inferior_tty_destroy(struct win_inferior_tty *tty);

#endif /* __PROCESS_WIN_H__ */
