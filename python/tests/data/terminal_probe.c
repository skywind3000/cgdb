/* The terminal-behaviour probe inferior, shared by the POSIX integration
 * and e2e suites. It emits the exact sequences the /usr/bin/cgdb 0.8.0
 * probe was driven with during the alignment experiments, so the test
 * assertions are the ones that passed against real cgdb:
 *   - a colour run (SGR 31)
 *   - a full-screen clear + absolute cursor positioning
 *   - a stdin round trip (the pty's line discipline echoes the input)
 *   - a SIGWINCH report while pausing (the resize chain's end point)
 * It ends in pause() so the harness can interrupt it with VINTR (0x03).
 */
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>

static volatile sig_atomic_t winch = 0;

static void on_winch(int sig)
{
    (void) sig;
    winch = 1;
}

int main(void)
{
    char buf[128];

    printf("TTY:%s\n", ttyname(0) ? ttyname(0) : "?");
    printf("\033[31mRED\033[0m normal\n");
    fflush(stdout);
    printf("\033[2J\033[H");           /* clear screen, cursor home */
    printf("\033[3;10H\033[31mSCREEN-MODE\033[0m\n");
    printf("TYPE>");
    fflush(stdout);
    if (fgets(buf, sizeof buf, stdin)) {
        printf("GOT:%s", buf);
        fflush(stdout);
    }
    signal(SIGWINCH, on_winch);
    printf("PAUSING\n");
    fflush(stdout);
    for (;;) {
        pause();
        if (winch) {
            printf("WINCH-SEEN\n");
            fflush(stdout);
            winch = 0;
        }
    }
    return 0;
}
