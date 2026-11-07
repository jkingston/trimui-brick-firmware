# Brick WPE 2.54 DRM spike

This directory qualifies one captive-browser implementation: upstream WPE
WebKit 2.54.1's built-in DRM platform over Mesa softpipe/GBM. See
`../wpe-software-egl/README.md` for the live GBM/DMA-BUF measurements and
`DECISION.md` for the selection and remaining device gates.

Contents:

* `../../../package/brick-wpewebkit/`: canonical pinned Buildroot package for
  DRM + softpipe/GBM;
* `launcher/`: minimal captive browser and policy tests;
* `integration/`: display ownership/watchdog/sandbox experiments;
* `drm-dumb-probe.c`: non-disruptive KMS allocation evidence.

Run the browser and every WPE child with the private Mesa environment:

```sh
LD_LIBRARY_PATH=/opt/brick-wpe-mesa/lib \
LIBGL_DRIVERS_PATH=/opt/brick-wpe-mesa/lib/dri \
GBM_BACKENDS_PATH=/opt/brick-wpe-mesa/lib/gbm \
GBM_ALWAYS_SOFTWARE=true GALLIUM_DRIVER=softpipe \
brick-captive-wpe
```

Do not set `LIBGL_ALWAYS_SOFTWARE` for WPE. WPE 2.54 interprets that variable
as a request to hide the display's DRM device from WebKit, selects SHM
transport, and the DRM view consequently rejects the buffer. The
GBM-specific switch selects Mesa's proven kms_swrast/softpipe path while
preserving DMA-BUF transport.

Do not install Mesa's `libEGL.so`/`libGLESv2.so` over the stock PowerVR
libraries. The spike must be staged under a private prefix until packaging and
shell handoff are fully qualified.
