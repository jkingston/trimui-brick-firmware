// SPDX-License-Identifier: GPL-2.0-only
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static FILE *open_file(const char *path)
{
	FILE *file = fopen(path, "rb");

	if (!file) {
		perror(path);
		exit(2);
	}
	return file;
}

int main(int argc, char **argv)
{
	FILE *actual, *expected, *mask;
	unsigned long tolerance, checked = 0, mismatches = 0, maximum = 0;
	char *end;

	if (argc != 5) {
		fprintf(stderr, "usage: %s ACTUAL EXPECTED MASK TOLERANCE\n", argv[0]);
		return 2;
	}
	tolerance = strtoul(argv[4], &end, 10);
	if (!*argv[4] || *end || tolerance > 255) {
		fprintf(stderr, "invalid tolerance\n");
		return 2;
	}
	actual = open_file(argv[1]);
	expected = open_file(argv[2]);
	mask = open_file(argv[3]);
	for (;;) {
		int a = fgetc(actual), e = fgetc(expected), m = fgetc(mask);

		if (a == EOF || e == EOF || m == EOF) {
			if (!(a == EOF && e == EOF && m == EOF)) {
				fprintf(stderr, "fixture lengths differ\n");
				return 2;
			}
			break;
		}
		if (m) {
			unsigned difference = abs(a - e);

			checked++;
			if (difference > maximum)
				maximum = difference;
			if (difference > tolerance)
				mismatches++;
		}
	}
	if (ferror(actual) || ferror(expected) || ferror(mask)) {
		perror("read fixture");
		return 2;
	}
	printf("checked=%lu mismatches=%lu maximum_error=%lu tolerance=%lu\n",
	       checked, mismatches, maximum, tolerance);
	return mismatches ? 1 : 0;
}
