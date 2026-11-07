// SPDX-License-Identifier: MIT
/* Runtime qualification probe for the primitives used by WebKit's sandbox. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(__aarch64__)
#define PROBE_AUDIT_ARCH AUDIT_ARCH_AARCH64
#elif defined(__x86_64__)
#define PROBE_AUDIT_ARCH AUDIT_ARCH_X86_64
#else
#error Unsupported probe architecture
#endif

static int probe_namespace(const char *name, int flag)
{
	pid_t child = fork();
	int status;

	if (child < 0)
		return -1;
	if (!child) {
		if (unshare(flag) < 0)
			_exit(errno > 0 && errno < 126 ? errno : 125);
		_exit(0);
	}
	if (waitpid(child, &status, 0) != child)
		return -1;
	if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
		printf("namespace_%s=ok\n", name);
		return 0;
	}
	errno = WIFEXITED(status) ? WEXITSTATUS(status) : ECHILD;
	printf("namespace_%s=failed errno=%d\n", name, errno);
	return 1;
}

static int probe_seccomp(void)
{
	struct sock_filter instructions[] = {
		BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
			 offsetof(struct seccomp_data, arch)),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, PROBE_AUDIT_ARCH, 1, 0),
		BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
		BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
			 offsetof(struct seccomp_data, nr)),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_getppid, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
		BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
	};
	struct sock_fprog program = {
		.len = sizeof(instructions) / sizeof(instructions[0]),
		.filter = instructions,
	};

	if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0 ||
	    prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program) < 0) {
		printf("seccomp_filter=failed errno=%d\n", errno);
		return 1;
	}
	errno = 0;
	if (syscall(__NR_getppid) != -1 || errno != EPERM) {
		puts("seccomp_filter=failed ineffective");
		return 1;
	}
	puts("seccomp_filter=ok");
	return 0;
}

int main(void)
{
	int failed = 0;

	failed |= probe_namespace("user", CLONE_NEWUSER);
	failed |= probe_namespace("mount", CLONE_NEWNS);
	failed |= probe_namespace("pid", CLONE_NEWPID);
	failed |= probe_namespace("net", CLONE_NEWNET);
	failed |= probe_seccomp();
	return failed ? EXIT_FAILURE : EXIT_SUCCESS;
}
