#define _DARWIN_C_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <termios.h>
#include <unistd.h>

static volatile sig_atomic_t resized;
static volatile sig_atomic_t stopped;

static void handle_signal(int signal_number) {
    if (signal_number == SIGWINCH) {
        resized = 1;
    } else {
        stopped = signal_number;
    }
}

static void show_size(const char *instance) {
    struct winsize size = {0};
    if (ioctl(STDIN_FILENO, TIOCGWINSZ, &size) == -1) {
        return;
    }
    printf("\r\nSIZE %s %u %u %u %u\r\n", instance,
           size.ws_col, size.ws_row, size.ws_xpixel, size.ws_ypixel);
    fflush(stdout);
}

int main(int argument_count, char **arguments) {
    if (argument_count != 3 || strlen(arguments[1]) > 32 ||
        strspn(arguments[1], "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") != strlen(arguments[1]) ||
        arguments[1][0] == '\0' ||
        (strcmp(arguments[2], "0") != 0 && strcmp(arguments[2], "75") != 0)) {
        fprintf(stderr, "usage: fixture INSTANCE_ID {0|75}\n");
        return 64;
    }
    struct termios original;
    if (tcgetattr(STDIN_FILENO, &original) == -1) {
        perror("tcgetattr");
        return 70;
    }
    struct termios raw = original;
    cfmakeraw(&raw);
    if (tcsetattr(STDIN_FILENO, TCSANOW, &raw) == -1) {
        perror("tcsetattr");
        return 70;
    }
    sigset_t blocked;
    sigset_t previous;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGWINCH);
    sigaddset(&blocked, SIGTERM);
    sigaddset(&blocked, SIGHUP);
    sigaddset(&blocked, SIGINT);
    sigprocmask(SIG_BLOCK, &blocked, &previous);
    struct sigaction action = {0};
    action.sa_handler = handle_signal;
    sigemptyset(&action.sa_mask);
    sigaction(SIGWINCH, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGHUP, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    printf("\033[?1049h\033[?2004h\033[?1000h\033[?1006h\033[2J\033[H");
    printf("\033[38;2;80;180;255mZTERM2 M1 %s\033[0m\r\n", arguments[1]);
    printf("Unicode: été — 日本語 — e\314\201 — 🐚\r\n");
    printf("Input bytes are shown as hex; Ctrl-D exits with %s.\r\n", arguments[2]);
    show_size(arguments[1]);
    int exit_code = atoi(arguments[2]);
    while (!stopped) {
        if (resized) {
            resized = 0;
            show_size(arguments[1]);
        }
        fd_set readers;
        FD_ZERO(&readers);
        FD_SET(STDIN_FILENO, &readers);
        int ready = pselect(STDIN_FILENO + 1, &readers, NULL, NULL, NULL, &previous);
        if (ready == -1 && errno == EINTR) {
            continue;
        }
        if (ready == -1) {
            exit_code = 70;
            break;
        }
        unsigned char input[256];
        ssize_t length = read(STDIN_FILENO, input, sizeof(input));
        if (length <= 0) {
            break;
        }
        printf("\r\nINPUT %s ", arguments[1]);
        for (ssize_t index = 0; index < length; index++) {
            printf("%02x", input[index]);
        }
        printf("\r\n");
        fflush(stdout);
        if (memchr(input, 4, (size_t)length) != NULL) {
            break;
        }
    }
    if (stopped) {
        exit_code = 128 + stopped;
    }
    printf("\033[?1006l\033[?1000l\033[?2004l\033[?1049lEXIT %s %d\r\n", arguments[1], exit_code);
    fflush(stdout);
    tcsetattr(STDIN_FILENO, TCSANOW, &original);
    sigprocmask(SIG_SETMASK, &previous, NULL);
    return exit_code;
}
