#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <xf86drm.h>
#include <xf86drmMode.h>

#define DEVICE_PATH "/dev/dri/card0"
#define SHELL_INIT "/etc/init.d/S20brick-shell"
#define SHELL_PIDFILE "/run/brick-shell.pid"
#define MAX_CRTC_CONNECTORS 16
#define DRM_MASTER_RETRY_INTERVAL_MS 20
#define DRM_MASTER_RETRY_TIMEOUT_MS 3000

struct saved_display {
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t x;
    uint32_t y;
    drmModeModeInfo mode;
    uint32_t connectors[MAX_CRTC_CONNECTORS];
    int connector_count;
};

static volatile sig_atomic_t stop_requested;
static volatile sig_atomic_t child_pid_for_signal;

static void on_signal(int signo)
{
    (void)signo;
    stop_requested = 1;
    if (child_pid_for_signal > 1)
        kill(-(pid_t)child_pid_for_signal, SIGTERM);
}

static int install_signal_handlers(void)
{
    const int signals[] = { SIGINT, SIGTERM, SIGHUP };
    struct sigaction action;
    size_t i;

    memset(&action, 0, sizeof(action));
    action.sa_handler = on_signal;
    sigemptyset(&action.sa_mask);
    for (i = 0; i < sizeof(signals) / sizeof(signals[0]); i++) {
        if (sigaction(signals[i], &action, NULL) < 0)
            return -1;
    }
    return 0;
}

static int read_pidfile(pid_t *pid)
{
    FILE *file;
    long value;

    file = fopen(SHELL_PIDFILE, "r");
    if (!file)
        return -1;
    if (fscanf(file, "%ld", &value) != 1 || value <= 1 || value > INT32_MAX) {
        fclose(file);
        errno = EINVAL;
        return -1;
    }
    fclose(file);
    *pid = (pid_t)value;
    return 0;
}

static bool shell_running(pid_t *pid)
{
    pid_t found;

    if (read_pidfile(&found) < 0)
        return false;
    if (kill(found, 0) < 0 && errno != EPERM)
        return false;
    if (pid)
        *pid = found;
    return true;
}

static int run_shell_service(const char *action)
{
    pid_t child;
    int status;

    child = fork();
    if (child < 0)
        return -1;
    if (child == 0) {
        execl(SHELL_INIT, SHELL_INIT, action, (char *)NULL);
        _exit(127);
    }
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR)
            return -1;
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        errno = ECHILD;
        return -1;
    }
    return 0;
}

static int wait_shell_running(pid_t *pid, int timeout_ms)
{
    struct timespec delay = { .tv_sec = 0, .tv_nsec = 20 * 1000 * 1000 };
    int elapsed;

    for (elapsed = 0; elapsed <= timeout_ms; elapsed += 20) {
        if (shell_running(pid))
            return 0;
        nanosleep(&delay, NULL);
    }
    errno = ETIMEDOUT;
    return -1;
}

static int save_active_display(int fd, struct saved_display *saved)
{
    drmModeRes *resources = NULL;
    drmModeConnector *connector = NULL;
    drmModeEncoder *encoder = NULL;
    drmModeCrtc *crtc = NULL;
    uint32_t target_crtc = 0;
    int i;
    int rc = -1;

    resources = drmModeGetResources(fd);
    if (!resources)
        goto out;
    for (i = 0; i < resources->count_connectors; i++) {
        connector = drmModeGetConnector(fd, resources->connectors[i]);
        if (!connector)
            continue;
        if (connector->connection == DRM_MODE_CONNECTED && connector->encoder_id) {
            encoder = drmModeGetEncoder(fd, connector->encoder_id);
            if (encoder && encoder->crtc_id) {
                target_crtc = encoder->crtc_id;
                drmModeFreeEncoder(encoder);
                encoder = NULL;
                drmModeFreeConnector(connector);
                connector = NULL;
                break;
            }
            drmModeFreeEncoder(encoder);
            encoder = NULL;
        }
        drmModeFreeConnector(connector);
        connector = NULL;
    }
    if (!target_crtc) {
        errno = ENODEV;
        goto out;
    }

    crtc = drmModeGetCrtc(fd, target_crtc);
    if (!crtc)
        goto out;
    if (!crtc->mode_valid || !crtc->buffer_id) {
        errno = ENOTSUP;
        goto out;
    }

    memset(saved, 0, sizeof(*saved));
    saved->crtc_id = crtc->crtc_id;
    saved->fb_id = crtc->buffer_id;
    saved->x = crtc->x;
    saved->y = crtc->y;
    saved->mode = crtc->mode;

    for (i = 0; i < resources->count_connectors; i++) {
        connector = drmModeGetConnector(fd, resources->connectors[i]);
        if (!connector)
            continue;
        if (connector->encoder_id) {
            encoder = drmModeGetEncoder(fd, connector->encoder_id);
            if (encoder && encoder->crtc_id == target_crtc) {
                if (saved->connector_count >= MAX_CRTC_CONNECTORS) {
                    errno = E2BIG;
                    goto out;
                }
                saved->connectors[saved->connector_count++] = connector->connector_id;
            }
            drmModeFreeEncoder(encoder);
            encoder = NULL;
        }
        drmModeFreeConnector(connector);
        connector = NULL;
    }
    if (!saved->connector_count) {
        errno = ENODEV;
        goto out;
    }
    rc = 0;
out:
    drmModeFreeCrtc(crtc);
    drmModeFreeEncoder(encoder);
    drmModeFreeConnector(connector);
    drmModeFreeResources(resources);
    return rc;
}

static int monotonic_ms(uint64_t *value)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
        return -1;
    *value = (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
    return 0;
}

static int recover_display_and_shell(int fd, struct saved_display *saved,
                                     bool restart_shell)
{
    struct timespec retry_delay = {
        .tv_sec = 0,
        .tv_nsec = DRM_MASTER_RETRY_INTERVAL_MS * 1000 * 1000,
    };
    uint64_t retry_started_ms = 0;
    uint64_t retry_now_ms = 0;
    pid_t shell_pid;
    bool acquired_master = false;
    unsigned int master_attempts = 0;
    int master_error = 0;
    int rc = -1;

    /* A sandbox launcher can outlive its killed presenter for a few scheduler
     * ticks while it tears down nested processes and closes their DRM fds.
     * Treat that bounded EBUSY window as cleanup latency, not a failed
     * recovery. Other errors remain fatal immediately. */
    if (monotonic_ms(&retry_started_ms) < 0)
        retry_started_ms = 0;
    for (;;) {
        master_attempts++;
        if (drmSetMaster(fd) == 0) {
            acquired_master = true;
            break;
        }
        master_error = errno;
        if (master_error != EBUSY || !retry_started_ms ||
            monotonic_ms(&retry_now_ms) < 0 ||
            retry_now_ms - retry_started_ms >= DRM_MASTER_RETRY_TIMEOUT_MS)
            break;
        nanosleep(&retry_delay, NULL);
    }
    if (!acquired_master) {
        if (retry_started_ms && monotonic_ms(&retry_now_ms) == 0)
            fprintf(stderr,
                    "watchdog: DRM master unavailable attempts=%u elapsed=%llu ms\n",
                    master_attempts,
                    (unsigned long long)(retry_now_ms - retry_started_ms));
        errno = master_error;
        perror("watchdog acquire DRM master");
        goto shell;
    }
    if (master_attempts > 1 && monotonic_ms(&retry_now_ms) == 0)
        printf("watchdog: acquired DRM master attempts=%u elapsed=%llu ms\n",
               master_attempts,
               (unsigned long long)(retry_now_ms - retry_started_ms));
    if (drmModeSetCrtc(fd, saved->crtc_id, saved->fb_id,
                       saved->x, saved->y, saved->connectors,
                       saved->connector_count, &saved->mode) < 0) {
        perror("watchdog restore saved CRTC");
        goto shell;
    }
    printf("watchdog: restored crtc=%u fb=%u mode=%s offset=%u,%u\n",
           saved->crtc_id, saved->fb_id, saved->mode.name,
           saved->x, saved->y);
    rc = 0;

shell:
    if (acquired_master && drmDropMaster(fd) < 0)
        fprintf(stderr, "warning: watchdog drop DRM master failed: %s\n",
                strerror(errno));
    if (!restart_shell)
        return rc;
    if (!shell_running(NULL) && run_shell_service("start") < 0) {
        perror("watchdog restart brick-shell");
        rc = -1;
    }
    if (wait_shell_running(&shell_pid, 3000) < 0) {
        perror("watchdog verify brick-shell");
        rc = -1;
    } else {
        printf("watchdog: shell alive pid=%ld\n", (long)shell_pid);
    }
    return rc;
}

int main(int argc, char **argv)
{
    struct saved_display saved;
    struct timespec delay = { .tv_sec = 0, .tv_nsec = 10 * 1000 * 1000 };
    uint64_t started_ms = 0;
    uint64_t now_ms = 0;
    long kill_after_ms = 0;
    const char *child_cgroup = NULL;
    const char *pause_hook = NULL;
    const char *pause_argument = NULL;
    bool restore_only = false;
    char *end = NULL;
    int command_index = 1;
    int fd = -1;
    pid_t child;
    int status = 0;
    int wait_rc;
    bool injected_kill = false;
    bool child_done = false;
    bool recovery_needed;

    setvbuf(stdout, NULL, _IOLBF, 0);
    memset(&saved, 0, sizeof(saved));
    while (command_index < argc && strcmp(argv[command_index], "--")) {
        if (!strcmp(argv[command_index], "--restore-only")) {
            restore_only = true;
            command_index++;
            continue;
        }
        if (command_index + 1 < argc &&
            !strcmp(argv[command_index], "--child-cgroup")) {
            child_cgroup = argv[command_index + 1];
            command_index += 2;
            continue;
        }
        if (command_index + 1 < argc &&
            !strcmp(argv[command_index], "--pause-hook")) {
            pause_hook = argv[command_index + 1];
            command_index += 2;
            continue;
        }
        if (command_index + 1 < argc &&
            !strcmp(argv[command_index], "--pause-argument")) {
            pause_argument = argv[command_index + 1];
            command_index += 2;
            continue;
        }
        if (command_index + 1 < argc &&
            !strcmp(argv[command_index], "--kill-after-ms")) {
            errno = 0;
            kill_after_ms = strtol(argv[command_index + 1], &end, 10);
            if (errno || !end || *end || kill_after_ms < 1 ||
                kill_after_ms > 60000) {
                fprintf(stderr, "--kill-after-ms must be from 1 through 60000\n");
                return EXIT_FAILURE;
            }
            command_index += 2;
            continue;
        }
        fprintf(stderr, "unknown watchdog option: %s\n", argv[command_index]);
        return EXIT_FAILURE;
    }
    if ((child_cgroup || pause_hook || pause_argument) &&
        (!child_cgroup || !pause_hook || !pause_argument || !restore_only)) {
        fprintf(stderr, "production recovery options must be supplied together\n");
        return EXIT_FAILURE;
    }
    if (command_index < argc && strcmp(argv[command_index], "--") == 0)
        command_index++;
    if (command_index >= argc) {
        fprintf(stderr, "usage: %s [options] -- command [args...]\n",
                argv[0]);
        return EXIT_FAILURE;
    }
    if (install_signal_handlers() < 0) {
        perror("install signal handlers");
        return EXIT_FAILURE;
    }

    fd = open(DEVICE_PATH, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        perror("watchdog open DRM device");
        return EXIT_FAILURE;
    }
    if (save_active_display(fd, &saved) < 0) {
        perror("watchdog save display");
        close(fd);
        return EXIT_FAILURE;
    }
    printf("watchdog: saved crtc=%u fb=%u mode=%s offset=%u,%u connectors=%d\n",
           saved.crtc_id, saved.fb_id, saved.mode.name, saved.x, saved.y,
           saved.connector_count);
    if (drmDropMaster(fd) < 0 && errno != EINVAL) {
        perror("watchdog relinquish DRM master");
        close(fd);
        return EXIT_FAILURE;
    }
    if (pause_hook) {
        pid_t pause_child = fork();
        if (pause_child < 0) {
            perror("watchdog fork pause hook");
            close(fd);
            return EXIT_FAILURE;
        }
        if (!pause_child) {
            execl(pause_hook, pause_hook, pause_argument, (char *)NULL);
            _exit(127);
        }
        int pause_status;
        pid_t pause_result;
        do {
            pause_result = waitpid(pause_child, &pause_status, 0);
        } while (pause_result < 0 && errno == EINTR);
        if (pause_result < 0) {
            perror("watchdog wait pause hook");
            close(fd);
            return EXIT_FAILURE;
        }
        if (!WIFEXITED(pause_status) || WEXITSTATUS(pause_status)) {
            fprintf(stderr, "watchdog: display pause hook failed\n");
            close(fd);
            return EXIT_FAILURE;
        }
    }

    child = fork();
    if (child < 0) {
        perror("fork presenter");
        close(fd);
        return EXIT_FAILURE;
    }
    if (child == 0) {
        setpgid(0, 0);
        if (child_cgroup) {
            char path[512];
            int cgroup_fd;
            if (snprintf(path, sizeof(path), "%s/cgroup.procs", child_cgroup) >=
                (int)sizeof(path))
                _exit(126);
            cgroup_fd = open(path, O_WRONLY | O_CLOEXEC);
            if (cgroup_fd < 0 || dprintf(cgroup_fd, "%ld\n", (long)getpid()) < 0)
                _exit(126);
            close(cgroup_fd);
        }
        execvp(argv[command_index], &argv[command_index]);
        perror("exec presenter");
        _exit(127);
    }
    setpgid(child, child);
    child_pid_for_signal = child;
    if (monotonic_ms(&started_ms) < 0) {
        perror("monotonic clock");
        kill(-child, SIGKILL);
        injected_kill = true;
    }

    while (!child_done) {
        wait_rc = waitpid(child, &status, WNOHANG);
        if (wait_rc == child) {
            child_done = true;
            break;
        }
        if (wait_rc < 0 && errno != EINTR) {
            perror("wait presenter");
            break;
        }
        if (kill_after_ms && started_ms && !injected_kill &&
            monotonic_ms(&now_ms) == 0 &&
            now_ms - started_ms >= (uint64_t)kill_after_ms) {
            printf("watchdog: injecting SIGKILL into presenter group pid=%ld\n",
                   (long)child);
            kill(-child, SIGKILL);
            injected_kill = true;
        }
        nanosleep(&delay, NULL);
    }
    child_pid_for_signal = 0;
    if (!child_done) {
        kill(-child, SIGKILL);
        while (waitpid(child, &status, 0) < 0 && errno == EINTR)
            ;
    }

    recovery_needed = restore_only || stop_requested || injected_kill ||
                      !WIFEXITED(status) || WEXITSTATUS(status) != 0 ||
                      !shell_running(NULL);
    if (WIFSIGNALED(status))
        printf("watchdog: presenter signal=%d\n", WTERMSIG(status));
    else if (WIFEXITED(status))
        printf("watchdog: presenter exit=%d\n", WEXITSTATUS(status));

    if (recovery_needed) {
        if (recover_display_and_shell(fd, &saved, !restore_only) < 0) {
            printf("watchdog: recovery=FAIL\n");
            close(fd);
            return EXIT_FAILURE;
        }
        printf("watchdog: recovery=PASS\n");
    }
    close(fd);

    if (injected_kill)
        return recovery_needed ? EXIT_SUCCESS : EXIT_FAILURE;
    if (stop_requested || !WIFEXITED(status))
        return EXIT_FAILURE;
    return WEXITSTATUS(status);
}
