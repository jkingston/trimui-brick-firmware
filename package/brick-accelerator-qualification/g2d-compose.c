// SPDX-License-Identifier: GPL-2.0-only
/* Bounded MMAP compositor qualification. Explicit device paths required. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/media.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>

#define COUNT 2
#define WIDTH 63
#define HEIGHT 47
#define FG_WIDTH 15
#define FG_HEIGHT 11
struct buffer { void *map; unsigned length; };
struct node { int fd; enum v4l2_buf_type type; struct v4l2_pix_format fmt; struct buffer buf[COUNT]; bool streaming; };
static struct node nodes[3];
static int mediafd = -1;
static const char *paths[4];
static unsigned failures;

static int xioctl(int fd, unsigned long request, void *arg)
{
	int ret;
	do { ret = ioctl(fd, request, arg); } while (ret < 0 && errno == EINTR);
	return ret;
}
static void cleanup(void)
{
	for (int n = 0; n < 3; n++) {
		if (nodes[n].fd < 0) continue;
		if (nodes[n].streaming) xioctl(nodes[n].fd, VIDIOC_STREAMOFF, &nodes[n].type);
		for (int b = 0; b < COUNT; b++)
			if (nodes[n].buf[b].map && nodes[n].buf[b].map != MAP_FAILED)
				munmap(nodes[n].buf[b].map, nodes[n].buf[b].length);
		close(nodes[n].fd); nodes[n].fd = -1;
	}
	if (mediafd >= 0) { close(mediafd); mediafd = -1; }
}
static void die(const char *what)
{
	perror(what); cleanup(); exit(2);
}
static void require(bool condition, const char *what)
{
	if (!condition) { fprintf(stderr, "FAIL %s\n", what); cleanup(); exit(2); }
}
static void verify_graph(void)
{
	static const char * const names[] = { "g2d-background", "g2d-foreground", "g2d-capture", "g2d-source-over" };
	struct media_entity_desc found[4] = { 0 }, entity = { .id = MEDIA_ENT_ID_FLAG_NEXT };
	struct media_device_info info = { 0 };
	unsigned seen = 0, iterations = 0;
	mediafd = open(paths[0], O_RDWR | O_CLOEXEC);
	if (mediafd < 0 || xioctl(mediafd, MEDIA_IOC_DEVICE_INFO, &info)) die("media identity");
	require(!strcmp(info.driver, "sun8i-rotate") && !strcmp(info.model, "A133 G2D compositor"), "exact media driver/model");
	while (!xioctl(mediafd, MEDIA_IOC_ENUM_ENTITIES, &entity)) {
		require(++iterations <= 64, "bounded graph enumeration");
		for (int n = 0; n < 4; n++) if (!strcmp(entity.name, names[n])) {
			require(!(seen & (1U << n)), "unique graph entity names");
			found[n] = entity; seen |= 1U << n;
		}
		entity.id |= MEDIA_ENT_ID_FLAG_NEXT;
	}
	require(errno == EINVAL && seen == 15, "complete named compositor graph");
	for (int n = 0; n < 3; n++) {
		struct stat st;
		struct v4l2_capability caps = { 0 };
		nodes[n].type = n == 2 ? V4L2_BUF_TYPE_VIDEO_CAPTURE : V4L2_BUF_TYPE_VIDEO_OUTPUT;
		nodes[n].fd = open(paths[n + 1], O_RDWR | O_NONBLOCK | O_CLOEXEC);
		if (nodes[n].fd < 0 || fstat(nodes[n].fd, &st) || xioctl(nodes[n].fd, VIDIOC_QUERYCAP, &caps)) die("video identity");
		require(S_ISCHR(st.st_mode) && major(st.st_rdev) == found[n].dev.major && minor(st.st_rdev) == found[n].dev.minor, "video path belongs to named media entity");
		require(!strcmp((char *)caps.driver, "sun50i-g2d-comp"), "compositor video driver");
		unsigned expected = V4L2_CAP_STREAMING | (n == 2 ? V4L2_CAP_VIDEO_CAPTURE : V4L2_CAP_VIDEO_OUTPUT);
		require((caps.device_caps & expected) == expected && !(caps.device_caps & V4L2_CAP_VIDEO_M2M), "node direction and MMAP streaming");
	}
	struct media_pad_desc pads[3];
	struct media_link_desc links[8];
	require(found[3].pads == 3 && found[3].links <= 8, "composer topology bounds");
	struct media_links_enum list = { .entity = found[3].id, .pads = pads, .links = links };
	memset(pads, 0, sizeof(pads)); memset(links, 0, sizeof(links));
	if (xioctl(mediafd, MEDIA_IOC_ENUM_LINKS, &list)) die("enumerate graph links");
	unsigned matched = 0;
	for (unsigned l = 0; l < found[3].links; l++) {
		struct media_link_desc *link = &links[l];
		if (!(link->flags & MEDIA_LNK_FL_ENABLED)) continue;
		require(link->flags & MEDIA_LNK_FL_IMMUTABLE, "fixed compositor routing");
		for (int n = 0; n < 3; n++) {
			bool match = n == 2 ? link->source.entity == found[3].id && link->source.index == 2 && link->sink.entity == found[2].id && link->sink.index == 0 : link->source.entity == found[n].id && link->source.index == 0 && link->sink.entity == found[3].id && link->sink.index == n;
			if (match) matched |= 1U << n;
		}
	}
	require(matched == 7, "all three immutable graph routes");
}
static void set_formats(bool xrgb)
{
	for (int n = 0; n < 3; n++) {
		struct v4l2_format f = { .type = nodes[n].type };
		f.fmt.pix.width = n == 1 ? FG_WIDTH : WIDTH;
		f.fmt.pix.height = n == 1 ? FG_HEIGHT : HEIGHT;
		f.fmt.pix.pixelformat = n == 1 && xrgb ? V4L2_PIX_FMT_XRGB32 : V4L2_PIX_FMT_ARGB32;
		f.fmt.pix.field = V4L2_FIELD_NONE;
		if (xioctl(nodes[n].fd, VIDIOC_S_FMT, &f)) die("set format");
		require(f.fmt.pix.width == (unsigned)(n == 1 ? FG_WIDTH : WIDTH) && f.fmt.pix.height == (unsigned)(n == 1 ? FG_HEIGHT : HEIGHT), "exact fixture dimensions");
		require(f.fmt.pix.pixelformat == (n == 1 && xrgb ? V4L2_PIX_FMT_XRGB32 : V4L2_PIX_FMT_ARGB32), "exact fixture pixel format");
		require(f.fmt.pix.bytesperline >= f.fmt.pix.width * 4 && f.fmt.pix.sizeimage >= f.fmt.pix.bytesperline * f.fmt.pix.height, "valid negotiated layout");
		nodes[n].fmt = f.fmt.pix;
	}
}
static void allocate(void)
{
	for (int n = 0; n < 3; n++) {
		struct v4l2_requestbuffers req = { .count = COUNT, .type = nodes[n].type, .memory = V4L2_MEMORY_MMAP };
		if (xioctl(nodes[n].fd, VIDIOC_REQBUFS, &req)) die("allocate buffers");
		require(req.count >= COUNT, "two buffers per queue");
		for (int b = 0; b < COUNT; b++) {
			struct v4l2_buffer buf = { .index = b, .type = nodes[n].type, .memory = V4L2_MEMORY_MMAP };
			if (xioctl(nodes[n].fd, VIDIOC_QUERYBUF, &buf)) die("query buffer");
			nodes[n].buf[b].length = buf.length;
			nodes[n].buf[b].map = mmap(NULL, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, nodes[n].fd, buf.m.offset);
			if (nodes[n].buf[b].map == MAP_FAILED) die("mmap buffer");
		}
	}
}
static uint32_t background(int frame) { return frame ? 0xff905010 : 0xff105090; }
static uint32_t foreground(int frame, unsigned alpha) { return alpha << 24 | (frame ? 0x20c040 : 0xc04020); }
static void fill(int n, int frame, unsigned alpha, bool xrgb)
{
	unsigned char *p = nodes[n].buf[frame].map;
	memset(p, 0xa5, nodes[n].buf[frame].length);
	if (n == 2) return;
	uint32_t color = n ? foreground(frame, xrgb ? 0 : alpha) : background(frame);
	for (unsigned y = 0; y < nodes[n].fmt.height; y++) {
		uint32_t *row = (uint32_t *)(p + y * nodes[n].fmt.bytesperline);
		for (unsigned x = 0; x < nodes[n].fmt.width; x++) row[x] = color;
	}
}
static void queue(int n, int frame)
{
	struct v4l2_buffer b = { .index = frame, .type = nodes[n].type, .memory = V4L2_MEMORY_MMAP };
	if (n != 2) {
		b.bytesused = nodes[n].fmt.sizeimage;
		b.timestamp.tv_sec = (n == 1 ? 100 : 200) + frame;
		b.timestamp.tv_usec = 12345;
	}
	if (xioctl(nodes[n].fd, VIDIOC_QBUF, &b)) die("queue buffer");
}
static struct v4l2_buffer dequeue(int n)
{
	struct pollfd p = { .fd = nodes[n].fd, .events = n == 2 ? POLLIN : POLLOUT };
	int ret;
	do { ret = poll(&p, 1, 2000); } while (ret < 0 && errno == EINTR);
	require(ret == 1, "bounded buffer completion");
	struct v4l2_buffer b = { .type = nodes[n].type, .memory = V4L2_MEMORY_MMAP };
	if (xioctl(nodes[n].fd, VIDIOC_DQBUF, &b)) die("dequeue buffer");
	require(!(b.flags & V4L2_BUF_FLAG_ERROR) && b.index < COUNT, "successful bounded job");
	return b;
}
static unsigned mismatch(int frame, int left, int top, unsigned alpha)
{
	unsigned count = 0;
	uint32_t bg = background(frame), fg = foreground(frame, alpha);
	unsigned char *p = nodes[2].buf[frame].map;
	for (int y = 0; y < HEIGHT; y++) for (int x = 0; x < WIDTH; x++) {
		uint32_t pixel = ((uint32_t *)(p + y * nodes[2].fmt.bytesperline))[x];
		bool inside = x >= left && x < left + FG_WIDTH && y >= top && y < top + FG_HEIGHT;
		if ((pixel >> 24) < 253) { count++; continue; }
		for (int channel = 0; channel < 3; channel++) {
			int b = (bg >> (channel * 8)) & 255, f = (fg >> (channel * 8)) & 255;
			int expected = inside ? (f * (int)alpha + b * (255 - (int)alpha) + 127) / 255 : b;
			int actual = (pixel >> (channel * 8)) & 255;
			if (abs(actual - expected) > 2) { count++; break; }
		}
	}
	return count;
}
static bool padding_unchanged(int n, int frame)
{
	unsigned char *p = nodes[n].buf[frame].map;
	for (unsigned y = 0; y < nodes[n].fmt.height; y++)
		for (unsigned x = nodes[n].fmt.width * 4; x < nodes[n].fmt.bytesperline; x++)
			if (p[y * nodes[n].fmt.bytesperline + x] != 0xa5) return false;
	for (unsigned x = nodes[n].fmt.sizeimage; x < nodes[n].buf[frame].length; x++)
		if (p[x] != 0xa5) return false;
	return true;
}
static bool input_unchanged(int n, int frame, unsigned alpha, bool xrgb)
{
	unsigned char *p = nodes[n].buf[frame].map;
	uint32_t color = n ? foreground(frame, xrgb ? 0 : alpha) : background(frame);
	for (unsigned y = 0; y < nodes[n].fmt.height; y++)
		for (unsigned x = 0; x < nodes[n].fmt.width; x++)
			if (((uint32_t *)(p + y * nodes[n].fmt.bytesperline))[x] != color)
				return false;
	return true;
}
static void run(unsigned alpha, bool xrgb, int left, int top, bool hold_capture)
{
	memset(nodes, 0, sizeof(nodes)); for (int n = 0; n < 3; n++) nodes[n].fd = -1;
	verify_graph(); set_formats(xrgb);
	struct v4l2_selection sel = { .type = V4L2_BUF_TYPE_VIDEO_OUTPUT, .target = V4L2_SEL_TGT_COMPOSE, .r = { left, top, FG_WIDTH, FG_HEIGHT } };
	if (xioctl(nodes[1].fd, VIDIOC_S_SELECTION, &sel)) die("foreground placement");
	if (xrgb) {
		struct v4l2_control ctrl = { .id = V4L2_CID_ALPHA_COMPONENT, .value = alpha };
		if (xioctl(nodes[1].fd, VIDIOC_S_CTRL, &ctrl)) die("global alpha");
	}
	allocate();
	for (int n = 0; n < 3; n++) for (int frame = 0; frame < COUNT; frame++) {
		fill(n, frame, alpha, xrgb);
		if (!(hold_capture && n == 2)) queue(n, frame);
	}
	for (int n = 0; n < 3; n++) {
		if (xioctl(nodes[n].fd, VIDIOC_STREAMON, &nodes[n].type)) die("start graph");
		nodes[n].streaming = true;
	}
	if (hold_capture) {
		struct pollfd p = { .fd = nodes[0].fd, .events = POLLOUT };
		require(poll(&p, 1, 100) == 0, "no source completion without capture buffer");
		for (int frame = 0; frame < COUNT; frame++) queue(2, frame);
	}
	for (int frame = 0; frame < COUNT; frame++) {
		struct v4l2_buffer done[3];
		for (int n = 0; n < 3; n++) {
			done[n] = dequeue(n);
			require(done[n].index == (unsigned)frame && done[n].sequence == (unsigned)frame, "FIFO triplet pairing and sequence");
			require(padding_unchanged(n, frame), "stride/trailing canaries untouched");
			if (n != 2) require(input_unchanged(n, frame, alpha, xrgb), "input pixels unchanged");
		}
		require(done[2].timestamp.tv_sec == 100 + frame && done[2].timestamp.tv_usec == 12345, "foreground timestamp propagated");
		unsigned nominal = mismatch(frame, left, top, alpha);
		unsigned shifted = mismatch(frame, left ? left - 1 : 0, top ? top - 1 : 0, alpha);
		printf("alpha=%u format=%s pos=%d,%d frame=%d mismatch_nominal=%u mismatch_stock_minus1=%u\n", alpha, xrgb ? "XRGB" : "ARGB", left, top, frame, nominal, shifted);
		if (nominal) failures++;
	}
	cleanup();
}
static long long milliseconds(void)
{
	struct timespec t;
	if (clock_gettime(CLOCK_MONOTONIC, &t)) die("monotonic clock");
	return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static void cancel_fixture(int stop)
{
	memset(nodes, 0, sizeof(nodes)); for (int n = 0; n < 3; n++) nodes[n].fd = -1;
	verify_graph(); set_formats(false); allocate();
	for (int n = 0; n < 3; n++) for (int frame = 0; frame < COUNT; frame++) {
		fill(n, frame, 127, false); queue(n, frame);
	}
	for (int n = 0; n < 3; n++) {
		if (xioctl(nodes[n].fd, VIDIOC_STREAMON, &nodes[n].type)) die("cancel fixture streamon");
		nodes[n].streaming = true;
	}
	long long start = milliseconds();
	if (xioctl(nodes[stop].fd, VIDIOC_STREAMOFF, &nodes[stop].type)) die("cancel fixture streamoff");
	nodes[stop].streaming = false;
	long long elapsed = milliseconds() - start;
	require(elapsed <= 1500, "bounded active streamoff");
	cleanup();
	printf("cancel_first_node=%d elapsed_ms=%lld (completion may precede cancellation)\n", stop, elapsed);
}
int main(int argc, char **argv)
{
	if (argc != 5) {
		fprintf(stderr, "Usage: %s /dev/mediaX /dev/videoBACKGROUND /dev/videoFOREGROUND /dev/videoCAPTURE\n", argv[0]); return 2;
	}
	for (int n = 0; n < 4; n++) paths[n] = argv[n + 1];
	for (int n = 0; n < 3; n++) nodes[n].fd = -1;
	setvbuf(stdout, NULL, _IOLBF, 0);
	for (int stop = 0; stop < 3; stop++) cancel_fixture(stop);
	run(0, false, 0, 0, false);
	run(127, false, 0, 0, true);
	run(255, false, 0, 0, false);
	run(127, true, 2, 3, false);
	run(255, false, 1, 1, false);
	run(255, false, WIDTH - FG_WIDTH, HEIGHT - FG_HEIGHT, false);
	printf("RESULT fixtures=6 frames=12 nominal_failures=%u (shifted matches are diagnostics, not acceptance)\n", failures);
	return failures ? 1 : 0;
}
