// SPDX-License-Identifier: GPL-2.0-only
#define _POSIX_C_SOURCE 200809L

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/dma-buf.h>
#include <linux/ioctl.h>
#include <linux/rtc.h>
#include <linux/types.h>
#include <linux/videodev2.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define WIDTH 320U
#define HEIGHT 240U
#define H264_BASELINE_FRAMES 6U
#define H264_EXTENDED_FRAMES 36U
#define H264_BUFFERS 8U
#define H264_FORCE_FRAME 5U
#define H264_GOP_SIZE 30U
#define JPEG_FRAMES 6U
#define RAW_SIZE (WIDTH * HEIGHT * 3U / 2U)
#define CAPTURE_SIZE (64U * 1024U)
#define JPEG_CAPTURE_SIZE (4U * 1024U * 1024U)
#define JPEG_BUFFERS 3U
#define CAPTURE_CANARY 0xa5
/* Stock traces show VE capture writes rounded to this granularity. */
#define CAPTURE_CANARY_ALIGN 256U
#define RTC_WAKE_SECONDS 10

/* Stable upstream DMA-heap UAPI, absent from the Buildroot toolchain headers. */
struct dma_heap_allocation_data {
	__u64 len;
	__u32 fd;
	__u32 fd_flags;
	__u64 heap_flags;
};

#define DMA_HEAP_IOC_MAGIC 'H'
#define DMA_HEAP_IOCTL_ALLOC \
	_IOWR(DMA_HEAP_IOC_MAGIC, 0x0, struct dma_heap_allocation_data)

#ifndef V4L2_PIX_FMT_H264_SLICE
#define V4L2_PIX_FMT_H264_SLICE v4l2_fourcc('S', '2', '6', '4')
#endif

struct dma_buffer {
	int fd;
	void *data;
	size_t length;
};

struct mapped_buffer {
	void *data;
	size_t length;
};

struct queue {
	enum v4l2_buf_type type;
	struct mapped_buffer buffers[H264_BUFFERS];
	unsigned int count;
};

static int xioctl(int fd, unsigned long request, void *arg)
{
	int ret;

	do {
		ret = ioctl(fd, request, arg);
	} while (ret < 0 && errno == EINTR);
	return ret;
}

static void fail(const char *operation)
{
	perror(operation);
	exit(EXIT_FAILURE);
}

static void checked_ioctl(int fd, unsigned long request, void *arg,
			  const char *operation)
{
	if (xioctl(fd, request, arg) < 0)
		fail(operation);
}

static void write_all(int fd, const void *data, size_t length)
{
	const uint8_t *cursor = data;

	while (length) {
		ssize_t count = write(fd, cursor, length);

		if (count < 0) {
			if (errno == EINTR)
				continue;
			fail("write");
		}
		if (!count) {
			errno = EIO;
			fail("write");
		}
		cursor += count;
		length -= count;
	}
}

static void generate_frame(uint8_t *data, unsigned int frame)
{
	uint8_t *y = data;
	uint8_t *u = y + WIDTH * HEIGHT;
	uint8_t *v = u + WIDTH * HEIGHT / 4;
	unsigned int square_x = 8 + frame * 13;
	unsigned int square_y = 16 + frame * 7;
	unsigned int row, col;

	for (row = 0; row < HEIGHT; row++) {
		for (col = 0; col < WIDTH; col++) {
			unsigned int value = (col * 3 + row * 2 + frame * 17) & 0xff;

			if (col >= square_x && col < square_x + 24 &&
			    row >= square_y && row < square_y + 24)
				value = 235 - frame * 9;
			y[row * WIDTH + col] = value;
		}
	}
	for (row = 0; row < HEIGHT / 2; row++) {
		for (col = 0; col < WIDTH / 2; col++) {
			u[row * (WIDTH / 2) + col] =
				(96 + col + frame * 11) & 0xff;
			v[row * (WIDTH / 2) + col] =
				(160 + row - frame * 7) & 0xff;
		}
	}
}

static void set_and_require_control(int fd, uint32_t id, int32_t expected)
{
	struct v4l2_control control = { .id = id, .value = expected };

	checked_ioctl(fd, VIDIOC_S_CTRL, &control, "VIDIOC_S_CTRL");
	control.value = 0;
	checked_ioctl(fd, VIDIOC_G_CTRL, &control, "VIDIOC_G_CTRL");
	if (control.value != expected) {
		fprintf(stderr, "control %#x is %d, expected %d\n", id,
			control.value, expected);
		exit(EXIT_FAILURE);
	}
}

static void press_control(int fd, uint32_t id)
{
	struct v4l2_control control = { .id = id };

	checked_ioctl(fd, VIDIOC_S_CTRL, &control, "VIDIOC_S_CTRL button");
}

static void set_format(int fd, enum v4l2_buf_type type, uint32_t format,
		       uint32_t size)
{
	struct v4l2_format fmt = { .type = type };

	fmt.fmt.pix.width = WIDTH;
	fmt.fmt.pix.height = HEIGHT;
	fmt.fmt.pix.pixelformat = format;
	fmt.fmt.pix.field = V4L2_FIELD_NONE;
	fmt.fmt.pix.sizeimage = size;
	checked_ioctl(fd, VIDIOC_S_FMT, &fmt, "VIDIOC_S_FMT");
	if (fmt.fmt.pix.width != WIDTH || fmt.fmt.pix.height != HEIGHT ||
	    fmt.fmt.pix.pixelformat != format || fmt.fmt.pix.sizeimage < size) {
		fprintf(stderr, "driver changed the bounded format contract\n");
		exit(EXIT_FAILURE);
	}
	if (type == V4L2_BUF_TYPE_VIDEO_OUTPUT &&
	    (fmt.fmt.pix.bytesperline != WIDTH || fmt.fmt.pix.sizeimage != RAW_SIZE)) {
		fprintf(stderr, "driver changed the planar source layout\n");
		exit(EXIT_FAILURE);
	}
}

static void map_h264_queue(int fd, struct queue *queue, unsigned int count)
{
	struct v4l2_requestbuffers request = {
		.count = count,
		.type = queue->type,
		.memory = V4L2_MEMORY_MMAP,
	};
	unsigned int i;

	checked_ioctl(fd, VIDIOC_REQBUFS, &request, "VIDIOC_REQBUFS");
	if (request.count != count) {
		fprintf(stderr, "driver supplied %u buffers, expected %u\n",
			request.count, count);
		exit(EXIT_FAILURE);
	}
	queue->count = request.count;
	for (i = 0; i < queue->count; i++) {
		struct v4l2_buffer buffer = {
			.type = queue->type,
			.memory = V4L2_MEMORY_MMAP,
			.index = i,
		};

		checked_ioctl(fd, VIDIOC_QUERYBUF, &buffer, "VIDIOC_QUERYBUF");
		queue->buffers[i].length = buffer.length;
		if (buffer.length < (queue->type == V4L2_BUF_TYPE_VIDEO_OUTPUT ?
				     RAW_SIZE : CAPTURE_SIZE)) {
			fprintf(stderr, "driver returned an undersized mapped buffer\n");
			exit(EXIT_FAILURE);
		}
		queue->buffers[i].data = mmap(NULL, buffer.length,
					      PROT_READ | PROT_WRITE, MAP_SHARED,
					      fd, buffer.m.offset);
		if (queue->buffers[i].data == MAP_FAILED)
			fail("mmap");
	}
}

static void unmap_queue(struct queue *queue)
{
	unsigned int i;

	for (i = 0; i < queue->count; i++) {
		if (munmap(queue->buffers[i].data, queue->buffers[i].length))
			fail("munmap");
	}
}

static void persist_close(int fd, const char *operation)
{
	int saved_errno;

	if (fsync(fd)) {
		saved_errno = errno;
		close(fd);
		errno = saved_errno;
		fail(operation);
	}
	if (close(fd))
		fail(operation);
}

static void queue_buffer(int fd, enum v4l2_buf_type type, unsigned int index,
			 size_t bytesused)
{
	struct v4l2_buffer buffer = {
		.type = type,
		.memory = V4L2_MEMORY_MMAP,
		.index = index,
		.bytesused = bytesused,
	};

	checked_ioctl(fd, VIDIOC_QBUF, &buffer, "VIDIOC_QBUF");
}

static int open_h264_encoder(const char *device)
{
	struct v4l2_capability capability = {};
	int fd = open(device, O_RDWR | O_CLOEXEC);

	if (fd < 0)
		fail(device);
	checked_ioctl(fd, VIDIOC_QUERYCAP, &capability, "VIDIOC_QUERYCAP");
	if (strcmp((char *)capability.card, "Cedrus encoder qualification")) {
		fprintf(stderr, "%s is not the qualification encoder\n", device);
		exit(EXIT_FAILURE);
	}
	set_format(fd, V4L2_BUF_TYPE_VIDEO_OUTPUT, V4L2_PIX_FMT_YUV420,
		   RAW_SIZE);
	set_format(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_PIX_FMT_H264,
		   CAPTURE_SIZE);
	set_and_require_control(fd, V4L2_CID_MPEG_VIDEO_H264_PROFILE,
			 V4L2_MPEG_VIDEO_H264_PROFILE_HIGH);
	set_and_require_control(fd, V4L2_CID_MPEG_VIDEO_H264_LEVEL,
			 V4L2_MPEG_VIDEO_H264_LEVEL_5_1);
	set_and_require_control(fd, V4L2_CID_MPEG_VIDEO_H264_ENTROPY_MODE,
			 V4L2_MPEG_VIDEO_H264_ENTROPY_MODE_CABAC);
	set_and_require_control(fd, V4L2_CID_MPEG_VIDEO_FRAME_RC_ENABLE, 0);
	set_and_require_control(fd, V4L2_CID_MPEG_VIDEO_B_FRAMES, 0);
	set_and_require_control(fd, V4L2_CID_MPEG_VIDEO_GOP_SIZE,
				 H264_GOP_SIZE);
	set_and_require_control(fd, V4L2_CID_MPEG_VIDEO_H264_I_FRAME_QP, 30);
	set_and_require_control(fd, V4L2_CID_MPEG_VIDEO_H264_P_FRAME_QP, 30);
	return fd;
}

static void initialise_h264_capture(struct mapped_buffer *buffer)
{
	memset(buffer->data, CAPTURE_CANARY, buffer->length);
}

static void start_queues(int fd, struct queue *output, struct queue *capture)
{
	checked_ioctl(fd, VIDIOC_STREAMON, &capture->type,
		      "VIDIOC_STREAMON capture");
	checked_ioctl(fd, VIDIOC_STREAMON, &output->type,
		      "VIDIOC_STREAMON output");
}

static void stop_queues(int fd, struct queue *output, struct queue *capture)
{
	checked_ioctl(fd, VIDIOC_STREAMOFF, &output->type,
		      "VIDIOC_STREAMOFF output");
	checked_ioctl(fd, VIDIOC_STREAMOFF, &capture->type,
		      "VIDIOC_STREAMOFF capture");
}

static void path_name(char *path, size_t size, const char *directory,
		      const char *session, unsigned int frame)
{
	int count = snprintf(path, size, "%s/%s-frame%u.h264", directory,
			     session, frame);

	if (count < 0 || (size_t)count >= size) {
		fprintf(stderr, "evidence path is too long\n");
		exit(EXIT_FAILURE);
	}
}

struct h264_nal_summary {
	char types[32];
	unsigned int sps;
	unsigned int pps;
	unsigned int idr;
	unsigned int p;
};

static void summarise_h264_nals(const uint8_t *data, size_t length,
				struct h264_nal_summary *summary)
{
	size_t offset = 0;
	unsigned int count = 0;

	memset(summary, 0, sizeof(*summary));
	while (offset + 3 < length) {
		size_t header;
		size_t used;
		unsigned int type;
		int written;

		if (data[offset] || data[offset + 1]) {
			offset++;
			continue;
		}
		if (data[offset + 2] == 1)
			header = offset + 3;
		else if (offset + 4 < length && !data[offset + 2] &&
			 data[offset + 3] == 1)
			header = offset + 4;
		else {
			offset++;
			continue;
		}
		if (header >= length)
			break;
		type = data[header] & 0x1f;
		used = strlen(summary->types);
		written = snprintf(summary->types + used,
				   sizeof(summary->types) - used,
				   "%s%u", count ? "," : "", type);
		if (written < 0 || (size_t)written >= sizeof(summary->types) - used) {
			fprintf(stderr, "too many H.264 NAL units\n");
			exit(EXIT_FAILURE);
		}
		count++;
		summary->sps += type == 7;
		summary->pps += type == 8;
		summary->idr += type == 5;
		summary->p += type == 1;
		offset = header + 1;
	}
	if (!count) {
		fprintf(stderr, "H.264 payload has no Annex-B NAL unit\n");
		exit(EXIT_FAILURE);
	}
}

static void require_h264_payload(const struct mapped_buffer *buffer,
				 size_t length, bool keyframe,
				 bool parameter_sets,
				 struct h264_nal_summary *summary)
{
	const char *expected = parameter_sets ? "7,8,5" :
		keyframe ? "5" : "1";
	size_t canary_start;
	size_t i;

	if (!length || length > buffer->length) {
		fprintf(stderr, "invalid bounded H.264 payload\n");
		exit(EXIT_FAILURE);
	}
	canary_start = (length + CAPTURE_CANARY_ALIGN - 1) &
			~(CAPTURE_CANARY_ALIGN - 1);
	for (i = canary_start; i < buffer->length; i++) {
		if (((const uint8_t *)buffer->data)[i] != CAPTURE_CANARY) {
			fprintf(stderr, "H.264 capture canary changed at %zu\n", i);
			exit(EXIT_FAILURE);
		}
	}
	summarise_h264_nals(buffer->data, length, summary);
	if (strcmp(summary->types, expected)) {
		fprintf(stderr, "H.264 NAL sequence %s, expected %s\n",
			summary->types, expected);
		exit(EXIT_FAILURE);
	}
}

static void persist_payload(const char *path, const void *data, size_t length)
{
	int evidence = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);

	if (evidence < 0)
		fail("open payload evidence");
	write_all(evidence, data, length);
	persist_close(evidence, "persist payload evidence");
}

static void persist_h264_frame(const char *directory, const char *session,
			       unsigned int frame, const void *data,
			       size_t length)
{
	char path[512];

	path_name(path, sizeof(path), directory, session, frame);
	persist_payload(path, data, length);
}

static void persist_h264_stream(const char *directory, const char *session,
				unsigned int frames)
{
	uint8_t buffer[4096];
	char path[512];
	unsigned int frame;
	int output;

	if (snprintf(path, sizeof(path), "%s/%s.h264", directory, session) >=
	    (int)sizeof(path)) {
		fprintf(stderr, "stream evidence path is too long\n");
		exit(EXIT_FAILURE);
	}
	output = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
	if (output < 0)
		fail("open concatenated evidence");
	for (frame = 0; frame < frames; frame++) {
		ssize_t count;
		int input;

		path_name(path, sizeof(path), directory, session, frame);
		input = open(path, O_RDONLY | O_CLOEXEC);
		if (input < 0)
			fail("open frame evidence");
		while ((count = read(input, buffer, sizeof(buffer))) != 0) {
			if (count < 0) {
				if (errno == EINTR)
					continue;
				fail("read frame evidence");
			}
			write_all(output, buffer, count);
		}
		if (close(input))
			fail("close frame evidence");
	}
	persist_close(output, "persist concatenated evidence");
}

static bool h264_keyframe_expected(unsigned int frame,
				   unsigned int force_frame)
{
	unsigned int last_idr = force_frame == UINT_MAX ? 0 : force_frame;

	return frame == 0 || frame == force_frame ||
	       frame == last_idr + H264_GOP_SIZE;
}

static void encode_session(const char *device, const char *directory,
			   const char *session, unsigned int frames,
			   unsigned int force_frame)
{
	struct queue output = { .type = V4L2_BUF_TYPE_VIDEO_OUTPUT };
	struct queue capture = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE };
	bool seen[H264_EXTENDED_FRAMES] = {};
	unsigned int buffers = frames < H264_BUFFERS ? frames : H264_BUFFERS;
	char path[512];
	FILE *metadata;
	unsigned int frame;
	int count;
	int fd;

	if (!frames || frames > H264_EXTENDED_FRAMES ||
	    (force_frame != UINT_MAX && force_frame >= frames)) {
		fprintf(stderr, "invalid H.264 session bounds\n");
		exit(EXIT_FAILURE);
	}
	fd = open_h264_encoder(device);
	count = snprintf(path, sizeof(path), "%s/%s.meta", directory, session);
	if (count < 0 || (size_t)count >= sizeof(path)) {
		fprintf(stderr, "evidence path is too long\n");
		exit(EXIT_FAILURE);
	}
	metadata = fopen(path, "wx");
	if (!metadata)
		fail("fopen metadata");
	map_h264_queue(fd, &output, buffers);
	map_h264_queue(fd, &capture, buffers);
	generate_frame(output.buffers[0].data, 0);
	initialise_h264_capture(&capture.buffers[0]);
	queue_buffer(fd, capture.type, 0, 0);
	queue_buffer(fd, output.type, 0, RAW_SIZE);
	start_queues(fd, &output, &capture);

	for (frame = 0; frame < frames; frame++) {
		struct v4l2_buffer captured = {
			.type = capture.type,
			.memory = V4L2_MEMORY_MMAP,
		};
		struct v4l2_buffer consumed = {
			.type = output.type,
			.memory = V4L2_MEMORY_MMAP,
		};
		struct h264_nal_summary nals;
		unsigned int index = frame % buffers;
		bool keyframe, pframe, expected_key;

		checked_ioctl(fd, VIDIOC_DQBUF, &captured,
			      "VIDIOC_DQBUF H.264 capture");
		checked_ioctl(fd, VIDIOC_DQBUF, &consumed,
			      "VIDIOC_DQBUF H.264 output");
		if (captured.index != index || consumed.index != index ||
		    captured.sequence != frame || seen[frame] ||
		    !captured.bytesused ||
		    captured.bytesused > capture.buffers[index].length ||
		    (captured.flags & V4L2_BUF_FLAG_ERROR) ||
		    (consumed.flags & V4L2_BUF_FLAG_ERROR)) {
			fprintf(stderr, "invalid H.264 completion\n");
			exit(EXIT_FAILURE);
		}
		seen[frame] = true;
		keyframe = captured.flags & V4L2_BUF_FLAG_KEYFRAME;
		pframe = captured.flags & V4L2_BUF_FLAG_PFRAME;
		expected_key = h264_keyframe_expected(frame, force_frame);
		if ((expected_key && (!keyframe || pframe)) ||
		    (!expected_key && (keyframe || !pframe))) {
			fprintf(stderr, "invalid H.264 frame type flags\n");
			exit(EXIT_FAILURE);
		}
		require_h264_payload(&capture.buffers[index], captured.bytesused,
				     expected_key, frame == 0, &nals);
		persist_h264_frame(directory, session, frame,
				   capture.buffers[index].data,
				   captured.bytesused);
		fprintf(metadata,
			"frame=%u index=%u sequence=%u bytes=%u flags=%#x "
			"key=%u p=%u canary=1 nals=%s\n",
			frame, captured.index, captured.sequence,
			captured.bytesused, captured.flags, keyframe, pframe,
			nals.types);

		if (frame + 1 < frames) {
			unsigned int next = frame + 1;
			unsigned int next_index = next % buffers;

			generate_frame(output.buffers[next_index].data, next);
			initialise_h264_capture(&capture.buffers[next_index]);
			if (next == force_frame)
				press_control(fd,
					      V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME);
			queue_buffer(fd, capture.type, next_index, 0);
			queue_buffer(fd, output.type, next_index, RAW_SIZE);
		}
	}
	if (fflush(metadata) || fsync(fileno(metadata)) || fclose(metadata))
		fail("persist H.264 metadata");
	persist_h264_stream(directory, session, frames);
	stop_queues(fd, &output, &capture);
	unmap_queue(&capture);
	unmap_queue(&output);
	if (close(fd))
		fail("close encoder");
}

static void abort_retry_session(const char *device, const char *directory)
{
	struct queue output = { .type = V4L2_BUF_TYPE_VIDEO_OUTPUT };
	struct queue capture = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE };
	struct v4l2_buffer captured = {
		.type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
		.memory = V4L2_MEMORY_MMAP,
	};
	struct v4l2_buffer consumed = {
		.type = V4L2_BUF_TYPE_VIDEO_OUTPUT,
		.memory = V4L2_MEMORY_MMAP,
	};
	struct h264_nal_summary nals;
	char path[512];
	FILE *metadata;
	int fd = open_h264_encoder(device);

	if (snprintf(path, sizeof(path), "%s/h264-abort-retry.meta", directory) >=
	    (int)sizeof(path)) {
		fprintf(stderr, "abort evidence path is too long\n");
		exit(EXIT_FAILURE);
	}
	metadata = fopen(path, "wx");
	if (!metadata)
		fail("fopen abort metadata");
	map_h264_queue(fd, &output, 3);
	map_h264_queue(fd, &capture, 3);
	generate_frame(output.buffers[0].data, 0);
	initialise_h264_capture(&capture.buffers[0]);
	queue_buffer(fd, capture.type, 0, 0);
	queue_buffer(fd, output.type, 0, RAW_SIZE);
	start_queues(fd, &output, &capture);
	checked_ioctl(fd, VIDIOC_DQBUF, &captured,
		      "VIDIOC_DQBUF abort startup capture");
	checked_ioctl(fd, VIDIOC_DQBUF, &consumed,
		      "VIDIOC_DQBUF abort startup output");
	if (captured.index || consumed.index || captured.sequence ||
	    !(captured.flags & V4L2_BUF_FLAG_KEYFRAME) ||
	    (captured.flags & (V4L2_BUF_FLAG_ERROR | V4L2_BUF_FLAG_PFRAME))) {
		fprintf(stderr, "invalid abort-session startup completion\n");
		exit(EXIT_FAILURE);
	}
	require_h264_payload(&capture.buffers[0], captured.bytesused, true,
			     true, &nals);
	if (snprintf(path, sizeof(path), "%s/h264-abort-startup.h264",
		     directory) >= (int)sizeof(path)) {
		fprintf(stderr, "abort startup path is too long\n");
		exit(EXIT_FAILURE);
	}
	persist_payload(path, capture.buffers[0].data, captured.bytesused);
	fprintf(metadata, "phase=startup bytes=%u key=1 canary=1 nals=%s\n",
		captured.bytesused, nals.types);
	press_control(fd, V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME);
	fprintf(metadata,
		"phase=abort force=1 evidence_persisted=1 source_frame=1\n");
	if (fflush(metadata) || fsync(fileno(metadata)))
		fail("persist pre-abort evidence");
	generate_frame(output.buffers[1].data, 1);
	initialise_h264_capture(&capture.buffers[1]);
	queue_buffer(fd, capture.type, 1, 0);
	queue_buffer(fd, output.type, 1, RAW_SIZE);
	stop_queues(fd, &output, &capture);

	generate_frame(output.buffers[2].data, 1);
	initialise_h264_capture(&capture.buffers[2]);
	queue_buffer(fd, capture.type, 2, 0);
	queue_buffer(fd, output.type, 2, RAW_SIZE);
	start_queues(fd, &output, &capture);
	memset(&captured, 0, sizeof(captured));
	captured.type = capture.type;
	captured.memory = V4L2_MEMORY_MMAP;
	memset(&consumed, 0, sizeof(consumed));
	consumed.type = output.type;
	consumed.memory = V4L2_MEMORY_MMAP;
	checked_ioctl(fd, VIDIOC_DQBUF, &captured,
		      "VIDIOC_DQBUF retry capture");
	checked_ioctl(fd, VIDIOC_DQBUF, &consumed,
		      "VIDIOC_DQBUF retry output");
	if (captured.index != 2 || consumed.index != 2 || captured.sequence ||
	    !(captured.flags & V4L2_BUF_FLAG_KEYFRAME) ||
	    (captured.flags & (V4L2_BUF_FLAG_ERROR | V4L2_BUF_FLAG_PFRAME))) {
		fprintf(stderr, "invalid forced retry completion\n");
		exit(EXIT_FAILURE);
	}
	require_h264_payload(&capture.buffers[2], captured.bytesused, true,
			     true, &nals);
	if (snprintf(path, sizeof(path), "%s/h264-abort-retry.h264",
		     directory) >= (int)sizeof(path)) {
		fprintf(stderr, "abort retry path is too long\n");
		exit(EXIT_FAILURE);
	}
	persist_payload(path, capture.buffers[2].data, captured.bytesused);
	fprintf(metadata, "phase=retry bytes=%u key=1 canary=1 nals=%s\n",
		captured.bytesused, nals.types);
	if (fflush(metadata) || fsync(fileno(metadata)) || fclose(metadata))
		fail("persist abort-retry metadata");
	stop_queues(fd, &output, &capture);
	unmap_queue(&capture);
	unmap_queue(&output);
	if (close(fd))
		fail("close abort-retry session");
}

static void generate_nv12_frame(uint8_t *data, unsigned int frame)
{
	uint8_t *y = data;
	uint8_t *uv = y + WIDTH * HEIGHT;
	unsigned int square_x = 8 + frame * 13;
	unsigned int square_y = 16 + frame * 7;
	unsigned int row, col;

	for (row = 0; row < HEIGHT; row++) {
		for (col = 0; col < WIDTH; col++) {
			unsigned int value = (col * 3 + row * 2 + frame * 17) & 0xff;

			if (col >= square_x && col < square_x + 24 &&
			    row >= square_y && row < square_y + 24)
				value = 235 - frame * 9;
			y[row * WIDTH + col] = value;
		}
	}
	for (row = 0; row < HEIGHT / 2; row++) {
		for (col = 0; col < WIDTH / 2; col++) {
			uv[row * WIDTH + col * 2] = (96 + col + frame * 11) & 0xff;
			uv[row * WIDTH + col * 2 + 1] =
				(160 + row - frame * 7) & 0xff;
		}
	}
}

static int open_jpeg_encoder(const char *device)
{
	struct v4l2_capability capability = {};
	int fd = open(device, O_RDWR | O_CLOEXEC);

	if (fd < 0)
		fail(device);
	checked_ioctl(fd, VIDIOC_QUERYCAP, &capability, "VIDIOC_QUERYCAP");
	if (strcmp((char *)capability.card, "Cedrus encoder qualification")) {
		fprintf(stderr, "%s is not the qualification encoder\n", device);
		exit(EXIT_FAILURE);
	}
	set_format(fd, V4L2_BUF_TYPE_VIDEO_OUTPUT, V4L2_PIX_FMT_NV12,
		   RAW_SIZE);
	set_format(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_PIX_FMT_JPEG,
		   JPEG_CAPTURE_SIZE);
	set_and_require_control(fd, V4L2_CID_JPEG_COMPRESSION_QUALITY, 95);
	return fd;
}

static void release_queue(int fd, enum v4l2_buf_type type,
			  enum v4l2_memory memory)
{
	struct v4l2_requestbuffers request = {
		.type = type,
		.memory = memory,
	};

	checked_ioctl(fd, VIDIOC_REQBUFS, &request, "VIDIOC_REQBUFS release");
}

static void map_jpeg_queue(int fd, struct queue *queue)
{
	struct v4l2_requestbuffers request = {
		.count = JPEG_BUFFERS,
		.type = queue->type,
		.memory = V4L2_MEMORY_MMAP,
	};
	unsigned int i;

	checked_ioctl(fd, VIDIOC_REQBUFS, &request, "VIDIOC_REQBUFS JPEG");
	if (request.count != JPEG_BUFFERS) {
		fprintf(stderr, "driver supplied %u JPEG buffers, expected %u\n",
			request.count, JPEG_BUFFERS);
		exit(EXIT_FAILURE);
	}
	queue->count = request.count;
	for (i = 0; i < queue->count; i++) {
		struct v4l2_buffer buffer = {
			.type = queue->type,
			.memory = V4L2_MEMORY_MMAP,
			.index = i,
		};
		size_t minimum = queue->type == V4L2_BUF_TYPE_VIDEO_OUTPUT ?
			RAW_SIZE : JPEG_CAPTURE_SIZE;

		checked_ioctl(fd, VIDIOC_QUERYBUF, &buffer, "VIDIOC_QUERYBUF JPEG");
		if (buffer.length < minimum) {
			fprintf(stderr, "driver returned undersized JPEG buffer\n");
			exit(EXIT_FAILURE);
		}
		queue->buffers[i].length = buffer.length;
		queue->buffers[i].data = mmap(NULL, buffer.length,
					      PROT_READ | PROT_WRITE, MAP_SHARED,
					      fd, buffer.m.offset);
		if (queue->buffers[i].data == MAP_FAILED)
			fail("mmap JPEG");
	}
}

static void request_dmabuf_queue(int fd, enum v4l2_buf_type type)
{
	struct v4l2_requestbuffers request = {
		.count = JPEG_BUFFERS,
		.type = type,
		.memory = V4L2_MEMORY_DMABUF,
	};

	checked_ioctl(fd, VIDIOC_REQBUFS, &request, "VIDIOC_REQBUFS DMA-BUF");
	if (request.count != JPEG_BUFFERS) {
		fprintf(stderr, "driver supplied %u DMA-BUF slots, expected %u\n",
			request.count, JPEG_BUFFERS);
		exit(EXIT_FAILURE);
	}
}

static void dmabuf_sync(int fd, uint64_t flags)
{
	struct dma_buf_sync sync = { .flags = flags };

	checked_ioctl(fd, DMA_BUF_IOCTL_SYNC, &sync, "DMA_BUF_IOCTL_SYNC");
}

static void allocate_dmabufs(struct dma_buffer buffers[JPEG_BUFFERS])
{
	const char *heap_path = "/dev/dma_heap/default_cma_region";
	unsigned int i;
	int heap = open(heap_path, O_RDWR | O_CLOEXEC);

	if (heap < 0)
		fail(heap_path);
	for (i = 0; i < JPEG_BUFFERS; i++) {
		struct dma_heap_allocation_data allocation = {
			.len = JPEG_CAPTURE_SIZE,
			.fd_flags = O_RDWR | O_CLOEXEC,
		};

		checked_ioctl(heap, DMA_HEAP_IOCTL_ALLOC, &allocation,
			      "DMA_HEAP_IOCTL_ALLOC");
		buffers[i].fd = allocation.fd;
		buffers[i].length = JPEG_CAPTURE_SIZE;
		buffers[i].data = mmap(NULL, buffers[i].length,
				       PROT_READ | PROT_WRITE, MAP_SHARED,
				       buffers[i].fd, 0);
		if (buffers[i].data == MAP_FAILED)
			fail("mmap DMA-BUF");
	}
	if (close(heap))
		fail("close DMA heap");
}

static void free_dmabufs(struct dma_buffer buffers[JPEG_BUFFERS])
{
	unsigned int i;

	for (i = 0; i < JPEG_BUFFERS; i++) {
		if (munmap(buffers[i].data, buffers[i].length))
			fail("munmap DMA-BUF");
		if (close(buffers[i].fd))
			fail("close DMA-BUF");
	}
}

static void initialise_capture(void *data, size_t length, int dmabuf_fd)
{
	if (dmabuf_fd >= 0)
		dmabuf_sync(dmabuf_fd, DMA_BUF_SYNC_START | DMA_BUF_SYNC_WRITE);
	memset(data, CAPTURE_CANARY, length);
	if (dmabuf_fd >= 0)
		dmabuf_sync(dmabuf_fd, DMA_BUF_SYNC_END | DMA_BUF_SYNC_WRITE);
}

static void queue_jpeg_capture(int fd, unsigned int index,
			       enum v4l2_memory memory, int dmabuf_fd)
{
	struct v4l2_buffer buffer = {
		.type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
		.memory = memory,
		.index = index,
		.length = JPEG_CAPTURE_SIZE,
	};

	if (memory == V4L2_MEMORY_DMABUF)
		buffer.m.fd = dmabuf_fd;
	checked_ioctl(fd, VIDIOC_QBUF, &buffer, "VIDIOC_QBUF JPEG capture");
}

static void require_jpeg_payload(const uint8_t *data, size_t length,
				 size_t capacity)
{
	size_t canary_start = (length + CAPTURE_CANARY_ALIGN - 1) &
			      ~(CAPTURE_CANARY_ALIGN - 1);
	size_t i;

	if (length < 4 || length > capacity || data[0] != 0xff ||
	    data[1] != 0xd8 || data[length - 2] != 0xff ||
	    data[length - 1] != 0xd9) {
		fprintf(stderr, "invalid bounded JPEG payload\n");
		exit(EXIT_FAILURE);
	}
	for (i = canary_start; i < capacity; i++) {
		if (data[i] != CAPTURE_CANARY) {
			fprintf(stderr, "JPEG capture canary changed at %zu\n", i);
			exit(EXIT_FAILURE);
		}
	}
}

static void jpeg_path(char *path, size_t size, const char *directory,
		      const char *session, unsigned int frame)
{
	int count = snprintf(path, size, "%s/%s-frame%u.jpg", directory,
			     session, frame);

	if (count < 0 || (size_t)count >= size) {
		fprintf(stderr, "JPEG evidence path is too long\n");
		exit(EXIT_FAILURE);
	}
}

static void persist_jpeg(const char *directory, const char *session,
			 unsigned int frame, const void *data, size_t length)
{
	char path[512];
	int evidence;

	jpeg_path(path, sizeof(path), directory, session, frame);
	evidence = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
	if (evidence < 0)
		fail("open JPEG evidence");
	write_all(evidence, data, length);
	persist_close(evidence, "persist JPEG evidence");
}

static void require_file_contains(const char *path, const char *needle)
{
	char buffer[256];
	ssize_t count;
	int fd = open(path, O_RDONLY | O_CLOEXEC);

	if (fd < 0)
		fail(path);
	count = read(fd, buffer, sizeof(buffer) - 1);
	if (count < 0)
		fail(path);
	buffer[count] = '\0';
	if (!strstr(buffer, needle)) {
		fprintf(stderr, "%s lacks required value %s\n", path, needle);
		exit(EXIT_FAILURE);
	}
	if (close(fd))
		fail(path);
}

static void timed_idle_suspend(const char *directory)
{
	struct rtc_wkalrm original = {};
	struct rtc_wkalrm alarm = { .enabled = 1 };
	struct rtc_wkalrm verified = {};
	struct timespec before, after;
	char path[512];
	time_t wake = time(NULL) + RTC_WAKE_SECONDS;
	struct tm tm;
	FILE *metadata;
	int fd, status;

	require_file_contains("/sys/power/state", "freeze");
	require_file_contains("/sys/class/rtc/rtc0/device/power/wakeup", "enabled");
	fd = open("/dev/rtc0", O_RDWR | O_CLOEXEC);
	if (fd < 0)
		fail("/dev/rtc0");
	checked_ioctl(fd, RTC_WKALM_RD, &original, "RTC_WKALM_RD original");
	if (original.enabled || original.pending) {
		fprintf(stderr, "RTC alarm is already enabled or pending\n");
		exit(EXIT_FAILURE);
	}
	if (!gmtime_r(&wake, &tm))
		fail("gmtime_r");
	alarm.time.tm_sec = tm.tm_sec;
	alarm.time.tm_min = tm.tm_min;
	alarm.time.tm_hour = tm.tm_hour;
	alarm.time.tm_mday = tm.tm_mday;
	alarm.time.tm_mon = tm.tm_mon;
	alarm.time.tm_year = tm.tm_year;
	checked_ioctl(fd, RTC_WKALM_SET, &alarm, "RTC_WKALM_SET");
	checked_ioctl(fd, RTC_WKALM_RD, &verified, "RTC_WKALM_RD armed");
	if (!verified.enabled) {
		fprintf(stderr, "RTC wake alarm did not arm\n");
		exit(EXIT_FAILURE);
	}
	if (clock_gettime(CLOCK_BOOTTIME, &before))
		fail("clock_gettime before suspend");
	status = system("/usr/sbin/brick-suspend --disconnect-bluetooth");
	alarm.enabled = 0;
	checked_ioctl(fd, RTC_WKALM_SET, &alarm, "RTC_WKALM_SET disarm");
	checked_ioctl(fd, RTC_WKALM_RD, &verified, "RTC_WKALM_RD disarmed");
	if (verified.enabled || verified.pending) {
		fprintf(stderr, "RTC wake alarm remained active\n");
		exit(EXIT_FAILURE);
	}
	if (close(fd))
		fail("close RTC");
	if (status != 0) {
		fprintf(stderr, "brick-suspend exited with status %d\n", status);
		exit(EXIT_FAILURE);
	}
	if (clock_gettime(CLOCK_BOOTTIME, &after))
		fail("clock_gettime after suspend");
	if (after.tv_sec - before.tv_sec < RTC_WAKE_SECONDS - 2) {
		fprintf(stderr, "timed suspend returned too early\n");
		exit(EXIT_FAILURE);
	}
	if (snprintf(path, sizeof(path), "%s/suspend.meta", directory) >=
	    (int)sizeof(path)) {
		fprintf(stderr, "suspend evidence path is too long\n");
		exit(EXIT_FAILURE);
	}
	metadata = fopen(path, "wx");
	if (!metadata)
		fail("fopen suspend metadata");
	fprintf(metadata,
		"mode=idle-streaming-no-job wake=rtc seconds=%lld active_job=not-run-no-deterministic-inflight-window\n",
		(long long)(after.tv_sec - before.tv_sec));
	if (fclose(metadata))
		fail("close suspend metadata");
}

static void encode_jpeg_session(const char *device, const char *directory,
				const char *session, bool use_dmabuf,
				bool suspend_idle)
{
	struct queue output = { .type = V4L2_BUF_TYPE_VIDEO_OUTPUT };
	struct queue capture = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE };
	struct dma_buffer dma[JPEG_BUFFERS] = {};
	enum v4l2_memory capture_memory = use_dmabuf ?
		V4L2_MEMORY_DMABUF : V4L2_MEMORY_MMAP;
	bool seen[JPEG_FRAMES] = {};
	char path[512];
	FILE *metadata;
	unsigned int i;
	int count;
	int fd = open_jpeg_encoder(device);

	count = snprintf(path, sizeof(path), "%s/%s.meta", directory, session);
	if (count < 0 || (size_t)count >= sizeof(path)) {
		fprintf(stderr, "JPEG metadata path is too long\n");
		exit(EXIT_FAILURE);
	}
	metadata = fopen(path, "wx");
	if (!metadata)
		fail("fopen JPEG metadata");
	map_jpeg_queue(fd, &output);
	if (use_dmabuf) {
		request_dmabuf_queue(fd, capture.type);
		allocate_dmabufs(dma);
	} else {
		map_jpeg_queue(fd, &capture);
	}
	for (i = 0; i < JPEG_BUFFERS; i++) {
		void *data = use_dmabuf ? dma[i].data : capture.buffers[i].data;
		size_t length = use_dmabuf ? dma[i].length : capture.buffers[i].length;

		initialise_capture(data, length, use_dmabuf ? dma[i].fd : -1);
		queue_jpeg_capture(fd, i, capture_memory,
				   use_dmabuf ? dma[i].fd : -1);
	}
	checked_ioctl(fd, VIDIOC_STREAMON, &capture.type,
		      "VIDIOC_STREAMON JPEG capture");
	checked_ioctl(fd, VIDIOC_STREAMON, &output.type,
		      "VIDIOC_STREAMON JPEG output");
	if (suspend_idle)
		timed_idle_suspend(directory);
	for (i = 0; i < JPEG_BUFFERS; i++) {
		generate_nv12_frame(output.buffers[i].data, i);
		queue_buffer(fd, output.type, i, RAW_SIZE);
	}

	for (i = 0; i < JPEG_FRAMES; i++) {
		struct v4l2_buffer captured = {
			.type = capture.type,
			.memory = capture_memory,
		};
		struct v4l2_buffer consumed = {
			.type = output.type,
			.memory = V4L2_MEMORY_MMAP,
		};
		void *data;
		size_t capacity;
		int dma_fd = -1;
		bool keyframe;

		checked_ioctl(fd, VIDIOC_DQBUF, &captured,
			      "VIDIOC_DQBUF JPEG capture");
		checked_ioctl(fd, VIDIOC_DQBUF, &consumed,
			      "VIDIOC_DQBUF JPEG output");
		if (captured.index >= JPEG_BUFFERS ||
		    consumed.index >= JPEG_BUFFERS ||
		    captured.sequence >= JPEG_FRAMES ||
		    seen[captured.sequence] || !captured.bytesused ||
		    (captured.flags & V4L2_BUF_FLAG_ERROR) ||
		    (consumed.flags & V4L2_BUF_FLAG_ERROR)) {
			fprintf(stderr, "invalid JPEG completion\n");
			exit(EXIT_FAILURE);
		}
		seen[captured.sequence] = true;
		data = use_dmabuf ? dma[captured.index].data :
			capture.buffers[captured.index].data;
		capacity = use_dmabuf ? dma[captured.index].length :
			capture.buffers[captured.index].length;
		if (use_dmabuf) {
			dma_fd = dma[captured.index].fd;
			dmabuf_sync(dma_fd, DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ);
		}
		require_jpeg_payload(data, captured.bytesused, capacity);
		persist_jpeg(directory, session, captured.sequence, data,
			     captured.bytesused);
		if (use_dmabuf)
			dmabuf_sync(dma_fd, DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ);
		keyframe = captured.flags & V4L2_BUF_FLAG_KEYFRAME;
		if (!keyframe || captured.flags & (V4L2_BUF_FLAG_PFRAME |
						   V4L2_BUF_FLAG_BFRAME)) {
			fprintf(stderr, "invalid JPEG frame flags\n");
			exit(EXIT_FAILURE);
		}
		fprintf(metadata,
			"frame=%u index=%u sequence=%u bytes=%u flags=%#x key=1 eoi=1 canary=1 memory=%s\n",
			captured.sequence, captured.index, captured.sequence,
			captured.bytesused, captured.flags,
			use_dmabuf ? "dmabuf" : "mmap");
		if (i + JPEG_BUFFERS < JPEG_FRAMES) {
			unsigned int next = i + JPEG_BUFFERS;

			generate_nv12_frame(output.buffers[consumed.index].data, next);
			initialise_capture(data, capacity, dma_fd);
			queue_jpeg_capture(fd, captured.index, capture_memory, dma_fd);
			queue_buffer(fd, output.type, consumed.index, RAW_SIZE);
		}
	}
	stop_queues(fd, &output, &capture);
	if (fflush(metadata) || fsync(fileno(metadata)) || fclose(metadata))
		fail("persist JPEG metadata");
	unmap_queue(&output);
	release_queue(fd, output.type, V4L2_MEMORY_MMAP);
	if (use_dmabuf) {
		release_queue(fd, capture.type, V4L2_MEMORY_DMABUF);
		free_dmabufs(dma);
	} else {
		unmap_queue(&capture);
		release_queue(fd, capture.type, V4L2_MEMORY_MMAP);
	}
	if (close(fd))
		fail("close JPEG encoder");
}

static void abort_jpeg_session(const char *device)
{
	struct queue output = { .type = V4L2_BUF_TYPE_VIDEO_OUTPUT };
	struct queue capture = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE };
	int fd = open_jpeg_encoder(device);
	unsigned int i;

	map_jpeg_queue(fd, &output);
	map_jpeg_queue(fd, &capture);
	for (i = 0; i < JPEG_BUFFERS; i++) {
		initialise_capture(capture.buffers[i].data,
				   capture.buffers[i].length, -1);
		queue_jpeg_capture(fd, i, V4L2_MEMORY_MMAP, -1);
	}
	checked_ioctl(fd, VIDIOC_STREAMON, &capture.type,
		      "VIDIOC_STREAMON JPEG abort capture");
	checked_ioctl(fd, VIDIOC_STREAMON, &output.type,
		      "VIDIOC_STREAMON JPEG abort output");
	stop_queues(fd, &output, &capture);
	unmap_queue(&capture);
	unmap_queue(&output);
	release_queue(fd, capture.type, V4L2_MEMORY_MMAP);
	release_queue(fd, output.type, V4L2_MEMORY_MMAP);
	if (close(fd))
		fail("close JPEG abort session");
}

static void record_decoder_interface(const char *directory)
{
	struct v4l2_capability capability;
	struct dirent *entry;
	char path[512];
	FILE *evidence;
	DIR *devices = opendir("/sys/class/video4linux");
	bool found = false;

	if (!devices)
		fail("opendir video4linux");
	if (snprintf(path, sizeof(path), "%s/decoder-probe", directory) >=
	    (int)sizeof(path)) {
		fprintf(stderr, "decoder evidence path is too long\n");
		exit(EXIT_FAILURE);
	}
	evidence = fopen(path, "wx");
	if (!evidence)
		fail("fopen decoder probe");
	while ((entry = readdir(devices))) {
		int fd;
		unsigned int type;

		if (strncmp(entry->d_name, "video", 5))
			continue;
		if (snprintf(path, sizeof(path), "/dev/%s", entry->d_name) >=
		    (int)sizeof(path))
			continue;
		fd = open(path, O_RDWR | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0 || xioctl(fd, VIDIOC_QUERYCAP, &capability) < 0) {
			if (fd >= 0)
				close(fd);
			continue;
		}
		if (strcmp((char *)capability.card, "Cedrus encoder qualification")) {
			bool h264_slice = false;

			fprintf(evidence, "device=%s card=%s\n", path, capability.card);
			for (type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
			     type <= V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE; type++) {
				struct v4l2_fmtdesc format = { .type = type };

				for (; xioctl(fd, VIDIOC_ENUM_FMT, &format) == 0;
				     format.index++)
				{
					fprintf(evidence,
						"type=%u format=%c%c%c%c\n", type,
						format.pixelformat & 0xff,
						(format.pixelformat >> 8) & 0xff,
						(format.pixelformat >> 16) & 0xff,
						(format.pixelformat >> 24) & 0xff);
					if (type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE &&
					    format.pixelformat == V4L2_PIX_FMT_H264_SLICE)
						h264_slice = true;
				}
			}
			found |= h264_slice;
		}
		if (close(fd))
			fail("close decoder probe");
	}
	if (closedir(devices))
		fail("closedir video4linux");
	if (!found) {
		fprintf(stderr, "no independent V4L2 decoder interface found\n");
		exit(EXIT_FAILURE);
	}
	if (fclose(evidence))
		fail("close decoder probe");
}

int main(int argc, char **argv)
{
	if (argc != 3) {
		fprintf(stderr, "Usage: %s DEVICE EVIDENCE_DIRECTORY\n", argv[0]);
		return EXIT_FAILURE;
	}
	encode_session(argv[1], argv[2], "h264-baseline",
		       H264_BASELINE_FRAMES, UINT_MAX);
	encode_session(argv[1], argv[2], "h264-before",
		       H264_EXTENDED_FRAMES, H264_FORCE_FRAME);
	abort_retry_session(argv[1], argv[2]);
	encode_jpeg_session(argv[1], argv[2], "jpeg-mmap1", false, false);
	abort_jpeg_session(argv[1]);
	encode_jpeg_session(argv[1], argv[2], "jpeg-mmap2", false, false);
	encode_jpeg_session(argv[1], argv[2], "jpeg-dmabuf", true, true);
	record_decoder_interface(argv[2]);
	encode_session(argv[1], argv[2], "h264-after",
		       H264_EXTENDED_FRAMES, H264_FORCE_FRAME);
	puts("ENCODER_QUALIFICATION_CAPTURE_COMPLETE");
	return EXIT_SUCCESS;
}
