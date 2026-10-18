#if HAVE_CONFIG_H
#include "config.h"
#endif /* HAVE_CONFIG_H */

#if HAVE_STDIO_H
#include <stdio.h>
#endif /* HAVE_STDIO_H */

#if HAVE_STDARG_H
#include <stdarg.h>
#endif /* HAVE_STDARG_H */

#if HAVE_STDLIB_H
#include <stdlib.h>
#endif /* HAVE_STDLIB_H */

#if HAVE_ERRNO_H
#include <errno.h>
#endif /* HAVE_ERRNO_H */

#if HAVE_UNISTD_H
#include <unistd.h>
#endif /* HAVE_UNISTD_H */

#if HAVE_SYS_STAT_H
#include <sys/stat.h>
#endif

#if HAVE_STRING_H
#include <string.h>
#endif /* HAVE_STRING_H */

#ifdef HAVE_FCNTL_H
#include <fcntl.h>
#endif /* HAVE_FCNTL_H */

#if HAVE_SYS_TYPES_H
#include <sys/types.h>
#endif /* HAVE_SYS_TYPES_H */

#ifdef HAVE_SYS_TIME_H
#include <sys/time.h>
#endif

#ifdef HAVE_SYS_SELECT_H
#include <sys/select.h>
#endif /* HAVE_SYS_SELECT_H */

#if HAVE_ERRNO_H
#include <errno.h>
#endif /* HAVE_ERRNO_H */

#include "sys_util.h"
#include "cgdb_clog.h"
#include "io.h"

#ifdef _WIN32
#include <windows.h>
#include <io.h>

/* Map an fd to a waitable HANDLE. fd 0 is the console input; every
 * other fd is a pipe created by process_win via _open_osfhandle. */
static HANDLE io_fd_handle(int fd)
{
    if (fd == 0)
        return GetStdHandle(STD_INPUT_HANDLE);
    return (HANDLE) _get_osfhandle(fd);
}

/* The console input handle signals when ANY input record is queued
 * (focus, buffer-size, menu...), but ReadFile only produces data for
 * key-down records and blocks otherwise - which would hang kui's
 * timeout-based reads forever. Peek at the queue: when a key record is
 * at the head, ReadFile is safe; non-key records are consumed and
 * discarded here. */
static int io_win_console_key_pending(HANDLE h)
{
    INPUT_RECORD rec;
    DWORD count = 0;

    for (;;) {
        if (!PeekConsoleInput(h, &rec, 1, &count) || count == 0)
            return 0;

        if (rec.EventType == KEY_EVENT && rec.Event.KeyEvent.bKeyDown)
            return 1;

        /* Consume the non-key record so ReadFile cannot stall on it. */
        if (!ReadConsoleInput(h, &rec, 1, &count) || count == 0)
            return 0;
    }
}
#endif

static void process_error(void)
{
    if (errno == EINTR)
        clog_error(CLOG_CGDB, "ERRNO = EINTR");
    else if (errno == EAGAIN)
        clog_error(CLOG_CGDB, "ERRNO = EAGAIN");
    else if (errno == EIO)
        clog_error(CLOG_CGDB, "ERRNO = EIO");
    else if (errno == EISDIR)
        clog_error(CLOG_CGDB, "ERRNO = EISDIR");
    else if (errno == EBADF)
        clog_error(CLOG_CGDB, "ERRNO = EBADF");
    else if (errno == EINVAL)
        clog_error(CLOG_CGDB, "ERRNO = EINVAL");
    else if (errno == EFAULT)
        clog_error(CLOG_CGDB, "ERRNO = EFAULT");
}

int io_read_byte(char *c, int source)
{
    int ret_val = 0;

    if ((ret_val = read(source, c, 1)) == 0) {
        return -1;
    } else if (ret_val == -1) {
        clog_error(CLOG_CGDB, "I/O error");
        process_error();
        return -1;
    }

    return 0;
}

int io_write_byte(int dest, char c)
{
    if (write(dest, &c, 1) != 1)
        return -1;

    return 0;
}

int io_rw_byte(int source, int dest)
{
    char c;

    if (read(source, &c, 1) != 1) {
        clog_error(CLOG_CGDB, "I/O error");
        process_error();
        return -1;
    }

    if (write(dest, &c, 1) != 1) {
        clog_error(CLOG_CGDB, "I/O error");
        return -1;
    }

    return 0;
}

ssize_t io_read(int fd, void *buf, size_t count)
{
#ifndef _WIN32
    ssize_t amountRead;
#endif

#ifdef _WIN32
    /* ReadFile on the raw handle: the CRT read() can block forever on
     * a pipe whose handle is already signaled (observed with the MI
     * pipe after gdb goes idle), and text-mode translation would also
     * corrupt the byte stream. ERROR_BROKEN_PIPE is EOF (the writer
     * closed its end), matching the Linux EIO-to-EOF mapping. */
    {
        HANDLE h = io_fd_handle(fd);
        DWORD got = 0;

        if (h == NULL || h == INVALID_HANDLE_VALUE) {
            clog_error(CLOG_CGDB, "io_read: bad handle (fd=%d)", fd);
            return -1;
        }

        if (!ReadFile(h, buf, (DWORD) count, &got, NULL)) {
            if (GetLastError() == ERROR_BROKEN_PIPE)
                return 0;       /* EOF */
            clog_error(CLOG_CGDB, "ReadFile failed: %lu",
                       (unsigned long) GetLastError());
            return -1;
        }

        return (ssize_t) got;    /* 0 == EOF */
    }
#else
  tgdb_read:

    if ((amountRead = read(fd, buf, count)) == -1) {    /* error */
        if (errno == EINTR)
            goto tgdb_read;
        else if (errno != EIO) {
            clog_error(CLOG_CGDB, "error reading from fd");
            return -1;
        } else {
            return 0;           /* Happens on EOF for some reason */
        }

    } else if (amountRead == 0) {   /* EOF */
        return 0;
    }

    return amountRead;
#endif
}

ssize_t io_writen(int fd, const void *vptr, size_t n)
{
#ifndef _WIN32
    ssize_t nwritten;
#endif
    size_t nleft = n;
    const char *ptr = (const char *) vptr;

#ifdef _WIN32
    /* WriteFile on the raw handle; keeps \n as-is (no text mode). */
    {
        HANDLE h = io_fd_handle(fd);
        const char *p = ptr;
        DWORD wrote = 0;

        while (nleft > 0) {
            if (h == NULL || h == INVALID_HANDLE_VALUE ||
                    !WriteFile(h, p, (DWORD) nleft, &wrote, NULL) ||
                    wrote == 0)
                return -1;
            nleft -= wrote;
            p += wrote;
        }

        return (ssize_t) n;
    }
#else
    while (nleft > 0) {
        if ((nwritten = write(fd, ptr, nleft)) <= 0) {
            if (errno == EINTR)
                nwritten = 0;
            else
                return -1;
        }
        nleft -= nwritten;
        ptr += nwritten;
    }

    return (n);
#endif
}

void io_display_char(FILE * fd, char c)
{
    if (c == '\r')
        fprintf(fd, "(%s)", "\\r");
    else if (c == '\n')
        fprintf(fd, "(%s)\n", "\\n");
    else if (c == '\032')
        fprintf(fd, "(%s)", "\\032");
    else if (c == '\b')
        fprintf(fd, "(%s)", "\\b");
    else
        fprintf(fd, "(%c)", c);

    fflush(fd);
}

int io_data_ready(int fd, int ms)
{
#ifdef _WIN32
    HANDLE h = io_fd_handle(fd);

    if (h == NULL || h == INVALID_HANDLE_VALUE) {
        clog_error(CLOG_CGDB, "io_data_ready: bad handle (fd=%d)", fd);
        return -1;
    }

    if (fd == 0) {
        /* The console input handle signals when ANY input record is
         * queued - including key-up records, which never produce VT
         * bytes for ReadFile. Keep waiting (consuming the non-key
         * records along the way) until a key-down record is actually
         * queued or the timeout expires: that restores the Linux
         * select() contract "readable == real data exists", which the
         * kui relies on (a spurious 0 return while a key was expected
         * surfaces as a spurious -1 error in kuictx::findkey when no
         * map set is active, i.e. in status-bar focus, and kills the
         * main loop right after the user types ':'). */
        ULONGLONG deadline = GetTickCount64() +
                (ms < 0 ? 0 : (ULONGLONG) ms);
        int first = 1;

        for (;;) {
            DWORD wait_ms;
            DWORD ret;

            if (ms == -1) {
                wait_ms = INFINITE;
            } else {
                ULONGLONG now = GetTickCount64();
                ULONGLONG remaining = deadline > now ?
                        (deadline - now) : 0;

                /* ms == 0 must still poll the queue once (the caller
                 * uses it as a "is data ready right now" check); the
                 * deadline only ends the loop from the second pass
                 * on, after the strays were consumed. */
                if (!first && remaining == 0)
                    return 0;
                wait_ms = (DWORD) remaining;
            }
            first = 0;

            ret = WaitForSingleObject(h, wait_ms);
            if (ret == WAIT_TIMEOUT)
                return 0;
            if (ret != WAIT_OBJECT_0) {
                clog_error(CLOG_CGDB, "WaitForSingleObject failed: %lu",
                           (unsigned long) GetLastError());
                return -1;
            }

            if (io_win_console_key_pending(h))
                return 1;
        }
    }

    /* Anonymous pipe read handles are NOT reliably waitable here:
     * WaitForSingleObject returns "signaled" even while the pipe is
     * empty and the writer is alive (observed on Win10 19045, and it
     * made a blocking ReadFile hang the main loop). Poll with
     * PeekNamedPipe instead; a closed write end shows up as
     * ERROR_BROKEN_PIPE, which counts as ready so the subsequent read
     * can observe the EOF. */
    {
        ULONGLONG deadline = GetTickCount64() +
                (ms < 0 ? 0 : (ULONGLONG) ms);

        for (;;) {
            DWORD avail = 0;

            if (PeekNamedPipe(h, NULL, 0, NULL, &avail, NULL)) {
                if (avail > 0)
                    return 1;
            } else if (GetLastError() == ERROR_BROKEN_PIPE) {
                return 1;      /* EOF: let io_read see it */
            } else {
                clog_error(CLOG_CGDB, "PeekNamedPipe failed: %lu",
                           (unsigned long) GetLastError());
                return -1;
            }

            if (ms == 0)
                return 0;
            if (ms != -1 && GetTickCount64() >= deadline)
                return 0;

            Sleep(10);
        }
    }
#else
    int ret;

    for (;;) {
        fd_set readfds, exceptfds;
        struct timeval timeout;
        struct timeval *timeout_ptr = &timeout;

        FD_ZERO(&readfds);
        FD_ZERO(&exceptfds);
        FD_SET(fd, &readfds);
        FD_SET(fd, &exceptfds);

        timeout.tv_sec = ms / 1000;
        timeout.tv_usec = (ms % 1000) * 1000;

        /* Enforce blocking semantics if the user requested it */
        if (ms == -1)
            timeout_ptr = NULL;

        ret = select(fd + 1, &readfds, (fd_set *)NULL, &exceptfds, timeout_ptr);
        if (ret == -1) {
            if (errno == EINTR)
                continue;
            else {
                clog_error(CLOG_CGDB, "Errno(%d)\n", errno);
                return -1;
            }
        }

        // ready to read if at least 1 file is available
        return ret > 0;
    }
#endif
}

int io_getchar(int fd, unsigned int ms, int *key)
{
    char c;
#ifndef _WIN32
    int ret;
    int flag = 0;
#endif
    int val;

    if (!key)
        return -1;

    val = io_data_ready(fd, ms);
    if (val == -1) {
        clog_error(CLOG_CGDB, "Errno(%d)\n", errno);
        return -1;
    }

    if (val == 0)
        return 0;               /* Nothing to read. */

#ifdef _WIN32
    {
        HANDLE h = io_fd_handle(fd);
        DWORD got = 0;

        /* ReadFile on the console (in VT-input mode) returns the raw
         * escape-sequence byte stream; ReadFile on a pipe returns raw
         * MI bytes. The CRT read() would run the console input through
         * codepage translation instead, so ReadFile it is. Gate the
         * console on a real key record (see io_win_console_key_pending)
         * or ReadFile can block despite io_data_ready's verdict. */
        if (fd == 0 && !io_win_console_key_pending(h))
            return 0;           /* only non-key records were queued */

        if (h == NULL || h == INVALID_HANDLE_VALUE ||
                !ReadFile(h, &c, 1, &got, NULL) || got == 0) {
            clog_error(CLOG_CGDB, "ReadFile failed: %lu",
                       (unsigned long) GetLastError());
            return -1;
        }

        *key = (unsigned char) c;
        return 1;
    }
#else
    /* Set nonblocking */
    flag = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flag | O_NONBLOCK);

  read_again:

    /* Read a char */
    ret = read(fd, &c, 1);

    if (ret == -1 && errno == EAGAIN)
        c = 0;                  /* No data available */
    else if (ret == -1 && errno == EINTR)
        goto read_again;
    else if (ret == -1) {
        c = 0;
        clog_error(CLOG_CGDB, "Errno(%d)\n", errno);
    } else if (ret == 0) {
        c = 0;
        ret = -1;
        clog_error(CLOG_CGDB, "Read returned nothing\n");
    }

    /* Set to original state */
    fcntl(fd, F_SETFL, flag);

    if (ret == -1)
        return -1;

    *key = c;
    return 1;
#endif
}
