#if HAVE_CONFIG_H
#include "config.h"
#endif /* HAVE_CONFIG_H */

#include "process_win.h"

#include <windows.h>
#include <io.h>
#include <process.h>

#include <cstdio>
#include <cstring>
#include <string>

#include "cgdb_clog.h"

/* Quote one argument for a CreateProcessW command line. NULL-safe:
 * the caller may pass an unset debugger path before the default
 * substitution happens. */
static std::string quote_arg(const char *arg)
{
    std::string out = "\"";

    if (arg) {
        for (const char *p = arg; *p; ++p) {
            if (*p == '\"')
                out += "\\\"";
            else
                out += *p;
        }
    }

    out += "\"";
    return out;
}

static std::wstring to_wide(const std::string &s)
{
    int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, NULL, 0);
    if (len <= 0)
        return std::wstring();
    std::wstring w((size_t) len, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], len);
    w.resize((size_t) len - 1);
    return w;
}

/* Human-readable description of the last win_spawn_debugger failure;
 * empty when the last call succeeded (or none was made). Printed by
 * cgdb_cleanup_and_exit after endwin so the user actually sees it. */
static char g_spawn_error[512] = "";

const char *win_spawn_last_error(void)
{
    return g_spawn_error;
}

/* Fill g_spawn_error; 'what' is the debugger path for spawn failures. */
static void win_spawn_set_error(const char *what, DWORD err)
{
    if (err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND) {
        snprintf(g_spawn_error, sizeof(g_spawn_error),
                 "cgdb: debugger \"%s\" not found. Put gdb.exe on PATH "
                 "or pass: cgdb -d <path-to-gdb.exe>", what ? what : "gdb");
    } else {
        snprintf(g_spawn_error, sizeof(g_spawn_error),
                 "cgdb: failed to start the debugger \"%s\" "
                 "(CreateProcess error %lu)",
                 what ? what : "?", (unsigned long) err);
    }
}

int win_spawn_debugger(char *const argv[], struct win_process *wp)
{
    SECURITY_ATTRIBUTES sa;
    HANDLE in_read = NULL, in_write = NULL;    /* we write -> gdb stdin */
    HANDLE out_read = NULL, out_write = NULL;  /* gdb stdout -> we read */
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    std::string cmdline;
    BOOL ok;
    int fd;

    g_spawn_error[0] = '\0';

    memset(wp, 0, sizeof(*wp));
    memset(&si, 0, sizeof(si));
    memset(&pi, 0, sizeof(pi));

    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = NULL;
    sa.bInheritHandle = TRUE;

    if (!CreatePipe(&in_read, &in_write, &sa, 0) ||
            !CreatePipe(&out_read, &out_write, &sa, 0)) {
        clog_error(CLOG_CGDB, "CreatePipe failed: %lu",
                   (unsigned long) GetLastError());
        snprintf(g_spawn_error, sizeof(g_spawn_error),
                 "cgdb: CreatePipe failed: %lu",
                 (unsigned long) GetLastError());
        return -1;
    }

    /* gdb --nw -q -ex ... --interpreter=mi <user args> */
    cmdline = quote_arg(argv[0]);
    cmdline += " --nw -q";
    cmdline += " -ex \"set pagination off\"";
    cmdline += " -ex \"set confirm off\"";
    cmdline += " --interpreter=mi";
    for (int i = 1; argv[i]; ++i) {
        cmdline += " ";
        cmdline += quote_arg(argv[i]);
    }

    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = in_read;
    si.hStdOutput = out_write;
    si.hStdError = out_write;    /* merge stderr into the MI stream */

    /* CREATE_NEW_PROCESS_GROUP: lets GenerateConsoleCtrlEvent target
     * only gdb (and its inferior); gdb shares our console. */
    ok = CreateProcessW(NULL, &to_wide(cmdline)[0], NULL, NULL, TRUE,
                        CREATE_NEW_PROCESS_GROUP, NULL, NULL, &si, &pi);
    if (!ok) {
        DWORD err = GetLastError();

        clog_error(CLOG_CGDB, "CreateProcessW(%s) failed: %lu",
                   cmdline.c_str(), (unsigned long) err);
        win_spawn_set_error(argv[0], err);
        CloseHandle(in_read);
        CloseHandle(in_write);
        CloseHandle(out_read);
        CloseHandle(out_write);
        return -1;
    }

    /* Gdb owns these ends now. */
    CloseHandle(in_read);
    CloseHandle(out_write);

    /* _O_BINARY: keep the byte stream pristine (no CRLF translation);
     * io.cpp reads/writes via ReadFile/WriteFile on these fds anyway. */
    fd = _open_osfhandle((intptr_t) in_write, _O_WRONLY | _O_BINARY);
    if (fd == -1) {
        clog_error(CLOG_CGDB, "_open_osfhandle(in_write) failed");
        CloseHandle(in_write);
        CloseHandle(out_read);
        CloseHandle(pi.hProcess);
        CloseHandle(pi.hThread);
        return -1;
    }
    wp->stdin_fd = fd;

    fd = _open_osfhandle((intptr_t) out_read, _O_RDONLY | _O_BINARY);
    if (fd == -1) {
        clog_error(CLOG_CGDB, "_open_osfhandle(out_read) failed");
        CloseHandle(in_write);
        CloseHandle(out_read);
        CloseHandle(pi.hProcess);
        CloseHandle(pi.hThread);
        return -1;
    }
    wp->stdout_fd = fd;

    wp->pid = (unsigned long) pi.dwProcessId;
    wp->h_process = pi.hProcess;
    CloseHandle(pi.hThread);

    return 0;
}

int win_interrupt_debugger(const struct win_process *wp)
{
    if (!wp || !wp->pid)
        return -1;

    if (!GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, (DWORD) wp->pid)) {
        clog_error(CLOG_CGDB, "GenerateConsoleCtrlEvent failed: %lu",
                   (unsigned long) GetLastError());
        return -1;
    }

    return 0;
}

int win_process_alive(const struct win_process *wp)
{
    DWORD code = 0;

    if (!wp || !wp->h_process)
        return 0;

    if (!GetExitCodeProcess((HANDLE) wp->h_process, &code))
        return 0;

    return code == STILL_ACTIVE;
}

void win_process_destroy(struct win_process *wp)
{
    if (wp->stdin_fd >= 0) {
        close(wp->stdin_fd);
        wp->stdin_fd = -1;
    }
    if (wp->stdout_fd >= 0) {
        close(wp->stdout_fd);
        wp->stdout_fd = -1;
    }
    if (wp->h_process) {
        CloseHandle((HANDLE) wp->h_process);
        wp->h_process = NULL;
    }
    wp->pid = 0;
}
