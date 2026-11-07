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

#define AUTHORITY_FD 3
#define DEFAULT_CHECK_INTERVAL_MS 3000
#define DEFAULT_SESSION_TIMEOUT_MS (5 * 60 * 1000)

static volatile sig_atomic_t stopping;

static void stop_handler(int signal_number)
{
    (void)signal_number;
    stopping = 1;
}

static unsigned long long monotonic_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
        return 0;
    return (unsigned long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static unsigned long long duration_from_environment(const char *name,
    unsigned long long fallback, unsigned long long minimum,
    unsigned long long maximum)
{
    const char *value = getenv(name);
    if (!value || !*value)
        return fallback;
    char *end = NULL;
    errno = 0;
    unsigned long long duration = strtoull(value, &end, 10);
    if (errno || !end || *end || duration < minimum || duration > maximum)
        return fallback;
    return duration;
}

static int read_field(const char *path, const char *name, char *value,
    size_t value_size)
{
    FILE *stream = fopen(path, "re");
    if (!stream)
        return -1;
    char *line = NULL;
    size_t capacity = 0;
    int found = -1;
    size_t name_length = strlen(name);
    while (getline(&line, &capacity, stream) >= 0) {
        if (strncmp(line, name, name_length) || line[name_length] != '=')
            continue;
        char *start = line + name_length + 1;
        start[strcspn(start, "\r\n")] = '\0';
        if (strlen(start) < value_size) {
            strcpy(value, start);
            found = 0;
        }
        break;
    }
    free(line);
    fclose(stream);
    return found;
}

static int current_network_matches(void)
{
    const char *expected = getenv("BRICK_CAPTIVE_WPA_NETWORK_ID");
    const char *wpa_cli = getenv("WIFI_WPA_CLI");
    const char *control = getenv("WIFI_CTRL_DIR");
    if (!wpa_cli)
        wpa_cli = "wpa_cli";
    if (!control)
        control = "/run/wpa_supplicant";
    if (!expected || !*expected || strlen(expected) > 10)
        return 0;
    for (const char *cursor = expected; *cursor; cursor++) {
        if (*cursor < '0' || *cursor > '9')
            return 0;
    }

    int output[2];
    if (pipe2(output, O_CLOEXEC) < 0)
        return 0;
    pid_t child = fork();
    if (child < 0) {
        close(output[0]);
        close(output[1]);
        return 0;
    }
    if (!child) {
        close(output[0]);
        if (dup2(output[1], STDOUT_FILENO) < 0)
            _exit(126);
        close(output[1]);
        int null_fd = open("/dev/null", O_WRONLY | O_CLOEXEC);
        if (null_fd >= 0)
            dup2(null_fd, STDERR_FILENO);
        execlp(wpa_cli, wpa_cli, "-i", "wlan0", "-p", control, "status",
            (char *)NULL);
        _exit(127);
    }
    close(output[1]);
    char status_text[4096];
    size_t used = 0;
    while (used < sizeof(status_text) - 1) {
        ssize_t count = read(output[0], status_text + used,
            sizeof(status_text) - used - 1);
        if (count > 0) {
            used += (size_t)count;
            continue;
        }
        if (count < 0 && errno == EINTR)
            continue;
        break;
    }
    close(output[0]);
    status_text[used] = '\0';
    int child_status;
    while (waitpid(child, &child_status, 0) < 0) {
        if (errno != EINTR)
            return 0;
    }
    if (!WIFEXITED(child_status) || WEXITSTATUS(child_status))
        return 0;

    int id_matches = 0;
    int completed = 0;
    char *save = NULL;
    for (char *line = strtok_r(status_text, "\r\n", &save); line;
         line = strtok_r(NULL, "\r\n", &save)) {
        if (!strncmp(line, "id=", 3) && !strcmp(line + 3, expected))
            id_matches = 1;
        if (!strcmp(line, "wpa_state=COMPLETED"))
            completed = 1;
    }
    return id_matches && completed;
}

static int connectivity_online(void)
{
    const char *state_root = getenv("WIFI_STATE_DIR");
    const char *checker = getenv("BRICK_CONNECTIVITY_CHECKER");
    if (!state_root)
        state_root = "/run/brick-wifi";
    if (!checker)
        checker = "/usr/libexec/brick-wifi-connectivity";
    char dhcp_path[512], state_path[512], capport[256] = "";
    if (snprintf(dhcp_path, sizeof(dhcp_path), "%s/dhcp", state_root) >=
            (int)sizeof(dhcp_path) ||
        snprintf(state_path, sizeof(state_path), "%s/connectivity", state_root) >=
            (int)sizeof(state_path))
        return 0;
    (void)read_field(dhcp_path, "capport_uri", capport, sizeof(capport));

    pid_t child = fork();
    if (child < 0)
        return 0;
    if (!child) {
        int null_fd = open("/dev/null", O_WRONLY | O_CLOEXEC);
        if (null_fd >= 0) {
            dup2(null_fd, STDOUT_FILENO);
            dup2(null_fd, STDERR_FILENO);
        }
        if (*capport)
            execl(checker, checker, "check", capport, (char *)NULL);
        else
            execl(checker, checker, "check", (char *)NULL);
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR)
            return 0;
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status))
        return 0;
    char state[32];
    return !read_field(state_path, "state", state, sizeof(state)) &&
        !strcmp(state, "online");
}

static int append_authority_fd(char ***arguments_out, int argc, char **argv)
{
    char **arguments = calloc((size_t)argc + 3, sizeof(*arguments));
    if (!arguments)
        return -1;
    for (int i = 1; i < argc; i++)
        arguments[i - 1] = argv[i];
    arguments[argc - 1] = "--authority-fd";
    arguments[argc] = "3";
    *arguments_out = arguments;
    return 0;
}

static int wait_for_child_exit(pid_t child, unsigned int milliseconds,
    int *status)
{
    for (unsigned int elapsed = 0; elapsed < milliseconds; elapsed += 10) {
        pid_t result = waitpid(child, status, WNOHANG);
        if (result == child)
            return 1;
        if (result < 0 && errno != EINTR)
            return -1;
        struct timespec delay = { .tv_nsec = 10 * 1000 * 1000 };
        while (nanosleep(&delay, &delay) < 0 && errno == EINTR)
            ;
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fputs("Usage: brick-captive-authority BROWSER [ARG ...]\n", stderr);
        return 2;
    }
    char **arguments;
    if (append_authority_fd(&arguments, argc, argv) < 0)
        return 1;
    int sockets[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) < 0) {
        free(arguments);
        return 1;
    }
    struct sigaction action = { .sa_handler = stop_handler };
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGHUP, &action, NULL);
    signal(SIGPIPE, SIG_IGN);

    pid_t child = fork();
    if (child < 0)
        return 1;
    if (!child) {
        close(sockets[0]);
        if (dup2(sockets[1], AUTHORITY_FD) < 0)
            _exit(126);
        close(sockets[1]);
        execvp(arguments[0], arguments);
        _exit(127);
    }
    free(arguments);
    close(sockets[1]);

    unsigned long long started = monotonic_ms();
    unsigned long long check_interval = duration_from_environment(
        "BRICK_AUTHORITY_CHECK_INTERVAL_MS", DEFAULT_CHECK_INTERVAL_MS,
        100, 60000);
    unsigned long long session_timeout = duration_from_environment(
        "BRICK_AUTHORITY_SESSION_TIMEOUT_MS", DEFAULT_SESSION_TIMEOUT_MS,
        1000, 10 * 60 * 1000);
    unsigned long long next_check = started;
    int browser_ready = 0;
    int authorized = 0;
    char buffer[4096];
    size_t used = 0;
    while (!stopping) {
        int status;
        pid_t result = waitpid(child, &status, WNOHANG);
        if (result == child) {
            close(sockets[0]);
            if (!WIFEXITED(status))
                return 128 + WTERMSIG(status);
            return WEXITSTATUS(status);
        }
        unsigned long long now = monotonic_ms();
        if (now - started >= session_timeout)
            break;
        if (browser_ready && !authorized && now >= next_check) {
            if (current_network_matches() && connectivity_online() &&
                current_network_matches()) {
                if (write(sockets[0], "SUCCESS\n", 8) != 8)
                    break;
                authorized = 1;
            } else
                next_check = monotonic_ms() + check_interval;
        }
        struct pollfd descriptor = { sockets[0], POLLIN, 0 };
        int timeout = browser_ready ? 250 : 1000;
        result = poll(&descriptor, 1, timeout);
        if (result < 0 && errno != EINTR)
            break;
        if (result <= 0)
            continue;
        ssize_t count = read(sockets[0], buffer + used,
            sizeof(buffer) - used - 1);
        if (count <= 0) {
            if (authorized) {
                int status;
                int exited = wait_for_child_exit(child, 1000, &status);
                if (exited > 0) {
                    close(sockets[0]);
                    return WIFEXITED(status) ? WEXITSTATUS(status) :
                        128 + WTERMSIG(status);
                }
            }
            break;
        }
        used += (size_t)count;
        buffer[used] = '\0';
        char *line = buffer;
        char *newline;
        while ((newline = strchr(line, '\n'))) {
            *newline = '\0';
            if (!strcmp(line, "FRAME") || !strncmp(line, "LOADED ", 7))
                browser_ready = 1;
            if (!strncmp(line, "CLOSED ", 7))
                stopping = 1;
            line = newline + 1;
        }
        used -= (size_t)(line - buffer);
        memmove(buffer, line, used);
        if (used == sizeof(buffer) - 1)
            break;
    }
    if (authorized) {
        /* CLOSED is sent before the UI releases its exclusive controller and
         * tears down the WPE view.  Let that already-authorized child exit
         * naturally; killing it here made controller-enabled success race
         * into a false failure. */
        int status;
        int exited = wait_for_child_exit(child, 1000, &status);
        if (exited > 0)
            return WIFEXITED(status) ? WEXITSTATUS(status) :
                128 + WTERMSIG(status);
    } else {
        ssize_t ignored = write(sockets[0], "CANCEL\n", 7);
        (void)ignored;
    }
    close(sockets[0]);
    kill(child, SIGTERM);
    int status;
    while (waitpid(child, &status, 0) < 0 && errno == EINTR)
        ;
    return authorized && WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
