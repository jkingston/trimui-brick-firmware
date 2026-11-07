/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static uint64_t ns(void)
{
	struct timespec value;
	clock_gettime(CLOCK_MONOTONIC, &value);
	return (uint64_t)value.tv_sec * 1000000000ULL + value.tv_nsec;
}

int main(int argc, char **argv)
{
	const size_t bytes = argc > 1 ? strtoull(argv[1], NULL, 0) : 1024 * 768 * 4;
	const unsigned iterations = argc > 2 ? strtoul(argv[2], NULL, 0) : 1000;
	unsigned char *source, *destination;
	uint64_t start, finish;
	volatile unsigned char checksum;

	if (!bytes || !iterations || posix_memalign((void **)&source, 64, bytes) ||
		posix_memalign((void **)&destination, 64, bytes))
		return fprintf(stderr, "allocation/argument failure\n"), 1;
	memset(source, 0xa5, bytes);
	memset(destination, 0, bytes);
	start = ns();
	for (unsigned i = 0; i < iterations; i++) {
		memcpy(destination, source, bytes);
		/* Alternate buffers so every iteration is observable and exercises
		 * both read and write traffic without a synthetic checksum loop. */
		unsigned char *swap = source;
		source = destination;
		destination = swap;
	}
	finish = ns();
	checksum = source[0];
	printf("bytes=%zu iterations=%u seconds=%.6f payload_MiB_s=%.1f "
	       "frame_ms=%.3f checksum=%02x\n",
		bytes, iterations, (finish - start) / 1e9,
		(double)bytes * iterations / (finish - start) * 1e9 / (1024 * 1024),
		(double)(finish - start) / iterations / 1e6, checksum);
	free(source);
	free(destination);
	return 0;
}
