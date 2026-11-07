#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static int bind_tree(const char *source, const char *root, const char *name)
{
    char target[PATH_MAX];
    if (snprintf(target, sizeof(target), "%s/%s", root, name) >=
            (int)sizeof(target)) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return mount(source, target, NULL, MS_BIND | MS_REC, NULL);
}

int main(int argc, char **argv)
{
    char root[PATH_MAX];
    char old_root[PATH_MAX];
    struct stat root_stat;
    const char *trees[] = { "/dev", "/proc", "/sys", "/run" };

    if (argc < 3) {
        fprintf(stderr, "usage: %s ROOT COMMAND [ARG ...]\n", argv[0]);
        return 2;
    }
    if (!realpath(argv[1], root) || !strcmp(root, "/") ||
            stat(root, &root_stat) < 0 || !S_ISDIR(root_stat.st_mode)) {
        fprintf(stderr, "invalid staging root: %s\n", strerror(errno));
        return 2;
    }
    if (geteuid() != 0) {
        fputs("pivot-root harness requires root\n", stderr);
        return 1;
    }
    if (unshare(CLONE_NEWNS) < 0 ||
            mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) < 0 ||
            mount(root, root, NULL, MS_BIND | MS_REC, NULL) < 0 ||
            /* /data is noexec in the running firmware. This remount changes
             * only the private bind mount used as the disposable root. */
            mount(NULL, root, NULL, MS_BIND | MS_REMOUNT, NULL) < 0) {
        fprintf(stderr, "prepare private mount namespace: %s\n",
            strerror(errno));
        return 1;
    }
    for (size_t i = 0; i < sizeof(trees) / sizeof(trees[0]); i++) {
        if (bind_tree(trees[i], root, trees[i] + 1) < 0) {
            fprintf(stderr, "bind %s: %s\n", trees[i], strerror(errno));
            return 1;
        }
    }
    if (snprintf(old_root, sizeof(old_root), "%s/.brick-old-root", root) >=
            (int)sizeof(old_root) ||
            (mkdir(old_root, 0700) < 0 && errno != EEXIST) ||
            syscall(SYS_pivot_root, root, old_root) < 0 || chdir("/") < 0) {
        fprintf(stderr, "pivot staging root: %s\n", strerror(errno));
        return 1;
    }
    if (umount2("/.brick-old-root", MNT_DETACH) < 0 ||
            rmdir("/.brick-old-root") < 0) {
        fprintf(stderr, "detach old root: %s\n", strerror(errno));
        return 1;
    }
    execv(argv[2], &argv[2]);
    fprintf(stderr, "exec %s: %s\n", argv[2], strerror(errno));
    return 127;
}
