// SPDX-License-Identifier: MIT
/* Read-only dump of the DRM plane format/modifier ABI used by WPE qualification. */
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <drm_fourcc.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

static void print_fourcc(uint32_t format)
{
    printf("%c%c%c%c", format & 0xff, (format >> 8) & 0xff,
        (format >> 16) & 0xff, (format >> 24) & 0xff);
}

static void dump_in_formats(int fd, drmModeObjectPropertiesPtr properties)
{
    for (uint32_t i = 0; i < properties->count_props; i++) {
        drmModePropertyPtr property = drmModeGetProperty(fd, properties->props[i]);
        if (!property)
            continue;
        if (!strcmp(property->name, "IN_FORMATS") &&
            (property->flags & DRM_MODE_PROP_BLOB)) {
            drmModePropertyBlobPtr blob = drmModeGetPropertyBlob(fd,
                properties->prop_values[i]);
            if (blob && blob->length >= sizeof(struct drm_format_modifier_blob)) {
                const struct drm_format_modifier_blob *header = blob->data;
                const uint32_t *formats = (const uint32_t *)
                    ((const char *)blob->data + header->formats_offset);
                const struct drm_format_modifier *modifiers =
                    (const struct drm_format_modifier *)
                    ((const char *)blob->data + header->modifiers_offset);
                printf("  IN_FORMATS formats=%u modifiers=%u\n",
                    header->count_formats, header->count_modifiers);
                for (uint32_t f = 0; f < header->count_formats; f++) {
                    printf("    "); print_fourcc(formats[f]); printf(" modifiers=");
                    int any = 0;
                    for (uint32_t m = 0; m < header->count_modifiers; m++) {
                        if (f < modifiers[m].offset || f >= modifiers[m].offset + 64)
                            continue;
                        if (!(modifiers[m].formats & (UINT64_C(1) << (f - modifiers[m].offset))))
                            continue;
                        printf("%s0x%016" PRIx64, any++ ? "," : "",
                            (uint64_t)modifiers[m].modifier);
                    }
                    printf("%s\n", any ? "" : "none");
                }
            }
            if (blob)
                drmModeFreePropertyBlob(blob);
        }
        drmModeFreeProperty(property);
    }
}

int main(int argc, char **argv)
{
    const char *path = argc > 1 ? argv[1] : "/dev/dri/card0";
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        fprintf(stderr, "open %s: %s\n", path, strerror(errno));
        return 1;
    }
    if (drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1) ||
        drmSetClientCap(fd, DRM_CLIENT_CAP_ATOMIC, 1)) {
        fprintf(stderr, "client caps: %s\n", strerror(errno));
        close(fd);
        return 1;
    }
    drmModePlaneResPtr resources = drmModeGetPlaneResources(fd);
    if (!resources) {
        fprintf(stderr, "get plane resources: %s\n", strerror(errno));
        close(fd);
        return 1;
    }
    for (uint32_t p = 0; p < resources->count_planes; p++) {
        drmModePlanePtr plane = drmModeGetPlane(fd, resources->planes[p]);
        if (!plane)
            continue;
        printf("plane=%u possible_crtcs=0x%x crtc=%u fb=%u legacy_formats=%u\n",
            plane->plane_id, plane->possible_crtcs, plane->crtc_id,
            plane->fb_id, plane->count_formats);
        printf("  legacy=");
        for (uint32_t f = 0; f < plane->count_formats; f++) {
            if (f) printf(",");
            print_fourcc(plane->formats[f]);
        }
        printf("\n");
        drmModeObjectPropertiesPtr properties = drmModeObjectGetProperties(fd,
            plane->plane_id, DRM_MODE_OBJECT_PLANE);
        if (properties) {
            dump_in_formats(fd, properties);
            drmModeFreeObjectProperties(properties);
        }
        drmModeFreePlane(plane);
    }
    drmModeFreePlaneResources(resources);
    close(fd);
    return 0;
}
