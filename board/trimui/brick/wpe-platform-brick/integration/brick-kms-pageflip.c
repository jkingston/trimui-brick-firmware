#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <drm.h>
#include <drm_mode.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define DEVICE_PATH "/dev/dri/card0"
#define SHELL_INIT "/etc/init.d/S20brick-shell"
#define SHELL_PIDFILE "/run/brick-shell.pid"
#define EVENT_TIMEOUT_MS 3000
#define MAX_CRTC_CONNECTORS 16

struct dumb_buffer {
    uint32_t handle;
    uint32_t pitch;
    uint64_t size;
    uint32_t fb_id;
    void *map;
};

struct saved_display {
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t fb_width;
    uint32_t fb_height;
    uint32_t fb_pitch;
    uint32_t fb_bpp;
    uint32_t fb_depth;
    uint32_t x;
    uint32_t y;
    drmModeModeInfo mode;
    uint32_t connectors[MAX_CRTC_CONNECTORS];
    int connector_count;
};

static volatile sig_atomic_t interrupted;
static volatile sig_atomic_t flip_complete;
static unsigned int flip_sequence;
static unsigned int flip_sec;
static unsigned int flip_usec;

static void on_signal(int signo)
{
    (void)signo;
    interrupted = 1;
}

static int install_signal_handlers(void)
{
    const int signals[] = { SIGINT, SIGTERM, SIGHUP };
    struct sigaction action;
    size_t i;

    memset(&action, 0, sizeof(action));
    action.sa_handler = on_signal;
    sigemptyset(&action.sa_mask);

    for (i = 0; i < ARRAY_SIZE(signals); i++) {
        if (sigaction(signals[i], &action, NULL) < 0)
            return -1;
    }
    return 0;
}

static int read_pidfile(pid_t *pid)
{
    FILE *file;
    long value;

    file = fopen(SHELL_PIDFILE, "r");
    if (!file)
        return -1;
    if (fscanf(file, "%ld", &value) != 1 || value <= 1 || value > INT32_MAX) {
        fclose(file);
        errno = EINVAL;
        return -1;
    }
    fclose(file);
    *pid = (pid_t)value;
    return 0;
}

static bool process_alive(pid_t pid)
{
    if (kill(pid, 0) == 0)
        return true;
    return errno == EPERM;
}

static int wait_process_state(pid_t pid, bool alive, int timeout_ms)
{
    struct timespec delay = { .tv_sec = 0, .tv_nsec = 20 * 1000 * 1000 };
    int elapsed;

    for (elapsed = 0; elapsed <= timeout_ms; elapsed += 20) {
        if (process_alive(pid) == alive)
            return 0;
        nanosleep(&delay, NULL);
    }
    errno = ETIMEDOUT;
    return -1;
}

static int run_shell_service(const char *action)
{
    pid_t child;
    int status;

    child = fork();
    if (child < 0)
        return -1;
    if (child == 0) {
        execl(SHELL_INIT, SHELL_INIT, action, (char *)NULL);
        _exit(127);
    }
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR)
            return -1;
    }

    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        errno = ECHILD;
        return -1;
    }
    return 0;
}

static int find_active_display(int fd, struct saved_display *saved)
{
    drmModeRes *resources = NULL;
    drmModeConnector *connector = NULL;
    drmModeEncoder *encoder = NULL;
    drmModeCrtc *crtc = NULL;
    drmModeFB *fb = NULL;
    uint32_t target_crtc = 0;
    int i;
    int rc = -1;

    resources = drmModeGetResources(fd);
    if (!resources)
        goto out;

    for (i = 0; i < resources->count_connectors; i++) {
        connector = drmModeGetConnector(fd, resources->connectors[i]);
        if (!connector)
            continue;
        if (connector->connection == DRM_MODE_CONNECTED && connector->encoder_id) {
            encoder = drmModeGetEncoder(fd, connector->encoder_id);
            if (encoder && encoder->crtc_id) {
                target_crtc = encoder->crtc_id;
                drmModeFreeEncoder(encoder);
                encoder = NULL;
                drmModeFreeConnector(connector);
                connector = NULL;
                break;
            }
            drmModeFreeEncoder(encoder);
            encoder = NULL;
        }
        drmModeFreeConnector(connector);
        connector = NULL;
    }
    if (!target_crtc) {
        errno = ENODEV;
        goto out;
    }

    crtc = drmModeGetCrtc(fd, target_crtc);
    if (!crtc)
        goto out;
    if (!crtc->mode_valid || !crtc->buffer_id || crtc->width != crtc->mode.hdisplay ||
        crtc->height != crtc->mode.vdisplay) {
        fprintf(stderr,
                "refusing unsupported active state: mode_valid=%d fb=%u "
                "crtc=%ux%u mode=%ux%u offset=%u,%u\n",
                crtc->mode_valid, crtc->buffer_id, crtc->width, crtc->height,
                crtc->mode.hdisplay, crtc->mode.vdisplay, crtc->x, crtc->y);
        errno = ENOTSUP;
        goto out;
    }

    fb = drmModeGetFB(fd, crtc->buffer_id);
    if (!fb)
        goto out;
    if (fb->bpp != 32 || (fb->depth != 24 && fb->depth != 32) ||
        fb->width < crtc->x + crtc->mode.hdisplay ||
        fb->height < crtc->y + crtc->mode.vdisplay) {
        fprintf(stderr,
                "refusing unsupported framebuffer: %ux%u pitch=%u bpp=%u "
                "depth=%u for offset=%u,%u mode=%ux%u\n",
                fb->width, fb->height, fb->pitch, fb->bpp, fb->depth,
                crtc->x, crtc->y, crtc->mode.hdisplay, crtc->mode.vdisplay);
        errno = ENOTSUP;
        goto out;
    }

    memset(saved, 0, sizeof(*saved));
    saved->crtc_id = crtc->crtc_id;
    saved->fb_id = crtc->buffer_id;
    saved->fb_width = fb->width;
    saved->fb_height = fb->height;
    saved->fb_pitch = fb->pitch;
    saved->fb_bpp = fb->bpp;
    saved->fb_depth = fb->depth;
    saved->x = crtc->x;
    saved->y = crtc->y;
    saved->mode = crtc->mode;

    for (i = 0; i < resources->count_connectors; i++) {
        connector = drmModeGetConnector(fd, resources->connectors[i]);
        if (!connector)
            continue;
        if (connector->encoder_id) {
            encoder = drmModeGetEncoder(fd, connector->encoder_id);
            if (encoder && encoder->crtc_id == target_crtc) {
                if (saved->connector_count >= MAX_CRTC_CONNECTORS) {
                    errno = E2BIG;
                    goto out;
                }
                saved->connectors[saved->connector_count++] = connector->connector_id;
            }
            drmModeFreeEncoder(encoder);
            encoder = NULL;
        }
        drmModeFreeConnector(connector);
        connector = NULL;
    }
    if (saved->connector_count == 0) {
        errno = ENODEV;
        goto out;
    }

    rc = 0;
out:
    drmModeFreeCrtc(crtc);
    drmModeFreeFB(fb);
    drmModeFreeEncoder(encoder);
    drmModeFreeConnector(connector);
    drmModeFreeResources(resources);
    return rc;
}

static int create_buffer(int fd, uint32_t width, uint32_t height,
                         uint32_t depth,
                         struct dumb_buffer *buffer)
{
    struct drm_mode_create_dumb create;
    struct drm_mode_map_dumb map;

    memset(buffer, 0, sizeof(*buffer));
    memset(&create, 0, sizeof(create));
    create.width = width;
    create.height = height;
    create.bpp = 32;
    if (drmIoctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &create) < 0)
        return -1;
    buffer->handle = create.handle;
    buffer->pitch = create.pitch;
    buffer->size = create.size;

    if (drmModeAddFB(fd, width, height, depth, 32, buffer->pitch,
                     buffer->handle, &buffer->fb_id) < 0)
        return -1;

    memset(&map, 0, sizeof(map));
    map.handle = buffer->handle;
    if (drmIoctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) < 0)
        return -1;
    buffer->map = mmap(NULL, buffer->size, PROT_READ | PROT_WRITE,
                       MAP_SHARED, fd, map.offset);
    if (buffer->map == MAP_FAILED) {
        buffer->map = NULL;
        return -1;
    }
    return 0;
}

static void destroy_buffer(int fd, struct dumb_buffer *buffer)
{
    struct drm_mode_destroy_dumb destroy;

    if (buffer->map)
        munmap(buffer->map, buffer->size);
    if (buffer->fb_id && drmModeRmFB(fd, buffer->fb_id) < 0)
        fprintf(stderr, "warning: remove FB %u failed: %s\n",
                buffer->fb_id, strerror(errno));
    if (buffer->handle) {
        memset(&destroy, 0, sizeof(destroy));
        destroy.handle = buffer->handle;
        if (drmIoctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy) < 0)
            fprintf(stderr, "warning: destroy handle %u failed: %s\n",
                    buffer->handle, strerror(errno));
    }
    memset(buffer, 0, sizeof(*buffer));
}

static void paint_buffer(const struct dumb_buffer *buffer,
                         uint32_t width, uint32_t height, uint32_t phase)
{
    uint32_t x;
    uint32_t y;

    for (y = 0; y < height; y++) {
        uint32_t *row = (uint32_t *)((uint8_t *)buffer->map + y * buffer->pitch);
        for (x = 0; x < width; x++) {
            uint32_t band = (x / 64 + y / 64 + phase) % 3;
            row[x] = band == 0 ? 0x00204080 :
                     band == 1 ? 0x00008040 : 0x00802040;
        }
    }
    msync(buffer->map, buffer->size, MS_SYNC);
}

static void page_flip_handler(int fd, unsigned int sequence,
                              unsigned int tv_sec, unsigned int tv_usec,
                              void *user_data)
{
    (void)fd;
    (void)user_data;
    flip_sequence = sequence;
    flip_sec = tv_sec;
    flip_usec = tv_usec;
    flip_complete = 1;
}

static int wait_for_flip(int fd)
{
    drmEventContext context;
    struct pollfd pollfd;

    memset(&context, 0, sizeof(context));
    context.version = DRM_EVENT_CONTEXT_VERSION;
    context.page_flip_handler = page_flip_handler;
    pollfd.fd = fd;
    pollfd.events = POLLIN;

    flip_complete = 0;
    while (!flip_complete && !interrupted) {
        int rc = poll(&pollfd, 1, EVENT_TIMEOUT_MS);
        if (rc == 0) {
            errno = ETIMEDOUT;
            return -1;
        }
        if (rc < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (pollfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
            errno = EIO;
            return -1;
        }
        if ((pollfd.revents & POLLIN) && drmHandleEvent(fd, &context) < 0)
            return -1;
    }
    if (interrupted) {
        errno = EINTR;
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    struct saved_display saved;
    struct dumb_buffer buffers[2];
    pid_t original_shell_pid = -1;
    pid_t restarted_shell_pid = -1;
    int fd = -1;
    int exit_code = EXIT_FAILURE;
    bool shell_stopped = false;
    bool display_changed = false;
    bool restored = false;
    bool master = false;
    long cycles = 1;
    long cycle;
    char *end = NULL;

    memset(&saved, 0, sizeof(saved));
    memset(buffers, 0, sizeof(buffers));
    setvbuf(stdout, NULL, _IOLBF, 0);

    if (argc == 3 && strcmp(argv[1], "--cycles") == 0) {
        errno = 0;
        cycles = strtol(argv[2], &end, 10);
        if (errno || !end || *end || cycles < 1 || cycles > 1000) {
            fprintf(stderr, "--cycles must be an integer from 1 through 1000\n");
            return EXIT_FAILURE;
        }
    } else if (argc != 1) {
        fprintf(stderr, "usage: %s [--cycles 1..1000]\n", argv[0]);
        return EXIT_FAILURE;
    }

    if (install_signal_handlers() < 0) {
        perror("install signal handlers");
        goto cleanup;
    }
    if (read_pidfile(&original_shell_pid) < 0 ||
        !process_alive(original_shell_pid)) {
        fprintf(stderr, "brick-shell is not running; refusing disruptive probe\n");
        goto cleanup;
    }

    fd = open(DEVICE_PATH, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        perror("open DRM device");
        goto cleanup;
    }
    if (find_active_display(fd, &saved) < 0) {
        perror("record active display");
        goto cleanup;
    }
    printf("saved: crtc=%u fb=%u mode=%s %ux%u clock=%u flags=0x%x "
           "offset=%u,%u connectors=%d fb-geometry=%ux%u pitch=%u "
           "bpp=%u depth=%u\n",
           saved.crtc_id, saved.fb_id, saved.mode.name,
           saved.mode.hdisplay, saved.mode.vdisplay, saved.mode.clock,
           saved.mode.flags, saved.x, saved.y, saved.connector_count,
           saved.fb_width, saved.fb_height, saved.fb_pitch,
           saved.fb_bpp, saved.fb_depth);

    if (run_shell_service("stop") < 0) {
        perror("stop brick-shell");
        goto cleanup;
    }
    shell_stopped = true;
    if (wait_process_state(original_shell_pid, false, 2000) < 0) {
        perror("verify brick-shell stopped");
        goto cleanup;
    }
    printf("shell: stopped pid=%ld\n", (long)original_shell_pid);
    if (interrupted) {
        errno = EINTR;
        perror("interrupted after shell stop");
        goto cleanup;
    }

    if (drmSetMaster(fd) < 0) {
        perror("acquire DRM master");
        goto cleanup;
    }
    master = true;

    if (create_buffer(fd, saved.fb_width, saved.fb_height, saved.fb_depth,
                      &buffers[0]) < 0 ||
        create_buffer(fd, saved.fb_width, saved.fb_height, saved.fb_depth,
                      &buffers[1]) < 0) {
        perror("create dumb framebuffer");
        goto cleanup;
    }
    printf("buffers: fb1=%u handle1=%u pitch1=%u size1=%llu "
           "fb2=%u handle2=%u pitch2=%u size2=%llu\n",
           buffers[0].fb_id, buffers[0].handle, buffers[0].pitch,
           (unsigned long long)buffers[0].size,
           buffers[1].fb_id, buffers[1].handle, buffers[1].pitch,
           (unsigned long long)buffers[1].size);
    paint_buffer(&buffers[0], saved.fb_width, saved.fb_height, 0);
    paint_buffer(&buffers[1], saved.fb_width, saved.fb_height, 1);

    for (cycle = 1; cycle <= cycles; cycle++) {
        if (interrupted) {
            errno = EINTR;
            perror("interrupted before cycle");
            goto cleanup;
        }
        if (drmModeSetCrtc(fd, saved.crtc_id, buffers[0].fb_id,
                           saved.x, saved.y, saved.connectors,
                           saved.connector_count, &saved.mode) < 0) {
            perror("display first temporary framebuffer");
            goto cleanup;
        }
        display_changed = true;

        if (drmModePageFlip(fd, saved.crtc_id, buffers[1].fb_id,
                            DRM_MODE_PAGE_FLIP_EVENT, NULL) < 0) {
            perror("queue page flip");
            goto cleanup;
        }
        if (wait_for_flip(fd) < 0) {
            perror("wait page flip");
            goto cleanup;
        }

        if (drmModeSetCrtc(fd, saved.crtc_id, saved.fb_id, saved.x, saved.y,
                           saved.connectors, saved.connector_count,
                           &saved.mode) < 0) {
            perror("restore saved CRTC");
            goto cleanup;
        }
        restored = true;
        display_changed = false;
        printf("cycle=%ld flip-sequence=%u timestamp=%u.%06u restored-fb=%u\n",
               cycle, flip_sequence, flip_sec, flip_usec, saved.fb_id);
    }
    printf("restore: cycles=%ld crtc=%u fb=%u mode=%s\n",
           cycles, saved.crtc_id, saved.fb_id, saved.mode.name);
    exit_code = EXIT_SUCCESS;

cleanup:
    if (display_changed && fd >= 0 && saved.crtc_id) {
        if (drmModeSetCrtc(fd, saved.crtc_id, saved.fb_id, saved.x, saved.y,
                           saved.connectors, saved.connector_count,
                           &saved.mode) == 0) {
            restored = true;
            display_changed = false;
            fprintf(stderr, "cleanup: restored saved CRTC\n");
        } else {
            fprintf(stderr, "CRITICAL: cleanup restore failed: %s\n",
                    strerror(errno));
            exit_code = EXIT_FAILURE;
        }
    }
    if (fd >= 0) {
        destroy_buffer(fd, &buffers[1]);
        destroy_buffer(fd, &buffers[0]);
        if (master && drmDropMaster(fd) < 0)
            fprintf(stderr, "warning: drop DRM master failed: %s\n",
                    strerror(errno));
        close(fd);
    }
    if (shell_stopped) {
        if (run_shell_service("start") < 0) {
            perror("restart brick-shell");
            exit_code = EXIT_FAILURE;
        } else {
            int tries;
            for (tries = 0; tries < 100; tries++) {
                if (read_pidfile(&restarted_shell_pid) == 0 &&
                    process_alive(restarted_shell_pid))
                    break;
                usleep(20 * 1000);
            }
            if (tries == 100) {
                fprintf(stderr, "brick-shell restart could not be verified\n");
                exit_code = EXIT_FAILURE;
            } else {
                printf("shell: restarted pid=%ld\n", (long)restarted_shell_pid);
            }
        }
    }
    printf("result: %s restored=%s shell_restart=%s\n",
           exit_code == EXIT_SUCCESS ? "PASS" : "FAIL",
           restored ? "yes" : "no", shell_stopped ? "attempted" : "not-needed");
    return exit_code;
}
