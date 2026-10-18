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

/* Quote one argument for a CreateProcessW command line. */
static std::string quote_arg(const char *arg)
{
    std::string out = "\"";
    for (const char *p = arg; *p; ++p) {
        if (*p == '\"')
            out += "\\\"";
        else
            out += *p;
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
        clog_error(CLOG_CGDB, "CreateProcessW(%s) failed: %lu",
                   cmdline.c_str(), (unsigned long) GetLastError());
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

/* ---------------------------------------------------------------- inferior */

/* The inferior's tty is a duplex named pipe: gdb (on inferior start)
 * connects as the client and hands that handle to the inferior for
 * stdin/stdout/stderr. Our server handle is created overlapped so the
 * reader thread's pending ReadFile does not serialize against the main
 * thread's WriteFile calls (a synchronous handle deadlocks here: the
 * reader blocks forever waiting for output, the writer blocks forever
 * behind the pending read). */
struct inf_ctx {
    HANDLE h_pipe;             /* overlapped duplex server handle */
    HANDLE h_read_event;       /* signals read/connect completion */
    HANDLE h_write_event;      /* signals write completion */
    HANDLE h_notify_event;     /* auto-reset: data drained by cgdb */
    HANDLE h_thread;
    volatile LONG connected;
    CRITICAL_SECTION lock;
    std::string buffer;
};

static DWORD WINAPI inferior_reader(LPVOID param)
{
    struct inf_ctx *ctx = (struct inf_ctx *) param;
    char buf[4096];
    OVERLAPPED ov;
    DWORD got = 0;

    memset(&ov, 0, sizeof(ov));
    ov.hEvent = ctx->h_read_event;

    if (!ConnectNamedPipe(ctx->h_pipe, &ov)) {
        if (GetLastError() == ERROR_IO_PENDING) {
            if (!GetOverlappedResult(ctx->h_pipe, &ov, &got, TRUE)) {
                /* gdb exited before connecting; nothing to read. */
                return 0;
            }
        } else if (GetLastError() != ERROR_PIPE_CONNECTED) {
            return 0;
        }
    }
    InterlockedExchange(&ctx->connected, 1);

    for (;;) {
        BOOL ok;

        memset(&ov, 0, sizeof(ov));
        ov.hEvent = ctx->h_read_event;

        ok = ReadFile(ctx->h_pipe, buf, sizeof(buf), &got, &ov);
        if (!ok && GetLastError() == ERROR_IO_PENDING) {
            ok = GetOverlappedResult(ctx->h_pipe, &ov, &got, TRUE);
        }

        if (!ok || got == 0)
            break;      /* EOF / broken pipe (ERROR_BROKEN_PIPE = 109) */

        EnterCriticalSection(&ctx->lock);
        ctx->buffer.append(buf, got);
        LeaveCriticalSection(&ctx->lock);
        SetEvent(ctx->h_notify_event);
    }

    /* Signal one last time so the main loop notices the (possible) EOF
     * and re-checks gdb liveness. */
    SetEvent(ctx->h_notify_event);
    return 0;
}

int win_inferior_tty_create(struct win_inferior_tty *tty,
        unsigned long owner_pid)
{
    struct inf_ctx *ctx;

    memset(tty, 0, sizeof(*tty));

    ctx = new struct inf_ctx();
    InitializeCriticalSection(&ctx->lock);

    snprintf(tty->name, sizeof(tty->name), "\\\\.\\pipe\\cgdb_inf_%lu",
             owner_pid);

    ctx->h_pipe = CreateNamedPipeA(tty->name,
                                   PIPE_ACCESS_DUPLEX |
                                   FILE_FLAG_OVERLAPPED,
                                   PIPE_TYPE_BYTE | PIPE_READMODE_BYTE |
                                   PIPE_WAIT,
                                   1, 4096, 4096, 0, NULL);
    if (ctx->h_pipe == INVALID_HANDLE_VALUE) {
        clog_error(CLOG_CGDB, "CreateNamedPipe(%s) failed: %lu",
                   tty->name, (unsigned long) GetLastError());
        DeleteCriticalSection(&ctx->lock);
        delete ctx;
        return -1;
    }

    ctx->h_read_event = CreateEvent(NULL, TRUE, FALSE, NULL);
    ctx->h_write_event = CreateEvent(NULL, TRUE, FALSE, NULL);
    ctx->h_notify_event = CreateEvent(NULL, FALSE, FALSE, NULL);
    if (!ctx->h_read_event || !ctx->h_write_event ||
            !ctx->h_notify_event) {
        if (ctx->h_read_event)
            CloseHandle(ctx->h_read_event);
        if (ctx->h_write_event)
            CloseHandle(ctx->h_write_event);
        if (ctx->h_notify_event)
            CloseHandle(ctx->h_notify_event);
        CloseHandle(ctx->h_pipe);
        DeleteCriticalSection(&ctx->lock);
        delete ctx;
        return -1;
    }

    ctx->h_thread = CreateThread(NULL, 0, inferior_reader, ctx, 0, NULL);
    if (!ctx->h_thread) {
        CloseHandle(ctx->h_pipe);
        CloseHandle(ctx->h_read_event);
        CloseHandle(ctx->h_write_event);
        CloseHandle(ctx->h_notify_event);
        DeleteCriticalSection(&ctx->lock);
        delete ctx;
        return -1;
    }

    tty->h_pipe = ctx;
    tty->h_event = ctx->h_notify_event;
    tty->h_thread = ctx->h_thread;
    tty->running = 1;
    return 0;
}

void *win_inferior_tty_event(struct win_inferior_tty *tty)
{
    return tty->h_event;
}

int win_inferior_tty_connected(struct win_inferior_tty *tty)
{
    struct inf_ctx *ctx = (struct inf_ctx *) tty->h_pipe;

    if (!ctx)
        return 0;

    return ctx->connected;
}

int win_inferior_tty_drain(struct win_inferior_tty *tty, std::string *out)
{
    struct inf_ctx *ctx = (struct inf_ctx *) tty->h_pipe;
    int n = 0;

    if (!ctx)
        return 0;

    EnterCriticalSection(&ctx->lock);
    if (!ctx->buffer.empty()) {
        n = (int) ctx->buffer.size();
        if (out)
            out->append(ctx->buffer);
        ctx->buffer.clear();
    }
    LeaveCriticalSection(&ctx->lock);

    return n;
}

int win_inferior_tty_write(struct win_inferior_tty *tty,
        const char *buf, int count)
{
    struct inf_ctx *ctx = (struct inf_ctx *) tty->h_pipe;
    OVERLAPPED ov;
    DWORD wrote = 0;
    BOOL ok;

    if (!ctx || count <= 0)
        return -1;

    if (!ctx->connected)
        return 0;       /* nobody reading yet: drop the bytes */

    memset(&ov, 0, sizeof(ov));
    ov.hEvent = ctx->h_write_event;
    ResetEvent(ctx->h_write_event);

    ok = WriteFile(ctx->h_pipe, buf, (DWORD) count, &wrote, &ov);
    if (!ok && GetLastError() == ERROR_IO_PENDING) {
        /* Give up after 2s (an inferior that never reads its stdin
         * would otherwise freeze the UI thread). */
        if (WaitForSingleObject(ctx->h_write_event, 2000) ==
                WAIT_OBJECT_0) {
            ok = GetOverlappedResult(ctx->h_pipe, &ov, &wrote, FALSE);
        } else {
            CancelIo(ctx->h_pipe);
            return 0;
        }
    }

    if (!ok)
        return -1;

    return (int) wrote;
}

void win_inferior_tty_destroy(struct win_inferior_tty *tty)
{
    struct inf_ctx *ctx = (struct inf_ctx *) tty->h_pipe;

    if (!ctx)
        return;

    /* Disconnect the pipe: unblocks the reader thread's pending
     * overlapped ReadFile. */
    CancelIo(ctx->h_pipe);
    DisconnectNamedPipe(ctx->h_pipe);
    CloseHandle(ctx->h_pipe);

    if (ctx->h_thread) {
        WaitForSingleObject(ctx->h_thread, 2000);
        CloseHandle(ctx->h_thread);
    }
    if (ctx->h_read_event)
        CloseHandle(ctx->h_read_event);
    if (ctx->h_write_event)
        CloseHandle(ctx->h_write_event);
    if (ctx->h_notify_event)
        CloseHandle(ctx->h_notify_event);

    DeleteCriticalSection(&ctx->lock);
    delete ctx;

    memset(tty, 0, sizeof(*tty));
}
