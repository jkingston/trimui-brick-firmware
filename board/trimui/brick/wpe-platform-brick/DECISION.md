# Brick WPE 2.54 engine decision

## Selected implementation

Ship one engine path: **WPE WebKit 2.54.1 with its built-in DRM WPEPlatform
backend, using private Mesa softpipe/GBM and `GBM_ALWAYS_SOFTWARE=true`**.
Carry only the four narrowly scoped integration patches in
`package/brick-wpewebkit/`: pre-opened DRM-fd support, child-sandbox device
isolation, bounded linear-SHM scanout for trusted UI buffers, and same-view
producer isolation while the portal is unmapped. Do not carry a custom
WPEPlatform implementation or a second browser/presenter path.

The official 2.54.1 release archive is pinned by SHA-256
`0de8bdfba011f9cd63ba9a9171fa6038dd2c08237894f4082abbb821f29d3fe9`.
The reproducible Buildroot package and patch series are under
`package/brick-wpewebkit/`.

## Why this path is now viable

The Brick exposes mainline `sun4i-drm` at `/dev/dri/card0` and fbdev is its
emulation. There is no render node. `DRM_CAP_DUMB_BUFFER=1` was measured and a
1024x768 XRGB8888 dumb allocation mapped successfully, but that alone did not
prove WPE's required DMA-BUF transport.

The live Mesa 26.1.8 probe closed that gap. With
`GBM_ALWAYS_SOFTWARE=true`, Mesa softpipe:

* rendered a 1024x768 XRGB8888 GBM surface with 4096-byte stride;
* exported the front buffer as a linear-modifier DMA-BUF; and
* re-imported it using `GBM_BO_IMPORT_FD_MODIFIER | GBM_BO_USE_SCANOUT`.

That is the same buffer shape used by WPE 2.54's built-in DRM view. The probe
ran 120 frames at about 48.5 fps with a 64 MiB process high-water mark. The
stripped Mesa plus libdrm runtime is 17.79 MiB. These are measured prerequisite
results. The subsequent complete WPE run also closed the presentation gap:
the UI process passed `/dev/dri/card0` to the WebProcess, hardware transport
and DMA import/export were active, the controlled page reached `LOADED`, and
primary plane 47 scanned framebuffer 61 as 1024x768 linear XR24 allocated by
`brick-captive-wpe`. Normal authority-approved exit restored stock framebuffer
58 at offset `(0,1536)` and restarted the shell. The trusted native OSK uses a
linear ARGB SHM buffer which the UI process copies into a brokered scanout BO;
the sandboxed WebProcess still has no display-device access.

`LIBGL_ALWAYS_SOFTWARE` must remain unset. WPE 2.54 treats it as a request to
hide the display DRM device, selects SHM, and its direct-DRM view rejects that
buffer with FourCC zero. `GBM_ALWAYS_SOFTWARE=true` is the narrower Mesa
selection which retains the device-proven DMA-BUF path. No DRM kernel-driver
change or custom display backend is required; the four userspace patches above
are part of the selected and device-qualified implementation.

## Rejected no-EGL option

A CPU/SHM no-EGL source path was audited and a custom fbdev presenter compiled,
but it required changes to WebKit process initialization and preferences plus a
second Brick-specific presentation implementation. It was rejected before the
modified WebKit translation units were compiled. Maintaining that fork is not
justified now that the upstream DRM/GBM buffer contract is device-proven. Its
patch, audit scripts, custom platform sources and build profile were removed.

Stock PowerVR EGL remains unsuitable: live `eglInitialize()` returned
`EGL_NOT_INITIALIZED`. The selected build must therefore load Mesa from a
private runtime location and must not replace the shell's stock EGL libraries.

## Pass/fail gate status

1. **Passed:** clean Buildroot build of pinned WPE 2.54.1 with the four selected
   patches, DRM, GBM, libdrm, libinput and udev; Wayland and the legacy WPE API
   are disabled. Each patch also applies cleanly to a pristine 2.54.1 tree.
2. **Passed:** private Mesa softpipe/GBM initialization and DMA-BUF transport
   in the real WPE WebProcess with `GBM_ALWAYS_SOFTWARE=true`.
3. **Passed for normal exit and injected presenter-group `SIGKILL`:** exact KMS
   restoration and live shell restart. The forced WPE fault required four
   DRM-master attempts over 61 ms, within the bounded 3-second EBUSY policy.
   Visual flash/stale-content inspection and broader stress remain release
   qualification rather than architecture risks.
4. **Partially passed:** controlled HTTPS, redirects, cookies, forms,
   JavaScript, certificate errors and the same-view OSK field matrix pass.
   Physical-controller handling, cross-origin iframe compatibility and a
   representative lawful real-portal corpus remain release qualification.
5. **Partially passed:** cold start, browser memory enforcement, idle frame
   submission and CPU behavior are measured. External current measurement and
   the long reliability/low-memory matrix remain release qualification.

Failure of a gate blocks the feature; it does not activate a second engine
implementation.

## Primary sources

* WPE 2.54 options and backend dependencies:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/cmake/OptionsWPE.cmake>
* WPE DRM DMA-BUF import:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/WPEPlatform/wpe/drm/WPEViewDRM.cpp>
* Mesa GBM software selection and import/export behavior:
  <https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-26.1.8/src/gbm/backends/dri/gbm_dri.c>
* WPE architecture: <https://wpewebkit.org/about/architecture.html>
* DRM dumb-buffer UAPI:
  <https://docs.kernel.org/gpu/drm-uapi.html#dumb-buffer-memory-mapping-ioctl>
