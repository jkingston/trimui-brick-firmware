// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <linux/if_alg.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

static int failures;

static void fail(const char *what)
{
	fprintf(stderr, "FAIL %s: %s\n", what, strerror(errno));
	failures++;
}

static int parse_hex(const char *hex, uint8_t *out, size_t bytes)
{
	size_t i;
	unsigned int value;

	for (i = 0; i < bytes; i++) {
		if (sscanf(hex + i * 2, "%2x", &value) != 1)
			return -1;
		out[i] = value;
	}
	return hex[bytes * 2] ? -1 : 0;
}

static int alg_socket(const char *type, const char *name)
{
	struct sockaddr_alg sa = { .salg_family = AF_ALG };
	int fd;

	if (strlen(type) >= sizeof(sa.salg_type) ||
	    strlen(name) >= sizeof(sa.salg_name)) {
		errno = EINVAL;
		return -1;
	}
	strcpy((char *)sa.salg_type, type);
	strcpy((char *)sa.salg_name, name);
	fd = socket(AF_ALG, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return -1;
	if (bind(fd, (struct sockaddr *)&sa, sizeof(sa))) {
		close(fd);
		return -1;
	}
	return fd;
}

static ssize_t read_full(int fd, void *buffer, size_t bytes)
{
	size_t done = 0;

	while (done < bytes) {
		ssize_t count = read(fd, (uint8_t *)buffer + done, bytes - done);

		if (count < 0 && errno == EINTR)
			continue;
		if (count <= 0)
			return count < 0 ? -1 : (ssize_t)done;
		done += (size_t)count;
	}
	return (ssize_t)done;
}

static int cipher(const char *name, const uint8_t *key, size_t keylen,
		  const uint8_t *iv, int encrypt, const uint8_t *input,
		  uint8_t *output, size_t bytes, int fragmented)
{
	uint8_t control[CMSG_SPACE(sizeof(uint32_t)) +
		CMSG_SPACE(sizeof(struct af_alg_iv) + 16)] = { 0 };
	struct iovec iov[4];
	struct msghdr msg = { 0 };
	struct cmsghdr *cmsg;
	struct af_alg_iv *alg_iv;
	uint32_t operation = encrypt ? ALG_OP_ENCRYPT : ALG_OP_DECRYPT;
	size_t offset = 0, i;
	int tfm = -1, op = -1, ret = -1;

	tfm = alg_socket("skcipher", name);
	if (tfm < 0)
		goto out;
	if (setsockopt(tfm, SOL_ALG, ALG_SET_KEY, key, keylen))
		goto out;
	op = accept4(tfm, NULL, NULL, SOCK_CLOEXEC);
	if (op < 0)
		goto out;
	msg.msg_control = control;
	msg.msg_controllen = CMSG_SPACE(sizeof(operation));
	cmsg = CMSG_FIRSTHDR(&msg);
	cmsg->cmsg_level = SOL_ALG;
	cmsg->cmsg_type = ALG_SET_OP;
	cmsg->cmsg_len = CMSG_LEN(sizeof(operation));
	memcpy(CMSG_DATA(cmsg), &operation, sizeof(operation));
	if (iv) {
		msg.msg_controllen += CMSG_SPACE(sizeof(*alg_iv) + 16);
		cmsg = CMSG_NXTHDR(&msg, cmsg);
		cmsg->cmsg_level = SOL_ALG;
		cmsg->cmsg_type = ALG_SET_IV;
		cmsg->cmsg_len = CMSG_LEN(sizeof(*alg_iv) + 16);
		alg_iv = (struct af_alg_iv *)CMSG_DATA(cmsg);
		alg_iv->ivlen = 16;
		memcpy(alg_iv->iv, iv, 16);
	}
	msg.msg_iov = iov;
	msg.msg_iovlen = fragmented ? ARRAY_SIZE(iov) : 1;
	for (i = 0; i < msg.msg_iovlen; i++) {
		size_t part = (i + 1 == msg.msg_iovlen) ? bytes - offset :
			bytes / msg.msg_iovlen;

		part &= ~(size_t)15;
		if (i + 1 == msg.msg_iovlen)
			part = bytes - offset;
		iov[i].iov_base = (void *)input + offset;
		iov[i].iov_len = part;
		offset += part;
	}
	if (sendmsg(op, &msg, 0) != (ssize_t)bytes)
		goto out;
	if (read_full(op, output, bytes) != (ssize_t)bytes)
		goto out;
	ret = 0;
out:
	if (op >= 0)
		close(op);
	if (tfm >= 0)
		close(tfm);
	return ret;
}

static int hash(const uint8_t *input, size_t bytes, uint8_t output[32],
		int streaming)
{
	struct iovec iov[4];
	struct msghdr msg = { .msg_iov = iov };
	size_t offset = 0, i;
	int tfm = -1, op = -1, ret = -1;

	tfm = alg_socket("hash", "sha256-a133-ce");
	if (tfm < 0)
		goto out;
	op = accept4(tfm, NULL, NULL, SOCK_CLOEXEC);
	if (op < 0)
		goto out;
	if (bytes) {
		msg.msg_iovlen = streaming ? ARRAY_SIZE(iov) : 1;
		for (i = 0; i < msg.msg_iovlen; i++) {
			size_t part = (i + 1 == msg.msg_iovlen) ? bytes - offset :
				bytes / msg.msg_iovlen;
			iov[i].iov_base = (void *)input + offset;
			iov[i].iov_len = part;
			offset += part;
		}
		if (streaming) {
			for (i = 0; i < msg.msg_iovlen; i++) {
				if (send(op, iov[i].iov_base, iov[i].iov_len,
					 i + 1 == msg.msg_iovlen ? 0 : MSG_MORE) !=
				    (ssize_t)iov[i].iov_len)
					goto out;
			}
		} else if (sendmsg(op, &msg, 0) != (ssize_t)bytes) {
			goto out;
		}
	}
	if (read_full(op, output, 32) != 32)
		goto out;
	ret = 0;
out:
	if (op >= 0)
		close(op);
	if (tfm >= 0)
		close(tfm);
	return ret;
}

struct aes_vector {
	const char *key, *plain, *ecb, *cbc;
};

static const struct aes_vector vectors[] = {
	{
		"000102030405060708090a0b0c0d0e0f",
		"00112233445566778899aabbccddeeff",
		"69c4e0d86a7b0430d8cdb78070b4c55a",
		NULL,
	},
	{
		"000102030405060708090a0b0c0d0e0f1011121314151617",
		"00112233445566778899aabbccddeeff",
		"dda97ca4864cdfe06eaf70a0ec0d7191",
		NULL,
	},
	{
		"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
		"00112233445566778899aabbccddeeff",
		"8ea2b7ca516745bfeafc49904b496089",
		NULL,
	},
};

static const struct aes_vector cbc_vectors[] = {
	{
		"2b7e151628aed2a6abf7158809cf4f3c",
		"6bc1bee22e409f96e93d7e117393172a",
		NULL, "7649abac8119b246cee98e9b12e9197d",
	},
	{
		"8e73b0f7da0e6452c810f32b809079e562f8ead2522c6b7b",
		"6bc1bee22e409f96e93d7e117393172a",
		NULL, "4f021db243bc633d7178183a9fa071e8",
	},
	{
		"603deb1015ca71be2b73aef0857d7781"
		"1f352c073b6108d72d9810a30914dff4",
		"6bc1bee22e409f96e93d7e117393172a",
		NULL, "f58c4c04d6e5f1ba779eabfb5f7bfbd6",
	},
};

static int aes_known_answers(void)
{
	static const char *iv_hex = "000102030405060708090a0b0c0d0e0f";
	uint8_t key[32], plain[16], expected[16], actual[16], roundtrip[16], iv[16];
	size_t i, keylen;

	parse_hex(iv_hex, iv, sizeof(iv));
	for (i = 0; i < ARRAY_SIZE(vectors); i++) {
		keylen = strlen(vectors[i].key) / 2;
		parse_hex(vectors[i].key, key, keylen);
		parse_hex(vectors[i].plain, plain, sizeof(plain));
		parse_hex(vectors[i].ecb, expected, sizeof(expected));
		if (cipher("ecb-aes-a133-ce", key, keylen, NULL, 1,
			   plain, actual, sizeof(actual), 0) ||
		    memcmp(actual, expected, sizeof(actual)) ||
		    cipher("ecb-aes-a133-ce", key, keylen, NULL, 0,
			   actual, roundtrip, sizeof(roundtrip), 0) ||
		    memcmp(roundtrip, plain, sizeof(plain))) {
			errno = EIO;
			fail("AES ECB known-answer");
			return -1;
		}
	}
	for (i = 0; i < ARRAY_SIZE(cbc_vectors); i++) {
		keylen = strlen(cbc_vectors[i].key) / 2;
		parse_hex(cbc_vectors[i].key, key, keylen);
		parse_hex(cbc_vectors[i].plain, plain, sizeof(plain));
		parse_hex(cbc_vectors[i].cbc, expected, sizeof(expected));
		if (cipher("cbc-aes-a133-ce", key, keylen, iv, 1,
			   plain, actual, sizeof(actual), 0) ||
		    memcmp(actual, expected, sizeof(actual)) ||
		    cipher("cbc-aes-a133-ce", key, keylen, iv, 0,
			   actual, roundtrip, sizeof(roundtrip), 0) ||
		    memcmp(roundtrip, plain, sizeof(plain))) {
			errno = EIO;
			fail("AES CBC known-answer");
			return -1;
		}
	}
	return 0;
}

struct hash_vector {
	size_t bytes;
	const char *digest;
};

static const struct hash_vector hash_vectors[] = {
	{ 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" },
	{ 3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" },
	{ 64, "fdeab9acf3710362bd2658cdc9a29e8f9c757fcf9811603a8c447cd1d9151108" },
	{ 65408, "ce02bea80478976241533417dc952f77fe2f4718254994349011930aaa0f6e0b" },
	{ 65409, "0ad747a6d1411681b65d6b2f7795abd79271aeb6210a8f661f7566345aac9cf1" },
};

static int sha_known_answers(void)
{
	uint8_t *input, expected[32], actual[32];
	size_t i, j, largest = hash_vectors[ARRAY_SIZE(hash_vectors) - 1].bytes;

	input = malloc(largest);
	if (!input) {
		fail("SHA input allocation");
		return -1;
	}
	for (j = 0; j < largest; j++)
		input[j] = (uint8_t)j;
	memcpy(input, "abc", 3);
	for (i = 0; i < ARRAY_SIZE(hash_vectors); i++) {
		if (i == 2)
			for (j = 0; j < largest; j++)
				input[j] = (uint8_t)j;
		parse_hex(hash_vectors[i].digest, expected, sizeof(expected));
		if (hash(input, hash_vectors[i].bytes, actual, 0) ||
		    memcmp(actual, expected, sizeof(actual))) {
			errno = EIO;
			fail("SHA-256 direct known-answer");
			free(input);
			return -1;
		}
		if (hash_vectors[i].bytes &&
		    (hash(input, hash_vectors[i].bytes, actual, 1) ||
		     memcmp(actual, expected, sizeof(actual)))) {
			errno = EIO;
			fail("SHA-256 fragmented known-answer");
			free(input);
			return -1;
		}
	}
	free(input);
	return 0;
}

static int aes_boundaries(void)
{
	static const uint8_t key[16] = {
		0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
		0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
	};
	uint8_t block[16], encrypted[16], *input, *output, *roundtrip;
	const size_t lengths[] = { 16, 65536, 65552 };
	size_t i, j, bytes;

	parse_hex(vectors[0].plain, block, sizeof(block));
	parse_hex(vectors[0].ecb, encrypted, sizeof(encrypted));
	for (i = 0; i < ARRAY_SIZE(lengths); i++) {
		bytes = lengths[i];
		input = malloc(bytes);
		output = malloc(bytes);
		roundtrip = malloc(bytes);
		if (!input || !output || !roundtrip) {
			free(input); free(output); free(roundtrip);
			fail("AES boundary allocation");
			return -1;
		}
		for (j = 0; j < bytes; j += sizeof(block))
			memcpy(input + j, block, sizeof(block));
		if (cipher("ecb-aes-a133-ce", key, sizeof(key), NULL, 1,
			   input, output, bytes, 1)) {
			fail("AES boundary encrypt");
			free(input); free(output); free(roundtrip);
			return -1;
		}
		for (j = 0; j < bytes; j += sizeof(encrypted))
			if (memcmp(output + j, encrypted, sizeof(encrypted))) {
				errno = EIO;
				fail("AES boundary known-answer");
				free(input); free(output); free(roundtrip);
				return -1;
			}
		if (cipher("ecb-aes-a133-ce", key, sizeof(key), NULL, 0,
			   output, roundtrip, bytes, 1) || memcmp(input, roundtrip, bytes)) {
			errno = EIO;
			fail("AES boundary decrypt");
			free(input); free(output); free(roundtrip);
			return -1;
		}
		free(input); free(output); free(roundtrip);
	}
	return 0;
}

struct worker {
	pthread_barrier_t *barrier;
	unsigned int index, iterations;
	int result;
};

static void *concurrent_worker(void *opaque)
{
	static const uint8_t key[16] = {
		0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
		0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
	};
	uint8_t input[64], output[64], roundtrip[64], expected[32], digest[32];
	struct worker *worker = opaque;
	unsigned int i;

	for (i = 0; i < sizeof(input); i++)
		input[i] = (uint8_t)(i + worker->index);
	pthread_barrier_wait(worker->barrier);
	for (i = 0; i < worker->iterations; i++) {
		if ((i + worker->index) & 1) {
			if (cipher("ecb-aes-a133-ce", key, sizeof(key), NULL, 1,
				   input, output, sizeof(input), 1) ||
			    cipher("ecb-aes-a133-ce", key, sizeof(key), NULL, 0,
				   output, roundtrip, sizeof(input), 1) ||
			    memcmp(roundtrip, input, sizeof(input))) {
				worker->result = errno ?: EIO;
				return NULL;
			}
		} else {
			if (hash(input, sizeof(input), digest, 0)) {
				worker->result = errno ?: EIO;
				return NULL;
			}
			if (worker->index == 0) {
				parse_hex("fdeab9acf3710362bd2658cdc9a29e8f9c757fcf9811603a8c447cd1d9151108",
					  expected, sizeof(expected));
				if (memcmp(digest, expected, sizeof(digest))) {
					worker->result = EIO;
					return NULL;
				}
			}
		}
	}
	return NULL;
}

static int concurrency(unsigned int iterations)
{
	pthread_barrier_t barrier;
	pthread_t threads[4];
	struct worker workers[4];
	unsigned int i, started = 0;
	int ret = -1;

	if (pthread_barrier_init(&barrier, NULL, ARRAY_SIZE(threads))) {
		fail("concurrency barrier");
		return -1;
	}
	for (i = 0; i < ARRAY_SIZE(threads); i++) {
		workers[i] = (struct worker){ .barrier = &barrier, .index = i,
			.iterations = iterations };
		if (pthread_create(&threads[i], NULL, concurrent_worker, &workers[i])) {
			fail("concurrency thread");
			/* Existing workers are waiting at the barrier. Process exit after
			 * this failed qualification is the only bounded teardown.
			 */
			return -1;
		}
		started++;
	}
	ret = 0;
	for (i = 0; i < started; i++) {
		pthread_join(threads[i], NULL);
		if (workers[i].result) {
			errno = workers[i].result;
			fail("four-flow mixed concurrency");
			ret = -1;
		}
	}
	pthread_barrier_destroy(&barrier);
	return ret;
}

int main(int argc, char **argv)
{
	unsigned int iterations = 64;

	if (argc > 2)
		iterations = (unsigned int)strtoul(argv[2], NULL, 10);
	if (argc < 2 || !strcmp(argv[1], "all") || !strcmp(argv[1], "kat")) {
		aes_known_answers();
		sha_known_answers();
		aes_boundaries();
	}
	if (argc < 2 || !strcmp(argv[1], "all") || !strcmp(argv[1], "concurrency"))
		concurrency(iterations);
	if (argc >= 2 && strcmp(argv[1], "all") && strcmp(argv[1], "kat") &&
	    strcmp(argv[1], "concurrency")) {
		fprintf(stderr, "usage: %s [all|kat|concurrency [ITERATIONS]]\n", argv[0]);
		return 2;
	}
	if (failures)
		return 1;
	printf("PASS CE AES/SHA mode=%s\n", argc < 2 ? "all" : argv[1]);
	return 0;
}
