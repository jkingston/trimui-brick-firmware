#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static unsigned long long monotonic_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
        return 0;
    return (unsigned long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

int main(int argc, char **argv)
{
    int sockets[2], status;
    unsigned hold_ms = 0;
    int command = 1;
    if (argc > 3 && !strcmp(argv[1], "--hold-ms")) {
        char *end = NULL;
        unsigned long parsed = strtoul(argv[2], &end, 10);
        if (!end || *end || parsed > 60000)
            return 2;
        hold_ms = parsed;
        command = 3;
    }
    if (command >= argc || socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC,
            0, sockets) < 0)
        return 2;
    unsigned long long started = monotonic_ms();
    pid_t child = fork();
    if (child < 0)
        return 1;
    if (!child) {
        close(sockets[0]);
        if (dup2(sockets[1], 3) < 0)
            _exit(126);
        close(sockets[1]);
        execvp(argv[command], argv + command);
        _exit(127);
    }
    close(sockets[1]);
    char buffer[4096];
    size_t used = 0;
    int approved = 0;
    while (!approved) {
        struct pollfd descriptor = { sockets[0], POLLIN, 0 };
        int result = poll(&descriptor, 1, 15000);
        if (result <= 0)
            break;
        ssize_t count = read(sockets[0], buffer + used,
            sizeof(buffer) - used - 1);
        if (count <= 0)
            break;
        used += (size_t)count;
        buffer[used] = '\0';
        char *line = buffer;
        char *newline;
        while ((newline = strchr(line, '\n'))) {
            *newline = '\0';
            unsigned long long elapsed = monotonic_ms() - started;
            if (!strcmp(line, "READY"))
                printf("ready_ms=%llu\n", elapsed);
            else if (!strncmp(line, "LOADED ", 7))
                printf("loaded_ms=%llu\n", elapsed);
            else if (!strcmp(line, "FRAME")) {
                printf("first_frame_ms=%llu\n", elapsed);
                fflush(stdout);
                const char *marker = getenv("BRICK_FRAME_MARKER");
                if (marker) {
                    int marker_fd = open(marker, O_WRONLY | O_CREAT | O_TRUNC,
                        0600);
                    if (marker_fd >= 0)
                        close(marker_fd);
                }
                if (hold_ms) {
                    struct timespec delay = { hold_ms / 1000,
                        (long)(hold_ms % 1000) * 1000000 };
                    nanosleep(&delay, NULL);
                }
                if (write(sockets[0], "SUCCESS\n", 8) != 8)
                    break;
                approved = 1;
            }
            line = newline + 1;
        }
        used -= (size_t)(line - buffer);
        memmove(buffer, line, used);
    }
    if (!approved)
        kill(child, SIGTERM);
    close(sockets[0]);
    if (waitpid(child, &status, 0) < 0)
        return 1;
    if (!WIFEXITED(status))
        return 128 + WTERMSIG(status);
    return WEXITSTATUS(status);
}
