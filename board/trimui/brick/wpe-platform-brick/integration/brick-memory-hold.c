#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <time.h>

int main(int argc, char **argv)
{
    char *end = NULL;
    if (argc != 3)
        return 2;
    unsigned long mib = strtoul(argv[1], &end, 10);
    if (!end || *end || mib < 1 || mib > 256)
        return 2;
    end = NULL;
    unsigned long seconds = strtoul(argv[2], &end, 10);
    if (!end || *end || seconds < 1 || seconds > 60)
        return 2;
    size_t length = (size_t)mib * 1024 * 1024;
    volatile uint8_t *memory = mmap(NULL, length, PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (memory == MAP_FAILED) {
        perror("mmap");
        return 1;
    }
    for (size_t offset = 0; offset < length; offset += 4096)
        memory[offset] = (uint8_t)offset;
    printf("held_bytes=%zu\n", length);
    fflush(stdout);
    struct timespec delay = { (time_t)seconds, 0 };
    while (nanosleep(&delay, &delay) < 0 && errno == EINTR)
        ;
    return 0;
}
