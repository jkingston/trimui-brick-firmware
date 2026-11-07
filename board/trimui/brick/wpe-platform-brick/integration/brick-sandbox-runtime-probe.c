#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <seccomp.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

static int print_namespace(const char *name)
{
    char path[64];
    char target[128];
    ssize_t length;

    if (snprintf(path, sizeof(path), "/proc/self/ns/%s", name) >= (int)sizeof(path))
        return -1;
    length = readlink(path, target, sizeof(target) - 1);
    if (length < 0)
        return -1;
    target[length] = '\0';
    printf("namespace_%s=%s\n", name, target);
    return 0;
}

static int require_open_denied(const char *path)
{
    int fd;
    int saved_errno;

    errno = 0;
    fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    saved_errno = errno;
    if (fd >= 0) {
        close(fd);
        fprintf(stderr, "DENIAL_FAIL path=%s opened\n", path);
        return -1;
    }
    if (saved_errno != ENOENT && saved_errno != EACCES && saved_errno != EPERM &&
        saved_errno != ENXIO) {
        fprintf(stderr, "DENIAL_FAIL path=%s errno=%d (%s)\n",
                path, saved_errno, strerror(saved_errno));
        return -1;
    }
    printf("denied=%s errno=%d\n", path, saved_errno);
    return 0;
}

static int verify_libseccomp(void)
{
    scmp_filter_ctx context;
    long result;

    context = seccomp_init(SCMP_ACT_ALLOW);
    if (!context) {
        fprintf(stderr, "seccomp_init failed\n");
        return -1;
    }
    if (seccomp_rule_add(context, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(getppid), 0) < 0 ||
        seccomp_load(context) < 0) {
        fprintf(stderr, "seccomp rule/load failed\n");
        seccomp_release(context);
        return -1;
    }
    seccomp_release(context);
    errno = 0;
    result = syscall(SYS_getppid);
    if (result != -1 || errno != EPERM) {
        fprintf(stderr, "libseccomp filter ineffective result=%ld errno=%d\n",
                result, errno);
        return -1;
    }
    printf("libseccomp_getppid=denied errno=%d\n", errno);
    return 0;
}

int main(void)
{
    static const char *const namespaces[] = { "user", "mnt", "pid", "net", "ipc", "uts" };
    static const char *const denied_paths[] = {
        "/data/wifi/wpa_supplicant.conf",
        "/dev/fb0",
        "/dev/dri/card0",
        "/dev/input/event0",
    };
    size_t i;
    int failures = 0;

    printf("uid=%ld gid=%ld\n", (long)getuid(), (long)getgid());
    for (i = 0; i < sizeof(namespaces) / sizeof(namespaces[0]); i++) {
        if (print_namespace(namespaces[i]) < 0) {
            perror("read namespace");
            failures++;
        }
    }
    for (i = 0; i < sizeof(denied_paths) / sizeof(denied_paths[0]); i++) {
        if (require_open_denied(denied_paths[i]) < 0)
            failures++;
    }
    if (verify_libseccomp() < 0)
        failures++;
    printf("sandbox_runtime_probe=%s\n", failures ? "FAIL" : "PASS");
    return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
