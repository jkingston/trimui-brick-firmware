#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <unistd.h>

#define CAPTIVE_USER "brick-captive"
#define CAPTIVE_EXEC "/usr/libexec/brick-captive-wpe"
#define CAPTIVE_DRM_DEVICE "/dev/dri/card0"
#define CAPTIVE_DRM_FD_ENV "BRICK_CAPTIVE_DRM_FD"
#define CAPTIVE_GPU_DRM_FD_ENV "BRICK_CAPTIVE_GPU_DRM_FD"

static int prepare_input_fd(int argc, char **argv)
{
    int input_index = -1;
    for (int index = 1; index < argc; index++) {
        if (!strcmp(argv[index], "--input-fd")) {
            fputs("brick-captive-run: caller must not provide --input-fd\n",
                stderr);
            return -1;
        }
        if (strcmp(argv[index], "--input-device"))
            continue;
        if (input_index >= 0 || index + 1 >= argc) {
            fputs("brick-captive-run: invalid --input-device\n", stderr);
            return -1;
        }
        input_index = index;
        index++;
    }
    if (input_index < 0)
        return 0;
    const char *device = argv[input_index + 1];
    const char *prefix = "/dev/input/event";
    if (strncmp(device, prefix, strlen(prefix)) || !device[strlen(prefix)]) {
        fputs("brick-captive-run: invalid controller path\n", stderr);
        return -1;
    }
    for (const char *cursor = device + strlen(prefix); *cursor; cursor++) {
        if (*cursor < '0' || *cursor > '9') {
            fputs("brick-captive-run: invalid controller path\n", stderr);
            return -1;
        }
    }
    int input_fd = open(device, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (input_fd < 0) {
        perror("brick-captive-run: open controller");
        return -1;
    }
    if (fcntl(input_fd, F_SETFD,
            fcntl(input_fd, F_GETFD) & ~FD_CLOEXEC) < 0) {
        perror("brick-captive-run: prepare controller fd");
        close(input_fd);
        return -1;
    }
    char *fd_string = NULL;
    if (asprintf(&fd_string, "%d", input_fd) < 0) {
        close(input_fd);
        return -1;
    }
    argv[input_index] = (char *)"--input-fd";
    argv[input_index + 1] = fd_string;
    return 0;
}

int main(int argc, char **argv)
{
    struct passwd *account = getpwnam(CAPTIVE_USER);
    int drm_fd = -1;
    int gpu_drm_fd = -1;
    if (!account) {
        fputs("brick-captive-run: dedicated account unavailable\n", stderr);
        return 1;
    }
    if (geteuid() != 0) {
        fputs("brick-captive-run: trusted root supervisor required\n", stderr);
        return 1;
    }
    if (geteuid() == 0) {
        char drm_fd_string[32];
        drm_fd = open(CAPTIVE_DRM_DEVICE, O_RDWR | O_CLOEXEC);
        if (drm_fd < 0) {
            perror("brick-captive-run: open DRM device");
            return 1;
        }
        gpu_drm_fd = fcntl(drm_fd, F_DUPFD_CLOEXEC, 3);
        if (gpu_drm_fd < 0) {
            perror("brick-captive-run: duplicate GPU DRM fd");
            return 1;
        }
        if (fcntl(drm_fd, F_SETFD, fcntl(drm_fd, F_GETFD) & ~FD_CLOEXEC) < 0) {
            perror("brick-captive-run: prepare DRM fd");
            return 1;
        }
        if (fcntl(gpu_drm_fd, F_SETFD,
                fcntl(gpu_drm_fd, F_GETFD) & ~FD_CLOEXEC) < 0) {
            perror("brick-captive-run: prepare GPU DRM fd");
            return 1;
        }
        snprintf(drm_fd_string, sizeof(drm_fd_string), "%d", drm_fd);
        if (setenv(CAPTIVE_DRM_FD_ENV, drm_fd_string, 1) < 0) {
            perror("brick-captive-run: export DRM fd");
            return 1;
        }
        snprintf(drm_fd_string, sizeof(drm_fd_string), "%d", gpu_drm_fd);
        if (setenv(CAPTIVE_GPU_DRM_FD_ENV, drm_fd_string, 1) < 0) {
            perror("brick-captive-run: export GPU DRM fd");
            return 1;
        }
        if (prepare_input_fd(argc, argv) < 0)
            return 1;
        if (setgroups(0, NULL) < 0 ||
            setresgid(account->pw_gid, account->pw_gid, account->pw_gid) < 0 ||
            setresuid(account->pw_uid, account->pw_uid, account->pw_uid) < 0) {
            perror("brick-captive-run: drop privileges");
            return 1;
        }
    }
    if (geteuid() != account->pw_uid || getegid() != account->pw_gid) {
        fputs("brick-captive-run: wrong execution identity\n", stderr);
        return 1;
    }
    if (getgroups(0, NULL) != 0) {
        fputs("brick-captive-run: supplementary groups retained\n", stderr);
        return 1;
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0) {
        perror("brick-captive-run: no-new-privileges");
        return 1;
    }
    unsetenv("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS");
    unsetenv("WEBKIT_ENABLE_DEBUG_PERMISSIONS_IN_SANDBOX");
    unsetenv("LD_PRELOAD");
    argv[0] = (char *)CAPTIVE_EXEC;
    execv(CAPTIVE_EXEC, argv);
    fprintf(stderr, "brick-captive-run: exec: %s\n", strerror(errno));
    return 127;
}
