# WPE 2.54 software EGL/GBM spike

This spike asks whether an unmodified WPE WebKit 2.54 engine can replace the
Brick-specific no-EGL engine patch by supplying Mesa software EGL. It keeps
three claims separate:

1. **surfaceless EGL** can initialize and render without a GPU, but has no
   display or presentation path;
2. **software GBM/KMS** is useful to WPE's built-in DRM platform only if the
   WebProcess can export a rendered DMA-BUF which `sun4i-drm` accepts;
3. **DRM dumb buffers** are CPU-mappable scanout storage, but this alone does
   not make them DMA-BUF output from Mesa or make WPE's DRM view accept SHM.

## Decision

Prefer the **unmodified WPE 2.54 engine plus Mesa softpipe/GBM** over carrying
the no-EGL engine patch. The live Brick passed the prerequisite which was in
doubt: software EGL rendered an XRGB8888 GBM surface, exported its front buffer
as DMA-BUF, then re-imported the same linear-modifier DMA-BUF with
`GBM_BO_IMPORT_FD_MODIFIER` and `GBM_BO_USE_SCANOUT`, matching WPE's DRM import
shape. This is the maintained-engine route and now justifies a full WPE DRM
build/runtime gate.

Ship exactly one implementation: unmodified WPE 2.54's built-in DRM platform
with Mesa softpipe/GBM. Do not carry a CPU/SHM WPEPlatform implementation or a
no-EGL WebKit fork. The selected route adds 17.8 MB stripped runtime and its
simple full-frame render reaches about 48 fps, so storage/RAM and a 30 fps
captive-login UI must remain within budget. Framebuffer/page-flip, DRM-master
handoff, input, full pages and idle power remain product gates; failure of one
of those gates blocks the feature rather than selecting a second engine path.

The original risk was architectural rather than merely performance. WPE 2.54's
built-in DRM view accepts only `WPEBufferDMABuf`, then imports it into GBM with
`GBM_BO_USE_SCANOUT`; it rejects other buffer types. Mesa cannot export its
fallback `bo->image == NULL` dumb representation. On this device, however,
forcing `GBM_ALWAYS_SOFTWARE=true` creates a kms_swrast image which *is*
exportable and re-importable. Thus `DRM_CAP_DUMB_BUFFER=1` did not establish
the transport, but the live probe now does. Surfaceless EGL separately avoids
WPE's startup crash but does not put pixels on the LCD.

The standalone Mesa probes below may set `LIBGL_ALWAYS_SOFTWARE`, but the WPE
launcher must not. WPE 2.54 treats that variable as a request to suppress its
DRM device and falls back to SHM, which its direct-DRM view cannot present.
The selected WPE runtime uses `GBM_ALWAYS_SOFTWARE=true` and
`GALLIUM_DRIVER=softpipe` only.

## Reproducible Buildroot configuration

The tree currently pins Buildroot 2026.08.1, Mesa 26.1.8 and WPE WebKit
2.50.5. The checked-in `mesa-softpipe.fragment` enables softpipe, EGL, GLES
and GBM without LLVM. Applying that fragment to `brick_defconfig` selects only
`libdrm` and `libpthread-stubs` as new runtime packages; zlib and expat are
already in the Brick image. LLVM/llvmpipe is intentionally excluded.

The actual AArch64 Buildroot release output, stripped with the target strip
tool, is 17,692,672 bytes for Mesa's six runtime DSOs. Adding the newly
selected stripped `libdrm.so.2` makes the cold-image delta 17,788,880 bytes;
`libpthread-stubs` has no runtime DSO. A tar of the six Mesa DSOs is 3,950,858
bytes with gzip -9 or 2,703,792 bytes with xz -9. Filesystem overhead and
symlinks are excluded. The 17,240,632-byte `libgallium-26.1.8.so` dominates,
so llvmpipe's LLVM runtime would only increase this; it was not built.

The current `brick-gpu` package is not a Buildroot virtual EGL/GLES provider,
but installs proprietary `libEGL.so*` and `libGLESv2.so*` directly into
`/usr/lib`. Mesa selects the virtual providers and installs the same sonames.
Kconfig therefore permits an unsafe, order-dependent collision. Until the
providers are modelled correctly, stage Mesa under a private prefix for a
probe; do not enable both packages in a production target.

Buildroot's WPE package also hard-selects Wayland, WPEBackend-fdo and the
legacy API. WPE 2.54 has not yet been packaged in the pinned tree (nor in
upstream Buildroot master at the time of this spike). A 2.54 package update
must make the new WPEPlatform backends selectable. `wpe-2.54-software.cmake`
records the captive-browser profile: DRM and headless on, Wayland and legacy
API off, with multimedia and unused GPU-facing features off. The DRM choice
adds libinput >= 1.19 and udev; the pinned Buildroot versions satisfy the
declared minimums.

## Reproduce the probes

Create a throw-away Buildroot output directory and merge
`mesa-softpipe.fragment`. Do not use the production target directory while
the EGL provider collision is unresolved. Cross-compile with its staging
sysroot:

```sh
$HOST_DIR/bin/aarch64-none-linux-gnu-gcc -O2 -Wall -Wextra -Werror \
  --sysroot="$STAGING_DIR" surfaceless-egl-probe.c -o surfaceless-egl-probe \
  -lEGL -lGLESv2
$HOST_DIR/bin/aarch64-none-linux-gnu-gcc -O2 -Wall -Wextra -Werror \
  --sysroot="$STAGING_DIR" gbm-software-probe.c -o gbm-software-probe \
  -lgbm -lEGL -lGLESv2
$HOST_DIR/bin/aarch64-none-linux-gnu-gcc -O2 -Wall -Wextra -Werror \
  copy-bandwidth.c -o copy-bandwidth
```

Copy the binaries and Mesa libraries to one directory under `/tmp` on the
Brick. Run with a private loader path, never by replacing stock libraries:

```sh
LD_LIBRARY_PATH=/tmp/mesa/lib \
LIBGL_DRIVERS_PATH=/tmp/mesa/lib/dri \
GALLIUM_DRIVER=softpipe LIBGL_ALWAYS_SOFTWARE=true \
  /tmp/mesa/surfaceless-egl-probe 1024 768 120

LD_LIBRARY_PATH=/tmp/mesa/lib \
GBM_BACKENDS_PATH=/tmp/mesa/lib/gbm \
GBM_ALWAYS_SOFTWARE=true GALLIUM_DRIVER=softpipe \
LIBGL_ALWAYS_SOFTWARE=true \
  /tmp/mesa/gbm-software-probe /dev/dri/card0 1024 768 120

/tmp/mesa/copy-bandwidth 3145728 1000
```

The GBM probe does not become DRM master, add a framebuffer, modeset or flip.
It creates a GBM render+scanout surface, renders clears, locks its front
buffer, requires DMA-BUF export, then repeats WPE's modifier-aware import.
On the live Brick it succeeded with Mesa 26.1.8: GBM backend `drm`, softpipe,
XRGB8888, 4096-byte stride, DMA-BUF export, linear modifier (`0`), and
modifier re-import. Without `GBM_ALWAYS_SOFTWARE=true`, Mesa treated sun4i-drm
as display-only and failed looking for a compatible render node; forcing only
`GALLIUM_DRIVER=softpipe` instead selected a missing kmsro driver. The
GBM-specific switch is therefore required. Success remains a prerequisite for
WPE DRM, not proof of full WPE operation.

## Resource accounting

The fixed 1024x768 XRGB8888 frame is 3,145,728 bytes (3 MiB). Two scanout
buffers cost 6 MiB; three cost 9 MiB. A full-frame copy at 60 fps moves
180 MiB/s of payload and at least 360 MiB/s of DRAM read+write traffic; 30 fps
halves those figures.

On the connected Brick, three continuous 1,000-copy runs of the checked-in
`copy-bandwidth.c` measured 2.338, 2.385 and 2.372 ms per 3 MiB copy
(1,258--1,283 MiB/s payload) while CPU0 reported 1.992 GHz. A full-frame copy
therefore consumes about 14.2% of one core at 60 fps or 7.1% at 30 fps,
before HTML rendering. This is a worst-case continuous-damage cost. A static
portal must stop submitting frames and copying after damage settles; remove
CSS spinners and nonessential animation. Damage-area cost should scale with
copied bytes, and should be measured rather than assumed for tiny rectangles.

Battery current was not measurable from the exposed power-supply API:
`axp2202-battery` reports voltage but no `current_now`, and the test unit is
USB-connected. The CPU and byte-rate results are measured; a watt/current
claim is not.

The surfaceless probe succeeded and reported Mesa/softpipe OpenGL ES 3.1. At
1024x768 it measured 22.661 ms per clear+finish over 120 frames, 9.876 ms for
one full readback and 72,648 KiB max RSS. The GBM path measured 20.615 ms per
clear+swap over 120 frames (48.5 fps) and 64,132 KiB max RSS; a confirming
30-frame run measured 20.773 ms/frame and 64,124 KiB. These are process-level
high-water marks including Mesa and the probe, not a WPE memory estimate. They
show softpipe can meet a 30 fps login UI but has little headroom for 60 fps
full-screen animation.

## Primary sources

* WPE 2.54 build options and backend dependencies:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/cmake/OptionsWPE.cmake>
* WPE DRM buffer import implementation:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/WPEPlatform/wpe/drm/WPEViewDRM.cpp>
* WPE architecture and WPEPlatform extension model:
  <https://wpewebkit.org/about/architecture.html>
* Mesa runtime driver selection (`GALLIUM_DRIVER`,
  `LIBGL_ALWAYS_SOFTWARE`): <https://docs.mesa3d.org/envvars.html>
* Mesa llvmpipe design and threading:
  <https://docs.mesa3d.org/drivers/llvmpipe.html>
* Mesa 26 GBM DRI backend (`GBM_ALWAYS_SOFTWARE`, kms_swrast and DMA-BUF
  export logic):
  <https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-26.1.8/src/gbm/backends/dri/gbm_dri.c>
* Buildroot WPE package (2.50.5 and legacy Wayland dependency graph):
  <https://gitlab.com/buildroot.org/buildroot/-/blob/master/package/wpewebkit/wpewebkit.mk>
