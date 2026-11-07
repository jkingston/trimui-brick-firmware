# Captive-browser production integration qualification

## Status and recommendation

The single engine, display, sandbox and trusted-OSK architecture is now
device-proven, but the feature is **not yet shippable**. Continue with the
remaining physical-input, portal-corpus, resource and reliability gates.
There is one implementation: WPE 2.54 DRM over private Mesa softpipe/GBM with
the four bounded integration patches recorded in `DECISION.md`. Failure of a
mandatory gate blocks the feature.

This document separates device observations from host-only spikes and proposed
behavior. The production package now installs the broker, supervisor,
restore-only KMS watchdog, connectivity authority, display hooks and control
CLI from `integration/`; programs explicitly named as probes or spikes remain
qualification-only. The complete production composition has passed normal,
UI-`SIGKILL`, supervisor-`SIGKILL` and broker-`SIGKILL` runs on the Brick with
exact KMS restoration and no process, cgroup or display-lock leak.

## Evidence obtained

### Device-observed

* `/dev/dri/card0` is `sun4i-drm`; connector `DSI-1` is connected at
  1024x768. `/dev/fb0` is the `sun4i-drmdrmfb` compatibility framebuffer.
* The non-disruptive `drm-dumb-probe.c` reports dumb-buffer support and created
  and mapped a 1024x768x32 buffer with pitch 4096 and size 3,145,728 bytes. It
  did not acquire DRM master, add a framebuffer, modeset or page-flip. The
  follow-up `integration/brick-kms-pageflip.c` did perform the coordinated
  ownership path. The stock framebuffer is 1024x2304x32, pitch 4096, depth 24;
  CRTC 55 scans a 1024x768 mode from offset `(0,1536)` in framebuffer 58.
  This confirms the stock shell uses a three-page virtual framebuffer and is
  why a temporary 1024x768 allocation alone cannot preserve current state.
* In the controlled cycle, the probe stopped shell PID 18393, acquired DRM
  master, added two 1024x2304 dumb framebuffers (IDs 60 and 61, 9,437,184
  bytes each), set framebuffer 60 using the exact recorded mode and offset,
  received the page-flip event for framebuffer 61 at sequence 5127, restored
  framebuffer 58, and restarted the shell as PID 19625. The bounded follow-up
  completed 20 set/flip/restore cycles, sequences 5467 through 5524, restored
  framebuffer 58 after every cycle, and restarted a verified live shell as
  PID 19654. A subsequent `SIGTERM` during a 1,000-cycle run arrived after 18
  completed cycles; the signal path retained restored framebuffer 58, cleaned
  its temporary objects, returned the expected failure status and restarted
  the shell as PID 19697. `DSI-1` remained connected and fb0 remained
  `sun4i-drmdrmfb`.
  This proves legacy KMS ownership, event delivery and cooperative restoration
  plus in-process `SIGTERM` cleanup on this kernel/display. That in-process
  probe alone does not prove atomic support, recovery from `SIGKILL` or
  supervisor failure, visual absence of tear/flash, or the 100-cycle release
  gate; the following external-watchdog evidence addresses part of that gap.
* `integration/brick-kms-watchdog.c` then closed the external `SIGKILL`
  recovery uncertainty for the probe architecture. It recorded CRTC 55,
  framebuffer 58 and offset `(0,1536)`, relinquished DRM master, launched the
  presenter as a separate process group and injected `SIGKILL` during cycle
  12. After observing signal 9, the watchdog reacquired DRM master, restored
  the exact saved CRTC, restarted the shell as PID 19748 and reported recovery
  PASS. The post-fault DRM client and GEM-name tables contained no entries and
  the framebuffer inventory contained only fbcon framebuffer 58.
* The full WPE 2.54.1 browser then loaded the controlled HTTP page using the
  real Bubblewrap WebProcess. With `LIBGL_ALWAYS_SOFTWARE` unset and
  `GBM_ALWAYS_SOFTWARE=true`, logs proved hardware buffer transport plus DMA
  import/export; plane 47 actively scanned a 1024x768 linear XR24 framebuffer
  allocated by `brick-captive-wpe`. The authority observed `LOADED`, sent
  `SUCCESS`, and the watchdog restored framebuffer 58 and restarted the shell.
  In the forced-fault run the presenter group received `SIGKILL`; delayed fd
  teardown produced three bounded `EBUSY` retries, DRM master was acquired on
  attempt four after 61 ms, and exact display/shell recovery passed. This also
  proves why `LIBGL_ALWAYS_SOFTWARE` is invalid here: WPE suppresses its DRM
  device, chooses SHM and presents FourCC zero to its DMA-BUF-only DRM view.
* A subsequent object-lifetime qualification ran 100 complete watchdog and
  presenter sessions, not merely 100 flips. Every session stopped and restarted
  the shell and created, displayed, flipped, restored, removed and destroyed
  two temporary KMS buffers. All 100 passed. The pre/post DRM debugfs client,
  GEM-name and framebuffer files were byte-identical with SHA-256 values
  `bdba0afb8e9c2e22aa04b7080aa3dfc5545fb054ab54218c0adb22a7eabe9bf3`,
  `2c52f8be8fa21d27a0e5824c3ed0c11fa6af01a005d1a027f885191670f1750e`
  and `3873a90fedbdbf9073a4d9f4f8090754ad85de0912c5c0f17cc0df85b78de93c`;
  the complete 1,747-byte atomic state file was also byte-identical at
  `da193dfbda1d84d37f2ab77a962b07f4ec1bb957eea3b77977d79fd9636945bb`.
  The 59,844-byte session log has SHA-256
  `9c87726037157d2a336a81608f7e8f7347965f039a5b32ed52baa515dfb52aa3`
  and contains exactly 100 probe PASS records, 100 watchdog exit-0 records and
  100 temporary framebuffer-pair creation records.
  The final inventory had one framebuffer, fbcon framebuffer 58, and the shell
  was live as PID 22097 with DSI-1 connected. A targeted kernel-log scan found
  no DRM, GEM, warning, BUG or oops entry. This is strong evidence against a
  client, framebuffer or named-GEM leak in normal ownership churn. It does not
  measure unexported allocator fragmentation and is not visual evidence of
  tear, flash, stale content or correct repaint.
* The earlier native NetSurf test proved the current fbdev page geometry,
  evdev gamepad path and controlled form/cookie/JavaScript fixture on the
  Brick. That does not prove WPE lifecycle or portal compatibility.
* `integration/brick-sandbox-probe.c` was cross-compiled and run on the Brick.
  User, mount, PID and network namespace creation all succeeded, and an
  installed seccomp rule returned the expected `EPERM`. This proves the kernel
  primitives in that root-run probe, not WebKit's complete sandbox policy.
* The package-level follow-up used an isolated Buildroot output and staged
  Bubblewrap 0.13.0, libseccomp 2.6.1 and xdg-dbus-proxy 0.1.8 entirely under
  `/tmp`; it did not modify the production root filesystem. All 23 staged files
  passed SHA-256 verification on the Brick. `brick-sandbox-device-test` then
  launched real Bubblewrap as dedicated UID/GID 65533, without setuid or root
  execution. All six namespace IDs changed: user `4026531837` to `4026532527`,
  mount `4026531832` to `4026532484`, PID `4026531836` to `4026532487`, network
  `4026531833` to `4026532489`, IPC `4026531839` to `4026532486`, and UTS
  `4026531838` to `4026532485`. The sandbox retained UID/GID 65533.
  `/data/wifi/wpa_supplicant.conf`, `/dev/fb0`, `/dev/dri/card0` and
  `/dev/input/event0` were absent inside the policy, and the staged libseccomp
  filter denied `getppid` with `EPERM`; the runtime probe reported PASS.
  Killing the inner child with `SIGKILL` and, in a separate run, killing outer
  Bubblewrap with `SIGKILL` both left no marker child or namespace helper.
  This proves the proposed package and privilege primitives on the device.
  The later actual-WPE audit below closes the multi-process namespace,
  seccomp and display-fd inheritance questions; unrestricted D-Bus remains
  disabled rather than exposed through a proxy allow-list.

### Source/configuration-observed

Resolving `trimui_brick_defconfig` in a fresh output directory produced:

* namespaces: user, mount, PID, IPC, UTS and network enabled;
* seccomp and seccomp-filter enabled;
* cgroups, memory cgroup and tmpfs enabled.

Buildroot's WPE sandbox option selects bubblewrap, xdg-dbus-proxy and
libseccomp and builds WebKit with its bubblewrap sandbox. The current Brick
defconfig selects none of WPE WebKit, its sandbox, bubblewrap or libseccomp.
This is declared package capability, not a device runtime result.

### Host-observed

`tests/test_captive_browser_integration.py` now passes 32 tests, including:

* browser child failure still invokes display resume and preserves its status;
* two stable 64-hex network keys receive separate mode-0700 data trees;
* raw SSIDs, path traversal and non-web URLs are rejected;
* the evdev translator handles pointer, click, scroll, key repeat and the
  required `SYN_DROPPED` ignore/resynchronize sequence;
* the sandbox probe compiles and its seccomp deny rule takes effect;
* the local portal fixture exercises CAPPORT metadata, hidden form state,
  POST, an HttpOnly/SameSite cookie and a JavaScript redirect target.

On this development host the probe reported user namespaces and seccomp
working, while mount, PID and network unshare were denied by the host's outer
sandbox. Those host denials say nothing about the Brick. The same binary was
cross-compiled and run on the Brick, where all four namespace probes and the
seccomp-filter probe passed as recorded above. This still does not substitute
for launching WebKit's complete Bubblewrap policy as its intended
unprivileged product user.

## Display and input ownership

Use one product-wide cooperative display lease. The shell releases it only
after stopping input delivery and completing a full frame. The browser then
owns display and browser input until it exits. `brick-captive-supervisor`
serializes this lifecycle and always invokes the resume hook after a normal
exit, signal, or browser-child crash.

The supervisor cannot recover if the supervisor itself is killed with
`SIGKILL` or the kernel panics. In the product, a long-lived init-managed
display broker must own the lease and child process. If the child disappears,
the broker must restore display state, release the evdev grab, restart or
resume the shell, and demand a full shell repaint. A timeout must kill a hung
browser process group before recovery.

### KMS production path

KMS ownership and the legacy CRTC/page-flip path are device-proven. The
probe deliberately clones the stock 1024x2304 virtual geometry and preserves
the `(0,1536)` scanout offset; a production presenter must not assume that the
visible 1024x768 mode is the full framebuffer. Qualify the selected WPE DRM
path as follows:

1. pauses the shell and records connector, CRTC, plane, mode and framebuffer
   state;
2. acquires DRM master without racing fbcon or another client;
3. lets WPE's built-in DRM view import Mesa's device-proven linear DMA-BUF
   using GBM with `GBM_ALWAYS_SOFTWARE=true`;
4. uses the now-proven legacy CRTC/page-flip path exposed by WPE DRM;
5. waits for each page-flip event before recycling a buffer;
6. restores the recorded state and requires a full shell repaint; and
7. repeats normal exit, web-process crash, UI-process crash and forced timeout
   at least 100 times without a blank/stale display or leaked GEM object.

The controlled cycle, 20-cycle bounded stress, external presenter `SIGKILL`
recovery and 100 complete ownership sessions close the basic KMS ownership,
restoration and exported-object leak uncertainties. `brick-kms-watchdog.c`
now also builds as the installed supervisor child in restore-only mode: it
records display state before invoking the pause hook, keeps itself outside the
browser cgroup, restores the exact state, and leaves shell restart to the
supervisor. Before shipping, fault that packaged chain on the device, kill the
real WPE UI and init-supervised broker, and have a human or camera verify that
every shell return repaints without a blank or stale page. Do not mix explicit
atomic primary-plane control with legacy page-flip calls. Closing a KMS fd is
not a sufficient restoration protocol.

### evdev and WPE events

Open the discovered Brick gamepad with `O_CLOEXEC`, take `EVIOCGRAB`, and
release it by closing the fd. Kernel close-on-process-death makes a browser
crash recoverable. On `SYN_DROPPED`, ignore events through the next
`SYN_REPORT`, query current state with `EVIOCGKEY`, and reconcile held buttons.
`brick-evdev-input.c` implements and tests that boundary while emitting a
normalized event stream.

The WPE platform adapter should translate the normalized stream as follows:

| Brick control | WPE/browser action |
| --- | --- |
| D-pad | bounded pointer motion, accelerated only for repeat events |
| East | primary pointer button down/up |
| South | navigation back on press, never repeat |
| Mode | Enter key down/up |
| View/filter | vertical scroll with repeat |

The product must clamp the pointer to view bounds and include monotonic event
timestamps. The adapter must emit balanced releases after resynchronization or
focus loss, and tests must cover a button held during a crash.

The production-path launcher adapter now implements those mechanics directly
against WPEPlatform rather than stopping at a normalized log stream. On the
Brick it opened and exclusively grabbed the runtime-proven `TrimUI Brick
Gamepad` at `/dev/input/event2` with `O_CLOEXEC`; `/proc` showed that descriptor
only in the UI process. The adapter sends pointer/button/scroll/Enter events,
clamps coordinates to the current WPE view, ignores the complete
`SYN_DROPPED` interval, reconciles held East/Mode buttons with `EVIOCGKEY`, and
balances releases on input loss and teardown. Deterministic file-event tests
cover mappings, repeat and dropped-event ordering. The device has no
`/dev/uinput`, so automated real-kernel injection was not possible. A human
button/navigation matrix, reconnect with device-node renumbering (the current
adapter retries the qualified path), and held-button process-death test remain
release gates; P0.4 is therefore not yet a pass.

## On-screen keyboard design

Implement a WPE input-method-context subclass; do not synthesize DOM key
events from injected JavaScript. Show the OSK only when WPE reports editable
focus caused by user interaction. Track input purpose/hints so numeric, email,
URL and password fields receive suitable layouts. WPE 2.54 specifically fixed
`inputmode` propagation and programmatic-focus OSK behavior, making 2.54.1 or
later the minimum practical baseline.

Render the OSK in launcher-owned native composition, never in page-controlled
content or a second WPE view.
D-pad changes the selected key, East preedits on press and commits on release,
South deletes, View/Filter move the caret, and Mode closes it. Password text,
preedit and clipboard contents must never be logged or persisted. Clear all
composition state on navigation, network loss and browser exit.

Required OSK gates are password fields, `inputmode=numeric`, email/URL
layouts, backspace, caret movement, preedit/commit, non-ASCII UTF-8, fields in
iframes, refocus, zoomed pages and a crash while composing.

The unavailable stock-shell integration is no longer part of the design. A
launcher-owned second WPE view was also tested and rejected, rather than kept
as a fallback. Live WPE returned a null toplevel for that view, raised the
fullscreen assertion, created a second WebProcess and drove the browser family
to roughly 660 MiB. Authority cancellation then failed to terminate the UI
cleanly. This is rejected-spike evidence about the OSK architecture, not a
regression in the portal's now-proven first-frame path.

`BrickOskContext` now renders native ARGB SHM buffers and submits them through
the portal's one `WPEView`, so WPE DRM's one pending/committed buffer pair and
page-flip callback remain the only presenter. The launcher unmaps the Web
producer and waits a resettable 100 ms barrier before submitting. Patch 0004
drops pending Web buffers while unmapped and does not grant Web `FrameDone` for
launcher-owned OSK buffers. It never issues an independent atomic/page-flip
request or writes the committed BO. Persistent producer or rendering failure
restores portal visibility and fails the session. Password, preedit and
surrounding text remain in the trusted UI process and are never rendered.

Automated Brick qualification completed numeric, password, email, URL, UTF-8,
uppercase, symbol and same-origin iframe commits with controlled result length
1, plus a 100-action navigation hammer. No entered value was logged. One or
two already-submitted portal completions sometimes reset the barrier; none
reached the terminal bound. A bounded WebProcess-kill run restored the shell,
mode-0660 display nodes and `wpa_state=COMPLETED`. The test-only qualifier is
compile-time guarded because programmatic fixture focus legitimately receives
`INHIBIT_OSK`; the shipping launcher has no injection option.

The same UI process subsequently completed 100 full focus/show/hide ownership
sessions. It emitted exactly 100 visible and 100 hidden markers, retained one
WebProcess/NetworkProcess/GPUProcess throughout and reached 543,984 KiB peak
summed RSS (not PSS). Two late portal buffers reset the handoff barrier without
reaching its terminal threshold. Pre/post DRM state matched at zero clients,
zero named GEMs, 18 framebuffer inventory lines and stock plane framebuffer
58. Cleanup left zero browser/Bubblewrap processes, a live shell, root:root
mode-0660 display nodes and WPA `COMPLETED`.

Cross-origin iframe input also passed on the Brick. The controlled top-level
document was served from `192.168.7.1:18083` and embedded an absolute iframe
from the distinct origin `192.168.7.1:18082`; the qualification JavaScript
therefore could not inspect or focus the framed DOM. The qualifier sent normal
WPE pointer events at the full-screen framed editor and used its build-only
show seam to overcome the artificial programmatic-focus `INHIBIT_OSK`; its
press/release commit then caused only the foreign origin to record
`OSK_RESULT length=1`. The top-level launcher log remained on the parent URL.
One already-submitted portal buffer arrived after handoff and reset the
bounded quiet barrier at retry 1; the OSK still rendered and no permanent
render failure occurred. This proves focused-frame input-method delivery
across the origin boundary, while the physical-controller focus/show path
remains a separate release gate. It needs no new origin privilege, IPC,
injected script, second view or browser implementation.

Release gates now are the physical-controller matrix and visual
blank/stale-frame inspection. Failure blocks shipment and must be fixed in
this one launcher-owned implementation; it does not authorize a page-injected,
second-view or second-browser fallback.

## Sandbox and privilege boundary

Use WPE WebKit's built-in bubblewrap/seccomp sandbox for web, network and
storage processes. Do not wrap the whole launcher in a second ad-hoc bwrap
layer: the trusted UI process needs the display/input devices, and an outer
sandbox can prevent WebKit from constructing its tested child sandboxes.

Run the UI process as a dedicated unprivileged user. Grant only the display
broker/UI process access to `/dev/dri/card0` and the chosen evdev
node. All device and lease fds must be `O_CLOEXEC`; auxiliary WebKit processes
must not inherit them. Keep WebKit's dangerous sandbox-disable environment
variable unset. Do not expose an unrestricted D-Bus socket; retain
xdg-dbus-proxy if any portal API eventually requires D-Bus.

The primitive probe and actual unprivileged Buildroot Bubblewrap policy have
passed on Brick. `integration/brick-sandbox-build.fragment` records the
package selection, and `integration/brick-sandbox-device-test` reproduces the
device checks against a `/tmp` stage. The minimal-page run under WPE WebKit's
own sandbox with process and fd logging has passed on the Brick. Preserve
these conditions in the production image:

* user, mount and PID namespaces work in the deployed privilege model;
* seccomp filter installation and inheritance work;
* web/network child processes show distinct namespace IDs;
* the web process cannot open `/data`, Wi-Fi credentials, display or input;
* the network process has only the access WebKit intentionally grants;
* no test requires root, setuid bwrap, or disabling the WebKit sandbox; and
* killing every child type leaves no namespace helper, proxy or stale lock.

Kernel configuration is a prerequisite, not proof of this behavior.

### Actual WPE 2.54.1 process audit on Brick (2026-11-06)

The real single WPE DRM launcher, NetworkProcess and WebProcess were audited
while loading the controlled portal under the display-recovery watchdog. The
result is a **P0.5 fail**, despite the WebProcess Bubblewrap sandbox being
active:

* WebProcess had distinct mount/PID/network/UTS namespaces,
  `NoNewPrivs: 1` and seccomp mode 2. It did not inherit the authority,
  controller or OSK descriptors after the launcher marked them close-on-exec.
* WPE 2.54.1's upstream `bindOpenGL()` bind-mounted all of `/dev/dri` and
  `/dev/fb0`; WebProcess held three descriptors for `/dev/dri/card0`. The
  Brick exposes only `card0`, no DRM render node, and software GBM/DMA-BUF
  rendering requires this node. This directly violates the planned rule that
  only the broker/UI may open the primary node. Hiding it breaks the selected
  renderer, so a source allow-list alone cannot satisfy the rule.
* NetworkProcess was not Bubblewrap-confined in this configuration. Because
  the qualification launcher was root, it ran UID/GID 0 with the full
  effective capability mask, no-new-privileges disabled and seccomp mode 0.
  The UI process had the same root privilege. Production must run the complete
  launcher as a dedicated unprivileged identity before this can pass.
* The upstream graphics policy also exposes fbdev even though this product
  path uses DRM. A narrowly documented WPE packaging patch may remove fbdev
  and unrelated GPU device binds, but WebProcess `card0` isolation requires a
  real render-node solution (driver support or a separately proven rendering
  architecture), not merely a Bubblewrap edit.

The exact 7.3 Brick kernel source declares sun4i with `DRIVER_GEM |
DRIVER_MODESET | DRIVER_ATOMIC`, matching the runtime absence of
`/dev/dri/renderD*`. Adding `DRIVER_RENDER` by itself is not a valid fix:
softpipe's KMS/GBM allocator uses `DRM_IOCTL_MODE_CREATE_DUMB`, while this
kernel marks that ioctl as primary-node-only rather than `DRM_RENDER_ALLOW`.
Changing either contract expands an untrusted process's kernel attack surface
and needs its own allocation/ioctl audit and device validation; it is
unfinished support, not a harmless configuration toggle.

The bounded run restored CRTC 55/fb 58 at offset `(0,1536)`, restarted the
shell, and left no WPE/Bubblewrap process or DRM client. Multiple stacked
temporary bind mounts left by earlier watchdog-killed qualification runs were
explicitly unmounted and verified absent. No sandbox-disable environment
variable was used. The tested launcher binary SHA-256 was
`dda049149ac8e5ddc73ddc85b284a8c3955b29d61639326d18e5a69bbcba1037`.

### GPU-process and unprivileged follow-up (2026-11-06)

`ENABLE_GPU_PROCESS=ON` built and linked all three WPE children. The public
WebKit feature identifier is `UseGPUProcessForDOMRendering`; the generated
internal preference key has an `Enabled` suffix, but passing that suffix to
`webkit_settings_set_feature_enabled()` fails. The launcher now uses the
public identifier and fails closed if it disappears.

The controlled `/osk` page then ran as UID/GID 997 with zero effective
capabilities in UI, Network, GPU and Web processes. The authority fd remained
UI-only and NetworkProcess held no display or input fd. GPUProcess existed and
held three `card0` fds, proving that DOM GPU-process selection was active.
However, WebProcess also opened three `card0` fds and appeared as a separate
DRM client. Its 346,368 KiB RSS / 322,513 KiB PSS also showed that the feature
does not move the dominant DOM heap out of WebProcess. Aggregate PSS was
approximately 370,238 KiB (UI 19,111; Network 16,093; GPU 12,521; Web
322,513), still above the 256 MiB budget.

Those child DRM fds are not leaked UI descriptors. The upstream DRM display
opens its UI descriptors with `O_CLOEXEC` (`WPEDisplayDRM.cpp` and
`WPEDRMSession.cpp`), and the live fd numbers and DRM clients were distinct.
They are fresh opens made possible by upstream `BubblewrapLauncher.cpp` calling
`bindOpenGL()` only for WebProcess; that function bind-mounts `/dev/dri` and
`/dev/fb0`. In the split diagnostic run, where the sandbox was disabled only
because Linux forbids creating an unprivileged user namespace after `chroot`,
both GPU and Web independently reopened the host primary node. The earlier
real-bubblewrap run proves the same Web open through its bound `/dev/dri`.

The single production-path source fix is now built off-device:

* the trusted root supervisor wrapper opens `card0`, creates separate UI and
  GPU transports, clears close-on-exec for exactly the wrapper-to-UI exec,
  removes all supplementary groups, drops permanently to `brick-captive`, and
  sets no-new-privileges;
* a narrow WPEPlatform DRM API patch duplicates that trusted fd with
  `F_DUPFD_CLOEXEC`; the UI connects the DRM display from it, closes the
  transport fd before any child launch, and removes its environment marker;
* `brick-captive` is no longer in `video`, and card nodes are root-only 0600;
* the GPU transport remains close-on-exec in UI and ProcessLauncher maps a
  duplicate only into GPUProcess; its GBM device consumes a CLOEXEC duplicate,
  then closes the transport. Every child launcher first removes the inherited
  fd-number environment marker, and only GPU receives a fixed replacement;
  Web and Network receive neither a mapped descriptor nor a stale marker;
* a narrow Bubblewrap patch moves `bindOpenGL()` from WebProcess to GPUProcess
  for device discovery, leaving Web's synthetic `/dev` without DRM or fbdev.
  It also corrects the upstream launch topology: GPU and Network are forced
  through Bubblewrap, Network alone keeps the host network namespace, and only
  its trusted captive profile root is bound writable. The root-only 0600 node
  still denies a fresh GPU open: rendering uses only the explicitly brokered
  fd, so the bind does not act as a permission grant.

The fixed GPU transport survives the Bubblewrap hop by design rather than by
assumption. In the pinned Bubblewrap 0.13.0 source, both `close_extra_fds()`
calls are confined to the monitor and PID-1 parent branches, whose comments
state that other descriptors have already been passed to the child. The final
child applies seccomp and reaches `execvp()` without an fd walk. An executable
host probe against Bubblewrap 0.12.0 independently preserved fd 198 as
`/dev/null` across the same final-exec path. The target 0.13.0 source audit is
authoritative for the image; the probe guards against misunderstanding the
branching semantics.

Both upstream patches dry-run cleanly against 2.54.1; the patched WPEPlatform,
WebKit library and both launcher stages cross-build successfully. The 33-test
host portal/input/OSK/lifecycle/source-contract suite passes.

The exact live retest used a private mount namespace and `pivot_root`, not
`chroot`, so unprivileged Bubblewrap user namespaces remained available. The
tested WebKit and launcher hashes were `a9402743...` and `244ea522...`.
UI, Network, GPU and Web were all UID/GID 997, group-free, capability-free and
no-new-privileges. Network, GPU and Web were seccomp mode 2 in independent
user/PID/mount namespaces. Network retained the host network namespace by
design. Web and Network saw neither DRM/fbdev nodes nor fds. GPU saw the
root-only discovery nodes, received fd 198 through Bubblewrap 0.13.0, consumed
and closed it, and retained only three CLOEXEC `card0` descriptors. UI was the
only entry in the kernel DRM client table and the only master. The apparent
GPU marker in `/proc/PID/environ` is the immutable initial environment range;
fd 198 was absent after `unsetenv()` and GBM adoption. Non-GPU children had no
marker at exec.

SIGKILL of Web, Network or GPU removed that sandbox while the other roles
remained isolated; none restarted within the two-second observation window.
SIGKILL of UI removed every child immediately. Each cancel/crash path left no
WPE/bwrap process, namespace mount or DRM client and restored the shell,
root-owned 0660 graphics nodes and saved WPA connection. P0.5 therefore passes
for this single implementation. All roles share host UID 997 because the
already-unprivileged UI cannot switch to additional host UIDs; independent
user/PID/mount namespaces and seccomp are the role boundary.

The subsequent display-format spike closed the first-frame failure without a
second renderer or backend. A read-only live KMS probe recorded four planes.
The selected primary plane 47 exposes 20 formats, including linear `AR24`,
`AB24`, `BA24`, `RA24` and all four XRGB variants; stock/fbcon was actively
scanning linear `XR24` at 1024x768 from its 1024x2304, pitch-4096 allocation.
Instrumented WPE proved that the rejected object was instead `WPEBufferSHM`:
the upstream DRM backend returned FourCC zero for every non-DMA-BUF before it
attempted import. This transport is expected in the secured configuration.
The display engine has no render node, Web has no primary-node fd by policy,
and WPE's final Web compositor therefore uses immutable shared-memory buffers
even though DOM work is isolated in GPUProcess. Forcing the DMA-BUF compositor
would put card0 back in Web and undo P0.5.

`0003-WPEPlatformDRM-scan-out-shared-memory-buffers.patch` maps WPE's sole SHM
format to linear DRM ARGB8888, validates dimensions, source stride and byte
extent, allocates a GBM scanout object on the UI's brokered DRM device, copies
each row and attaches the framebuffer lifetime to the immutable WPE buffer.
The bounds-hardened final live tuple was WebKit `476a6d3c...`, inner launcher `a40cc6fe...` and
broker `3187c0f9...`; it emitted a real `FRAME`. UI remained the sole kernel
DRM client/master. Web and Network had no graphics fds, while GPU retained its
three CLOEXEC card descriptors. Killing Web left UI, Network and GPU alive.
On a static portal the complete browser family consumed one scheduler tick in
ten seconds and all ten one-second plane samples stayed on framebuffer 60, so
the copy occurs on submitted immutable buffers rather than as a polling loop.
Cleanup left no WPE/Bubblewrap process, namespace mount or DRM client, restored
root-owned 0660 graphics nodes, restarted `brick-shell` and retained the saved
WPA connection. The three patches apply cleanly to 2.54.1 and the 32-test host
suite passes.

## Per-network data isolation

Use the same stable network identity material as private-MAC policy:
SHA-256 of the exact wpa_supplicant SSID representation plus key-management
mode. Never use raw SSIDs or transient wpa network numbers as path names.

`brick-captive-supervisor` accepts only a precomputed 64-character lowercase
hash, hashes it once to the launcher profile ID, and creates independent
mode-0700 cache, configuration and data roots owned by `brick-captive`.
The WPE launcher must also explicitly construct its website-data store with
those directories; XDG variables alone are only defense in depth. Cookies,
local storage, IndexedDB, service workers, HSTS and cache must not cross keys.
The NetworkProcess Bubblewrap bind names only the current hashed profile, not
the parent containing every network.

Persist cookies for a known captive network so reconnects do not needlessly
repeat login. Disable browser password storage and autofill. “Forget network”
must atomically remove the corresponding browser data and private-MAC state
while disconnected. Enhanced MAC rotation remains suppressed for known
captive networks; changing identity without clearing/rebinding portal data is
not allowed.

Test two servers using the same host name on two isolated fixture networks:
neither cookie nor storage may appear in the other profile. Also test profile
deletion, power loss during deletion, symlinks, full disk, corrupt databases
and migration across firmware versions.

The controlled Brick matrix now closes the cookie/local-storage and
rename-interruption portions of this gate. Against one identical HTTP origin,
profile A persisted `cookie=A, storage=A` across a normal five-second
authority exit, while fresh profile B reported `missing, missing`. A third
profile persisted `A, A` after the UI process was killed with `SIGKILL` two
seconds after the controlled result. This required the supported
`webkit_cookie_manager_set_persistent_storage()` API with a SQLite file under
the profile data root; the API default correctly discarded the fixture's
original session cookie. The final fixture uses a non-secret `Max-Age`
persistent cookie so it tests the intended product behavior.

With both A and B populated, Forget Network for A was killed immediately
after its atomic profile-to-tombstone rename. The published profile was absent
and the durable intent/tombstone remained. Two recovery invocations removed
the tombstone, intent and private-MAC record idempotently. Reopening A reported
`missing, missing`; B still reported `B, B`. The transaction previously failed
to remove a rename-complete tombstone on recovery; that defect and mismatched
launcher/forget default profile roots are fixed. Host symlink rejection and
all published fault boundaries pass.

The remaining storage faults were then exercised on Brick without touching a
real profile or filling global storage. A private 64 MiB tmpfs constrained the
synthetic profile root to zero free blocks. A's live page retained its
in-memory value but its attempted SQLite files were zero length; after space
was restored, A reopened `missing, missing` and the pre-existing B still
reopened `B, B`. Random-byte corruption of A's cookie database was not repaired
by WebKit, so the launcher now performs a read-only SQLite
`PRAGMA quick_check(1)` before session creation and resets only the confirmed
corrupt current-profile database/WAL/SHM. Recovery produced `missing, A`, and
after reseeding A reopened `A, A`. WebKit itself rebuilt a separately corrupted
C local-storage database and retained `A, A`. A device-level symlink-root
substitution was rejected before mutation; interrupted Forget recovery again
left B at `B, B`. Literal power removal and cross-version profile migration
remain external/destructive qualification rather than being inferred from
`SIGKILL` or bounded fault injection.

## Captive state machine

The external connectivity checker is authoritative; page content and browser
exit status never declare the device online.

```text
lease obtained -> detecting
  online -------------------------------> close / resume shell
  captive -> opening -> interactive -> verifying --online--> close
     |          |             |              |
 network loss   launch fail   user cancel    still captive -> interactive
     +----------+-------------+--------------+-------------> limited/fallback
```

* CAPPORT's authenticated API and DHCP option 114 take precedence.
* HTTP interception may select the initial portal URL, but HTTPS certificate
  errors are never silently bypassed.
* Recheck after navigation completion, form submission, an explicit user
  action, or a bounded timer; debounce checks to avoid portal load storms.
* Close immediately on Wi-Fi disconnect or network identity change.
* Put a hard wall-clock limit on the whole session and provide a QR/URL/manual
  fallback.
* Block downloads, file upload/chooser, camera, microphone, geolocation,
  notifications, pop-under windows and non-HTTP(S) external schemes unless a
  separately reviewed product flow handles them.

`captive-fixture.py` now supplies controlled CAPPORT, HTTP redirect
loops/chains, hidden forms, POST, cookies, JavaScript redirects, terms,
vouchers, popup/iframe IdP, success without navigation, authority-forgery
content, 1 MiB/parameterized large bodies, chunked responses, bounded slow and
stalled responses, connection loss, animation, download, file-chooser and
permission requests. Its TLS mode is covered with locally generated valid,
untrusted/self-signed, wrong-host and expired certificates. The launcher sets
TLS policy to `FAIL`, rejects non-HTTP(S) navigation, cancels downloads and
file chooser requests, denies permissions, and never derives authority state
from page content. The 26-test host integration suite passes, including source
contracts for the public bounded-memory API and true first-displayed-frame
notification, plus a warning-clean build of the target authority metrics peer.

Portal presentation is a product-authority decision, not browser policy.
Auto-open only follows an immutable, generation-tagged snapshot for a
user-initiated join. A captive result from background auto-join or during
gameplay exposes a non-modal sign-in action instead. UI commands are
idempotent and generation-tagged; raw supplicant events never drive the shell
or WPE session directly. The eventual product authority needs deterministic
host tests for stale-generation rejection, duplicate commands, authority
restart and the user-initiated/background transition matrix.

This controlled corpus does not substitute for ten lawful representative real
portal implementations, real external IdP/MFA traffic or Wi-Fi loss/roam on
the deployed network. Those require network owners/test accounts and the RF
rig; they remain external qualification dependencies rather than unspecified
launcher behavior.

### Packaged-chain Wi-Fi invalidation evidence (2026-11-06)

`brick-captive-network-fault-spike` exercised the complete packaged
broker -> supervisor -> watchdog -> connectivity-authority -> WPE chain on
the A133 Brick against the USB-only `/forged-authority` fixture. The page
contained every authority-looking token, so page bytes could not accidentally
stand in for the private authority fd. The first case issued only
`wpa_cli disconnect`, observed authority result `exit-1` and
`authorized=0`, then issued `reconnect` and recovered the same saved network
ID 0. The second case left the real connection untouched and changed only the
harness's read-only `wpa_cli status` proxy from expected ID 0 to observed ID
1; it also produced `exit-1` and `authorized=0`. A fresh session after each
fault produced `success` and `authorized=1`. No `select_network`,
`remove_network`, `set_network` or `save_config` operation was used.

All four sessions removed their browser processes, runtime session marker and
per-session cgroup, restored the live stock shell, and converged to
byte-identical `clients`, `gem_names`, `framebuffer` and `state` debugfs
hashes. The two stale sessions peaked at 369,438,720 and 369,782,784 bytes in
the 352 MiB-high/384 MiB-max production cgroup and recorded no max, OOM or OOM
kill event. The two successful retries peaked at 293,654,528 and 293,687,296
bytes. Final status was `state=idle result=success`; WPA was `COMPLETED` on ID
0 at `192.168.68.70`, no WPE/Bubblewrap process, captive mount or session
cgroup remained, and graphics nodes remained root:root mode 0660.

The tested package tuple used outer launcher
`94d7799d77502aa3e5f5d2eba3de23629a60e08808b8ff79aa6cfc315df83f6f`,
current packaged inner launcher
`5117380b81aaa260eb471419319929b6bf20a8e6090c95d34da7883ef395256d`,
authority
`cef58556f134e1179f3743c1d7d87ded1e0cd0a347de06e2f8b3b4185714799d`
and watchdog
`bf2003b1623b498e0e897ba9ecfaf7de1e70e26449b01f2377a31e6e69177fcf`.
The qualification result file hash is
`11c261d55a2d2f1ff8d9c353defa4366d2c86feb2572f571c4fc0debee18053e`;
the matching before/after DRM evidence files both hash to
`165d10563839854f5b1af489ab6c8708436d38f9dcf622ebd190429816fad31d`.
After evidence collection the device was restored to its released inner
launcher `f0be971872d703ecf14c0b22110dccebcb58796dce778fc0155ea7f87df14cee`
and every qualification artifact was removed by exact path.

### Packaged 1,000-cycle and accelerated-soak evidence (2026-11-06)

The frozen final packaged tuple completed 1,000 consecutive broker ->
supervisor -> watchdog -> authority -> WPE sessions. It comprised 984 normal
authority-approved closes, ten generation-safe control cancels and six exact
single-WebProcess `SIGKILL` injections. Every 25-cycle checkpoint found no
browser process, runtime session marker or per-session cgroup, a running stock
shell, and byte-identical `clients`, `gem_names`, `framebuffer` and `state`
debugfs hashes. All normal sessions returned `success`; cancels returned
`exit-143`; WebProcess failures returned `exit-1`. Cycle checkpoint
MemAvailable ranged from 536,412 to 604,532 KiB and finished at 581,728 KiB,
so it did not decline monotonically. The isolated profile grew linearly from
43 to 6,063 KiB rather than accelerating.

The same process then held a static page for 120 seconds and a full-viewport
`/animate-full?frames=10000` page for 180 seconds, sampling every five
seconds. Excluding the launch sample, static cgroup charge stayed within
357,961,728--359,161,856 bytes and aggregate process-family RSS within
482,308--482,940 KiB. Animated charge stayed within
368,599,040--369,197,056 bytes and RSS within 500,708--501,352 KiB. Animated
peak was 370,012,160 bytes under the 402,653,184-byte hard limit, with 2,749
soft-high events and zero max, OOM or OOM-kill event. Both holds authorized,
removed their cgroup and restored the exact DRM baseline. Final MemAvailable
was 596,956 KiB and profile size 6,075 KiB.

This run exposed a real cancellation state-machine defect before the final
pass: the broker's session-wrapper signal trap forwarded termination and
exited without publishing `runtime/outcome`, so KMS/cgroup/shell recovery
succeeded but broker status remained `running`. The wrapper now publishes
terminal outcome 143 atomically after waiting for the forwarded child; a host
regression test and all ten on-device cancels prove the fix. The finalized
nonblocking control can legitimately report a transient busy lock, so the
qualification driver retries only that exact diagnostic for at most five
seconds; all other control errors fail closed. Fault sessions remain captive
until injection, preventing authority success from racing PID selection.

The frozen hashes were control
`6fcb664518a4f4a69b100430365d070629b4e424590118b07a1e5c40a8b480c5`,
broker `e58a4aefc10942d57f413e38def94b6a13676e340346ddf30a906fb8063cfb54`,
inner launcher
`5117380b81aaa260eb471419319929b6bf20a8e6090c95d34da7883ef395256d`
and outer launcher
`94d7799d77502aa3e5f5d2eba3de23629a60e08808b8ff79aa6cfc315df83f6f`.
The 1,000-row result hash is
`a91f8c63e8dcd7c26badc93acb2e4f58e63436df7f313ffea3baa3c0356c648c`;
before/after DRM files both hash to
`165d10563839854f5b1af489ab6c8708436d38f9dcf622ebd190429816fad31d`.
The Brick was then restored to released inner launcher
`f0be971872d703ecf14c0b22110dccebcb58796dce778fc0155ea7f87df14cee`,
WPA `COMPLETED` on ID 0, and every staged artifact was removed by exact path.
This accelerated gate does not replace the external 24-hour animated/idle,
camera-scored blank/flash/tear and battery-current qualification.

### Resource lifecycle evidence (2026-11-06)

On the A133 Brick, the single WPE 2.54 DRM/Mesa-softpipe implementation first
passed 100 consecutive normal authority-approved sessions through the KMS
watchdog: 100/100 status zero, p95 complete lifecycle 1.76 seconds, maximum
five browser-family processes and zero process or stage-mount leaks. Every
cycle restored the stock CRTC and shell; the profile remained 56 KiB.

The combined GPU-process build was then measured with the supported WPE 2.54
memory-pressure API (192 MiB per-process base, one-second polling), the
`DOCUMENT_BROWSER` cache model and a real first-displayed-frame callback. Ten
controlled static-page sessions all passed: p95 first frame 1.560 seconds,
peak aggregate RSS 445,344 KiB, peak aggregate PSS 382,895 KiB, peak anonymous
PSS 281,760 KiB, four processes and zero process or mount leaks. The 10.01
second post-frame static interval used 0.60% of one CPU core and PSS remained
exactly 360,562 KiB. Controlled large-body, animated and IdP pages peaked at
356,594, 367,610 and 364,635 KiB PSS respectively, so page simplicity does not
close the 256 MiB gate.

This is not shared-map RSS inflation. A representative static WebProcess used
312,343 KiB PSS: 264,120 KiB anonymous and 48,223 KiB file-backed. Its largest
maps were a 129,032 KiB private heap and seven anonymous ranges with about
17--19 MiB resident each. Memory-pressure relief ran every second at roughly
270 MiB WebProcess RSS but released zero bytes after settling. Disabling JIT
lowered three-session peak aggregate PSS to 299,746 KiB, still 43,746 KiB over
budget and unsuitable as an undocumented product policy. GPU DOM rendering
also left the WebProcess opening DRM and did not move this footprint solely to
the GPU process.

The resource runs invoked the same WPE libexec with WebKit's sandbox disabled
only after the production wrapper independently failed closed because this
kernel rejects unprivileged user namespaces from the staged chroot. They are
valid resource/display isolation evidence, not a sandbox pass. The public
memory limit and cache model therefore do not make 256 MiB physically
attainable in this build. A separate-tree `USE_SYSTEM_MALLOC=ON` build was
confirmed supported and its completed objects were preserved, but the build
was deliberately interrupted before deployment when headroom containment,
rather than an arbitrary allocator target, became the decision criterion.

The Brick kernel exposes a complete cgroup-v2 `memory` controller when a
private hierarchy is mounted: `memory.high`, `memory.max`,
`memory.oom.group`, `memory.peak`, events and `cgroup.kill` all work. Stock
userspace simply does not mount it. Exact idle measurements were 997,636 KiB
MemTotal and 603--611 MiB MemAvailable with the 105,256 KiB-PSS shell running;
stopping the shell raised MemAvailable to 710,556 KiB. The initial 256 MiB
budget was an assumption in the plan, not a limit derived from these numbers.

`brick-wpe-cgroup-memory-spike` proved a bounded policy with a 512 MiB
admission floor, 280 MiB soft high and 304 MiB hard maximum. Static and 1 MiB
large-body pages passed with first frames at 745 and 1,295 ms, peak charges of
293,982,208 and 292,990,976 bytes, and shell recovery in 879--880 ms with zero
process or mount leaks. A 256 MiB high/280 MiB maximum passed five static runs
(p95 first frame 2,525 ms, maximum 282,386,432-byte charge) but made the large
page time out after 15 seconds, so it is not compatible. A 256 MiB high/270
MiB maximum produced a deterministic whole-group OOM (status 137,
`oom_group_kill=1`, seven child kills) and restored the shell in 1,099 ms with
zero leaks. A bounded 160 MiB external allocation lowered MemAvailable to
439,544 KiB; the real 512 MiB admission floor rejected the session, and memory
returned to 609,640 KiB afterward. This originally made 280/304 MiB the
evidence-backed spike policy, but the final sandbox/GPU-process tuple below
invalidated that sizing result.

The installed `brick-captive-supervisor` now implements the requalified policy in
the sole captive launch path: it checks MemAvailable before pausing the shell,
enables `memory` and `pids`, creates a per-session domain, applies 352/384 MiB
high/max, `memory.oom.group=1` and `pids.max=64`, and moves the execing browser
leader into the domain before it can fork. Cleanup sends bounded process-group
TERM/KILL, invokes `cgroup.kill`, waits for the domain to empty, records
current/peak/event/process metrics, attempts domain removal, and runs the
display-resume hook on every path that attempted a pause. Failure to
drain/remove the domain or resume the display is a fail-closed status 125 when
the browser otherwise succeeded.

Production service ordering is a release requirement. The connectivity
authority must invoke the supervisor as root in the host mount namespace and
provide working display pause/resume hooks. Init should mount cgroup2 at
`/sys/fs/cgroup` before Wi-Fi service startup; for compatibility, the
supervisor can mount and later unmount it itself when absent. The hierarchy
must expose both `memory` and `pids`, and no process may be placed in the
intermediate `brick-captive` node before its subtree controllers are enabled.
The runtime and profile roots must be writable before launch, and the shell
service must already be available for deterministic resume. The WPE privilege
wrapper remains responsible for dropping browser privileges after the root
supervisor has established the resource/display boundary.

Remaining external P0.7 work is the 24-hour static/animated soak, a durable
hardware flip counter and camera/human scoring of shell returns. The bounded
failure, authority-termination, 1,000-update and concurrent-pressure matrices
below close their respective automated gates.

### Bounded failure and cancellation qualification (2026-11-06)

The pre-display-fix single-path tuple (`libWPEWebKit-2.0.so.1.11.4`
`feecb78c9b99f226c6e71d23cf8fff2b4a88109f2aa9a948b45c8d7371cfbfc0`,
inner launcher `21010fbd52db96c35e60eb31e5d78eb8fa41d48762b16df17d577a64b4951cad`)
completed 100 injected process failures: 25 each for the UI, Web, Network and
GPU processes. It also completed 100 authority-termination cycles, alternating
50 explicit `CANCEL` commands with 50 authority-channel owner deaths. Every
cycle used an exact process group plus a private cgroup-v2 domain; cleanup
found no WPE, Bubblewrap or proxy process, an empty domain, a live stock shell,
WPA `COMPLETED`, and byte-identical `clients`, `gem_names`, `framebuffer` and
`state` debugfs hashes. The minimum post-cycle MemAvailable values were
559,712 KiB for process failures and 557,040 KiB for cancellation. These are
failure-containment results, not a production memory-policy pass: the fault
domain deliberately had no memory ceiling so that every target process could
reach its injection point.

The same final tuple invalidates the earlier 280/304 MiB cgroup result. Peak
charges were 361,127,936 bytes in the process-failure run and 361,816,064 bytes
in the cancellation run. Repeating one bounded cycle with the exact
293,601,280-byte `memory.high` and 318,767,104-byte `memory.max` produced a
whole-session status-137 kill before `FRAME`, while still restoring shell,
WPA and the exact DRM hashes. A provisional 336/352 MiB pair passed one cycle
at a 353,222,656-byte peak and another with a separately tracked 64 MiB memory
holder at a 358,387,712-byte peak; the pressured run began with 535,292 KiB
MemAvailable and cleaned the holder by its exact PID. This is only a sizing
probe. The production policy must not be changed until this pair passes the
portal corpus, admission rejection, soak and concurrent-pressure matrix.

The corrected final tuple is `libWPEWebKit-2.0.so.1.11.4`
`f771556f08fa198cb6f5da27b7d38bb9f1b706db402174f4c6fc00931d66c805`
and inner launcher
`f0be971872d703ecf14c0b22110dccebcb58796dce778fc0155ea7f87df14cee`.
`WPEBufferSHM` import now allocates its trusted linear GBM buffer once but
copies the current shared pixels on every render submission; the previous
one-time import silently scanned stale pixels after buffer reuse. The launcher
also marks and focuses its public `WPEView`; fullscreen alone left it hidden
and WebKit throttled the producer after its initial static paint.

With those fixes, `/animate-full?frames=1000` completed 1,000 viewport-sized
DOM colour updates in 58,134 ms. Qualification-only `WPEViewDRM`
instrumentation reached matching 100-event milestones for render submission,
SHM copy, atomic commit, page-flip event and completed-buffer flip. The
software renderer legitimately coalesced intermediate DOM changes, so this is
not a claim that every JavaScript update became a KMS flip; it proves the
producer completed and every observed scanout traversed the complete transport
and DRM completion lifecycle without stalling.

The exact production supervisor then passed at 352 MiB `memory.high` and 384
MiB `memory.max`. The normal run charged 373,321,728 bytes at peak, recorded
572 soft-high events, no max/OOM kill, and a 54-process peak. A second run with
an exact external 67,108,864-byte holder (PID tracked outside the browser
cgroup, initial MemAvailable 535,748 KiB) peaked at 371,281,920 bytes with 508
soft-high events and no max/OOM kill. Both removed their session cgroup and
restored the shell and WPA; after the holder's exact-PID termination,
MemAvailable returned to 609,376 KiB. The hard-limit headroom was at least
29,331,456 bytes in these runs.

These two resource runs used the supervisor's supported direct-child cgroup
path. A preceding attempt through the staged external watchdog saved and
restored KMS correctly, but the watchdog could not `execvp()` the temporary
`/tmp/brick-pivot-root-exec` harness (`ENOENT`), so it launched no browser and
recorded a zero peak. The installed production image invokes
`/usr/bin/brick-captive-wpe` directly rather than that temporary pivot helper;
the zero-peak staged attempt is not counted as qualification evidence. The
packaged-chain composition below closes that separate smoke requirement.

### Packaged production-chain fault composition (2026-11-06)

The installed command topology was then staged unchanged through broker,
supervisor, external KMS watchdog, connectivity authority, privilege-dropping
launcher and the final `f771556f...` / `f0be9718...` WPE tuple. The outer
launcher opens the validated evdev node before dropping permanently to UID/GID
997 and passes only that descriptor to the UI. A normal authority-approved
session plus UI, supervisor and broker `SIGKILL` cases all restored the exact
baseline debugfs hashes for `clients`, `gem_names`, `framebuffer` and `state`.
Every case left no browser process or session cgroup, a free display lease,
root-owned mode-0660 graphics nodes, a live shell and WPA `COMPLETED`.

This composition exposed three integration races that direct engine tests did
not cover. The authority killed an already-authorized UI immediately after its
pre-cleanup `CLOSED`, racing exclusive-controller release; it now waits a
bounded second for natural exit first. Resume hooks started the long-lived
shell with the supervisor or recovery broker's display-lock descriptor still
open; the hook child now receives fd 9/8 closed while the parent retains the
lease. Finally, supervisor `SIGKILL` left its empty cgroup directory orphaned;
the broker now removes it only after acquiring the display lease, allowing a
surviving supervisor to win and perform its own cleanup without a removal
race. These are the production semantics exercised by the four-case result,
not qualification-only launcher hooks.

## Release gates

No-go if any mandatory gate fails:

1. **Engine:** all mandatory gates in `DECISION.md`; private Mesa
   softpipe/GBM is required, while stock PowerVR EGL and any second engine or
   presenter are forbidden.
2. **Display:** 1,000 frame updates plus 100 ownership/crash cycles on both
   chosen presenter and shell return path; no tear, blank page or leaked object.
   Current evidence covers 100 normal ownership sessions, 100 real WPE
   authority-approved sessions, one external presenter `SIGKILL`, and
   byte-identical exported DRM object/state snapshots, 100 injected WPE crash
   cycles and 1,000 full-surface DOM updates with matched real DRM lifecycle
   milestones; camera-scored visual quality remains external.
3. **Input/OSK:** automated translation, overflow resync, editable-field and
   password non-persistence gates pass on device. Physical repeat,
   disconnect/reconnect and held-key process-death handling still require a
   human-operated gamepad run.
4. **Sandbox:** actual Brick WPE UI, Network, GPU and Web processes pass the
   bwrap/seccomp/namespace/fd matrix as dedicated unprivileged users; display
   access is brokered only to GPU, credential paths are hidden, fault cleanup
   is verified and no sandbox-disable escape hatch is used. Re-run this audit
   from the final clean image.
5. **Isolation:** cross-network cookie/storage tests and forget-network
   deletion pass across normal exit, UI crash and injected interruption after
   the atomic rename. Bounded profile-only ENOSPC, corrupt cookie and corrupt
   local-storage recovery also pass without sibling leakage. Literal power
   removal and cross-version migration remain external/destructive
   qualification.
6. **Compatibility:** controlled fixture and a representative portal corpus
   cover simple terms, form/voucher, JavaScript, popup/iframe and external IdP;
   every failure has a usable QR/URL fallback.
7. **Reliability:** 24-hour portal animation/idle run, 1,000 open/close cycles,
   repeated Wi-Fi loss/roam and low-memory kill leave the shell and Wi-Fi
   controls usable. The bounded profile-only full-disk injection passes; a
   global-storage exhaustion test is intentionally not required because the
   profile has no reason to consume or own the entire persistent filesystem.
8. **Resources:** cold-start latency, peak RSS/PSS, CPU, copy bandwidth and battery
   current meet an explicit product budget measured on Brick hardware. This
   gate remains open for battery/long-soak product budgets: the literal 256 MiB
   initial budget is incompatible, while the 352 MiB soft/384 MiB hard cgroup
   policy passed normal and concurrent 64 MiB external-pressure qualification.

## Primary references

* WPE 2.54 platform/input architecture:
  <https://wpewebkit.org/about/architecture.html>
* WPE 2.54 release and input-method fixes:
  <https://wpewebkit.org/release/wpewebkit-2.54.1.html>
* Linux KMS/dumb buffers and plane/page-flip rules:
  <https://docs.kernel.org/gpu/drm-kms.html>
* Linux evdev `SYN_DROPPED` recovery contract:
  <https://kernel.org/doc/html/latest/input/event-codes.html>
* Linux seccomp filter contract:
  <https://kernel.org/doc/html/latest/userspace-api/seccomp_filter.html>
* bubblewrap namespace and mount isolation model:
  <https://github.com/containers/bubblewrap/blob/main/README.md>
* RFC 8910 CAPPORT DHCP/RA identification:
  <https://www.rfc-editor.org/rfc/rfc8910.html>
* RFC 8908 captive-portal API:
  <https://www.rfc-editor.org/rfc/rfc8908.html>
