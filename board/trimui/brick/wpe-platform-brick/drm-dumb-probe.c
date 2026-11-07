// SPDX-License-Identifier: MIT
/*
 * Non-disruptive DRM/KMS capability probe for the TrimUI Brick.
 *
 * This deliberately does not become DRM master, change a mode, create a
 * framebuffer, or page flip.  It verifies that the display driver implements
 * the standard dumb-buffer allocation and mapping ABI needed by a CPU-rendered
 * WPEPlatform backend, then destroys everything it allocated.
 */

#include <drm/drm.h>
#include <drm/drm_mode.h>

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

static int drm_ioctl(int fd, unsigned long request, void *argument)
{
    int result;

    do {
        result = ioctl(fd, request, argument);
    } while (result == -1 && errno == EINTR);

    return result;
}

static int get_capability(int fd, uint64_t capability, uint64_t *value)
{
    struct drm_get_cap request = {
        .capability = capability,
    };

    if (drm_ioctl(fd, DRM_IOCTL_GET_CAP, &request) == -1)
        return -1;

    *value = request.value;
    return 0;
}

int main(int argc, char **argv)
{
    const char *path = argc > 1 ? argv[1] : "/dev/dri/card0";
    struct drm_mode_create_dumb create = {
        .width = 1024,
        .height = 768,
        .bpp = 32,
    };
    struct drm_mode_map_dumb map = { 0 };
    struct drm_mode_destroy_dumb destroy = { 0 };
    uint64_t dumb = 0;
    uint64_t preferred_depth = 0;
    uint64_t prefer_shadow = 0;
    void *mapping = MAP_FAILED;
    int fd;
    int status = 1;

    fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd == -1) {
        fprintf(stderr, "open %s: %s\n", path, strerror(errno));
        return 1;
    }

    if (get_capability(fd, DRM_CAP_DUMB_BUFFER, &dumb) == -1) {
        fprintf(stderr, "DRM_CAP_DUMB_BUFFER: %s\n", strerror(errno));
        goto out;
    }
    (void)get_capability(fd, DRM_CAP_DUMB_PREFERRED_DEPTH, &preferred_depth);
    (void)get_capability(fd, DRM_CAP_DUMB_PREFER_SHADOW, &prefer_shadow);
    printf("dumb_buffer=%" PRIu64 " preferred_depth=%" PRIu64
           " prefer_shadow=%" PRIu64 "\n",
           dumb, preferred_depth, prefer_shadow);
    if (!dumb) {
        fprintf(stderr, "driver does not support dumb buffers\n");
        goto out;
    }

    if (drm_ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &create) == -1) {
        fprintf(stderr, "CREATE_DUMB: %s\n", strerror(errno));
        goto out;
    }
    destroy.handle = create.handle;
    printf("created width=%u height=%u bpp=%u pitch=%u size=%" PRIu64
           " handle=%u\n",
           create.width, create.height, create.bpp, create.pitch,
           (uint64_t)create.size,
           create.handle);

    map.handle = create.handle;
    if (drm_ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) == -1) {
        fprintf(stderr, "MAP_DUMB: %s\n", strerror(errno));
        goto out;
    }
    mapping = mmap(NULL, create.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                   map.offset);
    if (mapping == MAP_FAILED) {
        fprintf(stderr, "mmap dumb buffer: %s\n", strerror(errno));
        goto out;
    }
    printf("mapped offset=%" PRIu64 " size=%" PRIu64 "\n",
           (uint64_t)map.offset, (uint64_t)create.size);
    status = 0;

out:
    if (mapping != MAP_FAILED)
        munmap(mapping, create.size);
    if (destroy.handle &&
        drm_ioctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy) == -1) {
        fprintf(stderr, "DESTROY_DUMB: %s\n", strerror(errno));
        status = 1;
    }
    close(fd);
    return status;
}
