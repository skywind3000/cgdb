#ifndef __PROCESS_WIN_H__
#define __PROCESS_WIN_H__

/* Win32 process glue for the Windows port. Replaces pseudo.cpp/fork_util.cpp
 * (pty + fork) with: gdb over anonymous pipes + the inferior in its own
 * console window (gdb "new-console on" - the Visual Studio convention,
 * adopted from the seer-windows port, see doc/win_specs.md section 4):
 *
 *  - gdb runs "path --nw -q -ex "set pagination off"
 *    -ex "set confirm off" --interpreter=mi <args>" with stdin/stdout/stderr
 *    bound to two anonymous pipes (stderr merged into the stdout pipe);
 *  - gdb shares cgdb's console and is created with
 *    CREATE_NEW_PROCESS_GROUP so GenerateConsoleCtrlEvent(CTRL_BREAK,
 *    gdb_pid) interrupts a running inferior (gdb injects DbgBreakPoint
 *    and reports *stopped) - PoC-verified to keep working when the
 *    inferior owns a separate console;
 *  - the inferior is NOT wired to cgdb: tgdb queues
 *    "-gdb-set new-console on" at startup, so console-subsystem
 *    inferiors get their own visible console window (stdin/stdout/
 *    ANSI colors/Ctrl-C handled natively by conhost) and GUI-subsystem
 *    inferiors get none. The MI stream stays clean (PoC on gdb 17.1).
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

/* Human-readable description of the last win_spawn_debugger failure
 * (empty string when the last call succeeded or none was made).
 * cgdb_cleanup_and_exit prints this after endwin so the user sees the
 * actual reason (e.g. "gdb not found") instead of only the log hint. */
const char *win_spawn_last_error(void);

/* Send a CTRL_BREAK_EVENT to the debugger's process group: interrupts a
 * running inferior. Returns 0 on success, -1 on failure. */
int win_interrupt_debugger(const struct win_process *wp);

/* Returns 1 while the debugger process is still running, 0 once it has
 * terminated. */
int win_process_alive(const struct win_process *wp);

/* Close the pipe fds and the process handle. */
void win_process_destroy(struct win_process *wp);

#endif /* __PROCESS_WIN_H__ */
