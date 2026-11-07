#define _GNU_SOURCE

#include <errno.h>
#include <grp.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <unistd.h>

static int parse_id(const char *text, unsigned long *value)
{
    char *end = NULL;

    errno = 0;
    *value = strtoul(text, &end, 10);
    if (errno || !end || *end || *value > UINT_MAX)
        return -1;
    return 0;
}

int main(int argc, char **argv)
{
    unsigned long uid;
    unsigned long gid;

    if (argc < 4) {
        fprintf(stderr, "usage: %s UID GID COMMAND [ARG...]\n", argv[0]);
        return EXIT_FAILURE;
    }
    if (geteuid() != 0) {
        fprintf(stderr, "brick-drop-exec must start as root\n");
        return EXIT_FAILURE;
    }
    if (parse_id(argv[1], &uid) < 0 || parse_id(argv[2], &gid) < 0) {
        fprintf(stderr, "invalid uid or gid\n");
        return EXIT_FAILURE;
    }
    if (setgroups(0, NULL) < 0 || setresgid((gid_t)gid, (gid_t)gid, (gid_t)gid) < 0 ||
        setresuid((uid_t)uid, (uid_t)uid, (uid_t)uid) < 0) {
        perror("drop credentials");
        return EXIT_FAILURE;
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0) {
        perror("PR_SET_NO_NEW_PRIVS");
        return EXIT_FAILURE;
    }
    execvp(argv[3], &argv[3]);
    perror("exec");
    return EXIT_FAILURE;
}
