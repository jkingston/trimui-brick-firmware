# Brick Wi-Fi productionisation plan

## Outcome and fixed decisions

The production feature is one integrated Wi-Fi stack, not a collection of
fallback implementations:

* the checksum-pinned published Brick kernel, with XR829 limitations handled
  by bounded userspace policy for the MVP;
* wpa_supplicant 2.12 as the sole association, security and roaming engine;
* one Brick connectivity service owning RF state, DHCP lifecycle, reachability,
  private-address policy, roam verification and captive-session authority;
* one captive engine: pinned WPE WebKit 2.54.1's DRM WPEPlatform backend with
  the four bounded integration patches, over Buildroot-selected Mesa
  softpipe/GBM with the proprietary `brick-gpu` package disabled; and
* the existing shell as the only user-facing settings and status surface.

There is no NetSurf fallback, custom WPEPlatform backend, no-EGL WebKit fork,
or driver-format workaround. A mandatory failure blocks the feature or
disables the affected optional policy.

The local kernel repository's forced-background-scan and TX-flush work remains
a separate kernel experiment on a newer kernel base. It is not part of this
MVP: the clean product image pins the published `brick-7.3.0-1` archive. A
future driver optimisation must pass kernel review and device qualification,
be published as its own checksum-pinned kernel release, and only then update
the base firmware. This preserves the kernel/base/product repository boundary
and avoids silently mixing an experimental driver into a userspace release.

Product policy is fixed as follows:

1. **Wi-Fi off** means WLAN RF soft-blocked, interface down, supplicant and
   DHCP stopped, address and routes removed. It does not claim the shared
   XR829 WLAN/Bluetooth rail is unpowered.
2. **Private address** defaults to one locally administered unicast address
   per SSID plus security type. It remains fixed for the saved profile and is
   deleted on Forget Network. An optional enhanced mode may rotate only before
   association, only for open non-captive networks, when the address is over
   24 hours old or its lease has expired and the profile has been unused for
   over four hours. Secured and known-captive profiles remain stable.
3. **Background scanning** is adaptive. Start with
   `bgscan="simple:30:-68:300"`; do not continuously scan. The final threshold
   and intervals are accepted only after the Phase 0 power/RF matrix.
4. **Roaming** preserves DHCP state only across two BSSIDs proven to be the
   same ESS and L2 domain. Association is not success: the controller verifies
   the AP/gateway path. Coordinated installations may offer continuity after
   802.11r/k/v qualification. Independent APs receive best-effort roaming with
   rollback/reconnect, never a zero-outage promise.
5. **Captive success** comes only from the connectivity authority after an
   independent Internet check. Web content cannot mark itself successful.
   CAPPORT API/DHCP option 114 takes priority; HTTP interception is discovery
   only, and TLS failures are never bypassed.
6. **Captive state** is isolated by stable network identity. Forget Network
   atomically deletes Wi-Fi credentials, private-address metadata, cookies,
   cache and website storage. Unsaved captive data expires after 30 days of
   non-use; saved profiles persist until forgotten.
7. **Initial security scope** is open networks and WPA2-PSK/CCMP. WPA2
   Enterprise and WPA3 are not advertised or selectable until a separately
   approved expansion matrix passes. Transition-mode APs must either negotiate
   the supported WPA2 mode cleanly or fail without wedging the radio.
8. **Internet classification** checks authenticated CAPPORT metadata first.
   Without CAPPORT it requires gateway reachability, successful DNS resolution
   and the expected response from a product-owned HTTP connectivity endpoint.
   The endpoint URL is a build-time setting with an operational owner, two
   independent serving locations and a documented outage behavior; redirects
   mean captive, never Internet.

## MVP production cut (2026-11-07)

The product MVP is implemented in the selected architecture, not as another
spike or fallback. `brick-wifi-authority` is the root-only `SOCK_SEQPACKET`
policy boundary; `S45wifi` remains its narrow radio/supplicant/DHCP mechanism;
and the shell uses the same picker from Settings and the in-game pause menu.
The authority owns persistent on/off, scanning, open and WPA2-PSK/CCMP joins,
saved reconnect, auto-connect, stable per-network private addresses, atomic
Forget delegation, connectivity classification and captive launch. Commands
carry generation and request IDs; stale generations cannot mutate state and a
per-client replay returns its prior result. Credentials are bounded, zeroed
after use and sent to supplicant only over its control socket.

The composed Buildroot graph selects the authority, shell, WPE captive chain,
TLS-capable curl probe and CA bundle. Host coverage includes 63 shell tests,
four authority protocol/integration tests and 43 focused service,
connectivity, persistence and private-address tests. On the attached Brick the
production shell retained its authority connection; off persisted, stopped
supplicant and soft-blocked WLAN; on restored WLAN, set
`ignore_old_scan_res=1`, and reconnected the saved WPA2 network in two seconds.
Runtime evidence also closed three downstream-specific integration gaps:
`WPA-PSK WPA-EAP` saved-profile encoding, `[SAE-H2E]` as a capability appended
to a WPA2-PSK suite, and `-W` preventing pre-monitor control commands. The
service deliberately starts control processing while WLAN remains blocked,
sets stale-scan policy, attaches the event monitor, prepares profile MACs and
only then unblocks RF.

This is the shippable MVP boundary. WPA3, Enterprise, address aging/rotation,
zero-outage roaming promises, automatic interruption of gameplay for network
intent and aggressive background scanning remain excluded. The Phase 5 cycle,
multi-device, long-soak, power and clean-image fault gates below are release
qualification, not a second implementation decision.

### Attached-device MVP deployment evidence (2026-11-07)

The packaged MVP was staged into the attached Brick's volatile root filesystem
and exercised against the normal `quietnet_Guest` AP and a temporary host AP
using the same SSID/security on a different channel and L2/DHCP domain. This is
runtime qualification, not durable image installation: rebooting the stock
RAM root restores its prior contents; the composed product image remains the
durable delivery mechanism.

Measured results were:

* WLAN off/on preserved the independent Bluetooth rfkill/function and the
  running `bluetoothd` and `bluealsa` processes, and restored the saved WPA2
  association in two seconds;
* XR829 associated to the second BSSID in about 100 ms, but a retained lease
  from the first AP was unusable on its different L2 domain. Consequently the
  product diverges from lease-preserving same-profile behavior: every
  unqualified BSSID change replaces DHCP. Lease preservation is permitted only
  after a pair has explicit same-L2/gateway-MAC qualification;
* XR829/supplicant can complete a forced direct BSSID roam without delivering
  the disconnect action. The authority therefore also observes BSSID changes
  and requests DHCP renewal independently of the event script. With that fix
  deployed, the DHCP PID changed, the `10.42.0.59` lease and working gateway
  path were established one second after the direct roam;
* with a valid `10.42.0.59` host-AP lease and working gateway traffic, taking
  that AP offline restored the router BSSID, its `192.168.68.70` lease and
  gateway traffic in four seconds; and
* the bounded recovery path restarts the WLAN service when station-level
  disconnect/scan/reconnect cannot clear a firmware epoch. It deliberately
  never resets the shared XR829 hardware or Bluetooth function. Bluetooth
  daemons remained present through these tests; an actual streaming A2DP sink
  is still required for coexistence qualification.

The host AP profile and copied credential were deleted after the test and the
host was restored to its original Wi-Fi connection. The Brick finished on
`quietnet_Guest` with a working gateway route. Its supplied DNS servers did not
answer from this network, so the authority correctly reported `limited` rather
than falsely declaring Internet access. Captive launch remained fail-closed in
that state and created no browser request.

## Phase 0: remaining risk-closing spikes

These are the only spikes still required. Each produces device logs, exact
build/module hashes, test scripts and a written pass/fail decision. They do not
introduce a second product path.

### Experiment execution rules

Every new spike starts with a one-page experiment contract: the product
decision it will resolve, the stock/custom-firmware/runtime references, the
metric and threshold, the cheapest falsifying test, required hardware, and the
exact device restoration check.  Evidence is collected in this order:

1. source/configuration audit against the pinned versions;
2. host unit, fixture and executable-probe tests;
3. incremental cross-build of only the changed module, library or launcher;
4. bounded live-device test with pre/post state and artifact hashes; then
5. a clean full-image build only at a milestone integration gate.

Builds run through one checked Buildroot SDK wrapper.  It supplies the host
tools and sysroot, records toolchain/source/patch/CMake hashes, rejects missing
or empty generated files, and writes a machine-readable manifest beside the
artifact.  For WebKit it also chooses parallelism from measured available
memory and swap activity, reducing jobs before the host reaches 2 GiB available
or sustained swap churn rather than assuming the CPU count is the limit.
Independent hypotheses use separate output directories but share
downloads and the persistent compiler cache.  Never run two builds against one
output tree, two memory-heavy WebKit builds on this host, or two sessions on the
single Brick.  Parallel work is limited to research, fixture preparation and
host tests until the heavy build or device lease is released.

CI retains the known-good dependency-complete SDK plus keyed kernel modules,
WPE engine variants and packages.  Ordinary iterations deploy only the changed
artifact; a compile-time engine decision is built asynchronously before a
device session is booked.  Device automation must capture radio, process,
mount, DRM, shell and network state, run the bounded test, collect logs, restore
the exact previous hashes/modes, and refuse to release the lease if any cleanup
check fails.  The two-AP/attenuator and inline-power fixtures are named lab
dependencies rather than open-ended software work.

### P0.1 Roam policy and forwarding recovery

Build a repeatable two-AP attenuated-RF rig with packet capture on the station,
both APs and gateway. Run at least 100 handoffs in each direction for:

* same SSID/security and coordinated bridge;
* independent APs sharing L2 but no mobility coordination;
* different L2/DHCP domains; and
* 802.11r enabled, then 802.11k/v individually and together where supported.

Add temporary driver debugfs evidence for the final departure frame: subtype,
firmware confirmation, ACK result and latency. Prototype the controller rule:
retain IP, emit ARP announcements, probe local AP and gateway, roll back to the
last known-good BSSID within a bounded window, then cool down the failing
BSSID pair. Prove that a different L2 always runs DHCP.

Pass criteria:

* no driver/firmware wedge, leaked station, lost route or DHCP teardown on a
  verified same-L2 roam;
* coordinated-AP handoff has p95 IP outage at most 1.5 seconds and maximum at
  most 3 seconds across 100 handoffs;
* an uncoordinated handoff either restores gateway traffic or begins rollback
  within 3 seconds of association, and returns to a usable path within 5
  seconds; and
* failed candidates enter cooldown and cannot cause ping-pong.

If 802.11r or k/v fails, leave that optional feature disabled. Do not block
basic best-effort BSSID selection.

### P0.2 Security compatibility and firmware recovery

Exercise open, WPA2-PSK/CCMP, WPA2/WPA3 transition mode, wrong credentials,
unsupported cipher, AP disappearance during JOIN and malformed/deauthentication
storms. Reproduce the observed
transition-mode JOIN wedge and implement a bounded driver recovery sequence.

Pass criteria: every failure returns scan/association capability without a
reboot; recovery never reuses stale keys; saved WPA2 remains functional; any
unsupported security mode is rejected clearly rather than retried forever.
WPA3 remains out of the advertised feature set unless this matrix passes.

### P0.3 Power, scan policy and Bluetooth coexistence

Use a logging external power analyser. Repeat the matrix in
`WIFI-BATTERY-SPIKE.md` at least three times per cell: PS off/on, idle and
traffic, 300-second and 30-second scans, strong/threshold/weak RSSI, screen
on/off, and Bluetooth idle/controller/audio states. Capture PM bytes,
firmware PM indication, CQM crossing, scan timestamps and SDIO-WUP counters.

Pass criteria:

* power save produces a repeatable connected-idle reduction without DHCP,
  multicast, wake, roam, controller or download failures;
* healthy-link scanning adds no more than 0.1 mAh per hour over a no-scan
  connected baseline;
* p95 interactive RTT regression is at most 25 ms and no test loses more than
  0.1% of packets outside an intentional roam; and
* WLAN off/on does not break an active Bluetooth controller.

Runtime SDIO-WUP gating is optional. Implement it only if instrumentation
shows material remaining idle cost and the wake/recovery fault matrix passes.

### P0.4 Real controller input and OSK

Replace the launcher OSK logging seam with the real shell IPC and connect the
qualified evdev translation to WPE events. Test pointer motion, click, scroll,
back, page navigation, focus traversal, repeat, `SYN_DROPPED`, controller
disconnect/reconnect and held-button browser death. Exercise text, numeric,
email, URL and password inputs, preedit/commit, deletion, long values and
non-ASCII text.

Pass criteria: every portal operation is possible without touch or a physical
keyboard; OSK appears only for editable focus; passwords never enter logs or
persistent preedit state; loss/resync cannot generate a stuck key; shell input
ownership is restored after every exit and crash.

### P0.5 WPE sandbox and privilege boundary

Run the real UI, Web and Network processes from the clean image and record
UID/GID, namespaces, mounts, seccomp state and every inherited fd. Verify that
Web content cannot open the Wi-Fi control socket, credentials, input devices,
fbdev or DRM primary node. Only the display broker/UI process may own DRM;
network access is restricted to what login compatibility requires. Fault each
process and the broker independently.

Pass criteria: no sandbox-disable escape hatch; no credential or control fd
leak; WebProcess has no device access; process death leaves no namespace,
mount, process, DRM client or input grab; init restores the shell even if the
display broker itself dies.

Spike status, 2026-11-06: the unprivileged GPU-enabled run proved that the
public feature identifier is `UseGPUProcessForDOMRendering`, but GPU DOM alone
does not isolate DRM: GPUProcess and WebProcess each independently opened
three `card0` fds. UI display fds are `O_CLOEXEC`; upstream WebProcess
`bindOpenGL()` is the exposure path. The one production design is now fixed:
root creates separate UI/GPU broker transports, permanently drops to the
group-free captive UID, UI connects through a narrow WPEPlatform `connect_fd`
API, and ProcessLauncher maps the second transport only into GPUProcess, where
GBM consumes and closes it. Card nodes remain root-only, and the upstream
Bubblewrap bind moves from Web to GPU for discovery, not permission. The live
audit also exposed that upstream only enables Bubblewrap for Web and explicitly
trusts Network; the same patch now enables it for GPU and Network, preserves
host networking only for Network, and binds only its declared captive profile
root. Both patches and launcher stages cross-build and the 26-test host suite
passes.

The fixed GPU descriptor is not lost inside Bubblewrap. The pinned 0.13.0
source closes extra descriptors only in the monitor and PID-1 parent copies;
its final-child path explicitly retains unknown inherited descriptors through
the final `execvp()`. A host executable probe against Bubblewrap 0.12.0 also
preserved fd 198 across its namespace setup. The live 0.13.0 GPU sandbox then
consumed fd 198, closed that transport, and retained only three CLOEXEC GBM
descriptors.

Live result, 2026-11-06: **P0.5 pass** for the single patched path. UI,
Network, GPU and Web ran as UID/GID 997 with no supplementary groups,
capabilities or privilege regain. Network, GPU and Web each ran with seccomp
mode 2 and distinct user, PID and mount namespaces. Web and Network had no DRM
or fbdev node, fd or environment marker. GPU alone received the broker fd and
root-only graphics discovery mounts; UI was the only DRM client and master.
Killing Web, Network or GPU left the other isolated roles intact; killing UI
removed every child, namespace, mount and DRM client. Normal/cancel and crash
paths restored the shell, root-owned 0660 device nodes and the saved WPA
connection. The roles retain one host UID because an already-unprivileged UI
cannot switch to additional host UIDs; their independent user/PID/mount
namespaces and seccomp filters are the enforced role boundary.

### P0.6 Portal, TLS and reachability corpus

Extend the fixture to valid, expired, wrong-host and self-signed TLS; redirect
loops; chunked/large/slow responses; terms, voucher and JavaScript forms;
cookies/local storage; popup/iframe/external IdP; success without navigation;
DNS-only and gateway-only failures; and Wi-Fi loss/roam during login. Test at
least ten representative real portal implementations where lawful.

Pass criteria: no TLS bypass, download, file chooser, device permission or
external-scheme escape; required IdP navigation stays in one controlled view;
authority state cannot be forged by the page; every incompatible portal has a
usable URL/QR/manual fallback; connectivity classification has no false
Internet result in 1,000 controlled transitions.

### P0.7 Resource and display lifecycle

Measure WPE cold/warm start, time to first frame, peak/steady RSS, CPU, frame
submission after a static page, storage growth and energy. Run 1,000 frame
updates, 100 normal sessions, 100 injected UI/Web/Network-process failures,
100 forced timeouts and a 24-hour static/animated portal soak. Use camera or
human observation to score blank, stale or torn returns.

Initial release budgets:

* p95 cold start to first frame at most 5 seconds;
* admit only with at least 512 MiB MemAvailable, then contain the complete
  browser family with cgroup-v2 `memory.high=280 MiB`,
  `memory.max=304 MiB`, `memory.oom.group=1` and `pids.max=64`;
* a static page stops continuous flips and settles below 5% of one CPU core;
* no leaked process, mount, fd, DRM object, profile lock or input grab; and
* zero unrecovered blank/stale shell returns.

The display-format risk is now closed in the one production backend. Live KMS
enumeration showed that primary plane 47 accepts linear ARGB8888 and XRGB8888,
while instrumented WPE identified the rejected transport as `WPEBufferSHM`
with FourCC zero rather than an unsupported hardware format. The secured Web
process cannot use DMA-BUF directly because this display engine has no render
node and Web is intentionally denied card0. A narrow WPEPlatform DRM patch now
validates and copies immutable ARGB8888 SHM frames into a linear UI-owned GBM
scanout object. The exact patched build reached `FRAME` with the P0.5 fd and
namespace matrix unchanged. A static portal held one framebuffer for all ten
one-second samples and used one aggregate scheduler tick in ten seconds. This
is the existing DRM backend's missing input transport, not a fallback browser
or presenter. The remaining P0.7 soak/fault/visual gates below still apply.

### P0.8 Clean-image integration and persistence faults

Build and boot a clean production image rather than the `/tmp` stage. Verify
Mesa and stock PowerVR libraries never collide, WPE children inherit only the
approved environment, service ordering is deterministic, and Wi-Fi-off state
survives reboot. Inject power loss/full disk during credential save, private
MAC metadata update, DHCP lease update, captive profile cleanup and Forget
Network.

Pass criteria: boot always reaches a usable shell; partial writes retain the
last valid state; Forget Network is eventually complete and idempotent; no
secret appears in logs or world-readable files; rollback image remains
bootable. Produce an SBOM, license bundle and vulnerability/update policy for
WPE, Mesa, supplicant and crypto dependencies.

## Production implementation sequence

### Phase 1: land and qualify the radio foundation

1. Convert the background-scan and TX-flush driver spikes into reviewable
   patches with KUnit/source-contract tests where practical.
2. Record stock/custom-firmware references, register/command encodings and the
   device evidence for each divergence, following `AGENTS.md`.
3. Keep background scan behind a default-off module/config gate until the
   Phase 0 RF matrix passes, then enable it in the product configuration.
4. Land the wpa_supplicant dedicated-MAC persistence patch and add upstream
   regression coverage.
5. Run scan, association, suspend/resume, module reload, Bluetooth coexistence
   and 24-hour traffic soaks on the final kernel/module hashes.

Exit: security matrix passes, no firmware wedge, and all selected driver
features are enabled deliberately rather than by probe parameters.

### Phase 2: make one connectivity authority

Replace shell-script coordination with one supervised C/GLib service. `S45wifi`
becomes only its init wrapper; it must not run an independent policy state
machine. The service owns this state machine:

`OFF -> STARTING -> ASSOCIATING -> LOCAL -> CAPTIVE|INTERNET -> ROAMING`, with
explicit `BACKOFF` and `STOPPING` transitions.

Responsibilities:

* serialize on/off, connect, forget, roam and captive operations;
* keep RF blocked until enabled profiles receive their private-MAC policy;
* preserve DHCP only for verified same-profile/same-L2 roaming;
* debounce CAPPORT/DNS/HTTP checks and expose reasoned connectivity state;
* launch exactly one captive session and hold the bidirectional authority fd;
* atomically persist versioned metadata and expire unsaved captive data; and
* emit structured, rate-limited diagnostics with SSID, credentials, URLs,
  cookies and MAC addresses redacted or hashed.

The shell IPC is a product-state API, not a wpa_supplicant event stream.  Each
publication is one immutable snapshot carrying a monotonically increasing
authority generation and revision.  Commands include the generation they were
issued against, have a stable request ID, and are idempotent: replay returns the
same accepted/result state, while a stale generation returns a refresh-required
result without mutating the newer state.  A subscriber either receives a whole
snapshot or reconnects and requests the latest one; it never merges partial
events.  The public outcome vocabulary is fixed to `Off`, `Turning on`,
`Connecting`, `Checking`, `Connected`, `Sign-in required`, `No internet`,
`Reconnecting`, plus stage-specific failures carrying a stable reason and
allowed recovery actions.  Association remains an internal fact and can map
only to `Checking`, never directly to `Connected`.

The logical network picker is also authority-owned data.  It groups compatible
BSSIDs into one network identity, orders current first, then saved-and-in-range,
then new networks, and includes a list revision.  The shell freezes that order
while focus is moving; a newer revision is applied only at a safe focus
boundary.  Per-network commands are `Connect`, `Disconnect`, `Retry`, `Sign
in`, `SetAutoConnect`, `SetPrivateAddress`, `GetConnectionInfo` and atomic
`Forget`.  Private address means the stable per-profile policy above, not a new
address on every BSSID.

Join intent is explicit input to `Connect`.  A captive result may auto-open the
single WPE session only when that generation originated in a user-initiated
join and the shell is in an interruptible context.  Background auto-join and
gameplay publish `Sign-in required` with a deferred, non-modal `Sign in`
action; they never seize input or the display.  The authority, not the page or
shell, consumes the one-shot auto-open eligibility so a restart or replay
cannot open it twice.

Exit: deterministic transition tests, concurrent-command tests, daemon
restart at every state, stale-generation/replayed-command tests, picker focus
stability tests, user/background captive-intent tests, and 1,000
on/off/connect/forget cycles pass.

### Phase 3: production captive session

1. Install the pinned `brick-wpewebkit` package and Mesa softpipe/GBM runtime
   from the normal Buildroot graph, with `brick-gpu` disabled.
2. Install an init-supervised display broker derived from the qualified KMS
   watchdog; remove qualification-only fault flags from the shipped interface.
3. Integrate controller input, OSK IPC, authority protocol, network-scoped
   WebKit storage, timeout/cancel and QR/URL fallback.
4. Enforce navigation, permission, MIME, download, popup and TLS policy in the
   launcher; keep WebKit's Bubblewrap/seccomp sandbox enabled.
5. Restore the exact stock CRTC state and force a shell repaint on every exit.

Exit: P0.4 through P0.8 pass on the clean image.

Build iteration is part of maintaining this path. The normal product build
downloads the checksum-pinned AArch64 WPE SDK release and compiles only the
small Brick launcher. `tools/archive_wpe.py` is the release-engineering path:
it records the upstream source, patches, toolchain, Buildroot configuration and
per-file hashes, and emits both binary and corresponding-source archives. Only
a WPE source, patch, toolchain or ABI change should invoke the multi-hour source
build and publish a new SDK revision. Never run two `make` processes against
one output tree.

Use the following bounded milestones in order. `make O="$out" configure`
regenerates the exact product configuration and profile fragments. Then
`make O="$out" captive-archive-check` verifies and extracts the pinned binary
SDK but deliberately performs no compile. The compatibility alias
`captive-patch-check` performs the same check. Only a milestone integration
gate may run the package build and final image; use `make release` for the
clean release tree rather than reusing an experiment output.

Do not mistake an extracted/patched tree or a directly cross-linked launcher
for an image. A trustworthy image requires the complete Buildroot dependency
graph, package staging/target installation, post-build processing and
`release-check`. If the selected output lacks those dependency stamps, stop at
`captive-archive-check`; it is an archive-integrity gate, not an image.

The package now installs the engine, privilege-dropping launcher, bounded
session supervisor, connectivity authority, exact-KMS watchdog, display
hooks, control CLI and init-respawned broker. That complete chain passed
normal, UI-`SIGKILL`, supervisor-`SIGKILL` and broker-`SIGKILL` runs on the
Brick. The remaining clean-image milestone must prove these same packaged
paths from the generated image rather than a staged qualification tuple.

The final bounded SDK milestone was repeated on the retained
`/tmp/brick-wpe-build` tree after all four package patches were present. Its
WPE `ninja -n` contained only forwarding-header generation, the library
symlink and the four expected child/injected-bundle relinks. The real relink
took 0.93 s and `brick-wpewebkit-rebuild`, including staging/target install and
all production integration binaries, took 12.23 s. The resulting uninstrumented
`libWPEWebKit-2.0.so.1.11.4` is
`f771556f08fa198cb6f5da27b7d38bb9f1b706db402174f4c6fc00931d66c805`;
the inner launcher is
`5117380b81aaa260eb471419319929b6bf20a8e6090c95d34da7883ef395256d`.
Every direct ELF dependency resolves in that target, and neither the engine
nor production binaries contains the qualification trace/OSK/frame strings.

That retained tree is deliberately not an image milestone. It has 98 of 157
non-virtual selected package install stamps; 25 host and 34 target package
stamps remain. Its selectively assembled target has neither `/etc/inittab` nor
the base `S01seedrng` init script, so `post-build.sh` correctly stops before it
can install the broker respawn line. Its old Buildroot package file-list
snapshots are also absent, so the selective rebuild cannot produce a trusted
package ownership manifest even though the expected files and display-hook
symlinks are present in `target/`. The shared release download cache already
contains 145 of 150 declared archives; the other five entries are generated
archives for in-tree Brick packages, not upstream downloads. Thus the next
the initial image proof had to use a fresh output tree rather than repairing
this partial one.

Run the base platform/package proof once, from this firmware directory, with:

```sh
make RELEASE_O="$PWD/output/release" release
```

The base `release` target refuses an existing `.config` or `local.mk`,
regenerates `brick_defconfig`, completes the dependency graph and image, then
runs `release-check`. This proves the captive package and platform, but it is
not the complete product image because `brick_defconfig` does not select
`BR2_PACKAGE_BRICK_SHELL`.

The clean base milestone completed on 2026-11-07. It exposed and closed three
cold-build defects: the WPA patch now has version-stable context, the SCP
package selects host Python XZ support required by the stock firmware ZIP's
LZMA members, and the captive launcher is built from a
`POST_INSTALL_STAGING` hook after WPE's generated headers, pkg-config file and
library exist. All 8,481 WPE Ninja edges and the image completed. Build time
was about 3 h 3 min across the diagnosed resumes. The generated image's five
published hashes verify; `release-check` stops only at its clean-revision gate
while the implementation remains uncommitted.

The complete product proof must be run from `brick/custom-firmware` with both
Buildroot externals and a fresh output:

```sh
RELEASE_O="$PWD/output/release" mise run release
```

The 2026-11-07 composed milestone completed from a fresh output in 2 h 25 min
42 s of build/image work, reusing only the declared download directory and
20 GiB Buildroot ccache. The shared-cache totals after this run were 25,113
hits from 54,223 cacheable calls (46.31%); 26,648 further calls were
uncacheable. A clean output therefore still recompiles many generated WebKit
unified sources and host tools. Keep clean product builds as release gates;
use the keyed dependency-complete SDK/package artifact described above for
ordinary iteration. Its one additional missing archive was Buildroot's
declared `libqrencode-4.1.1.tar.gz`; after fetching it through the package
source target, SHA-256
`5385bc1b8c2f20f3b91d258bf8ccc8cf62023935df2d2676b5b67049f31a049c`
verified and the complete composed `source` target found no other missing
archive. The product build completed all 8,481 WPE edges, Brick shell, seven
libretro cores, RetroArch, fonts, QR encoder, SCP, bootloader, kernel and final
SD image. In addition to `release-check`, accept the image
only if the Buildroot package file list is non-empty, generated `inittab`
contains exactly one
`::respawn:/usr/libexec/brick-captive-broker daemon` entry, the display hook
symlinks resolve to `brick-captive-display`, and the production binaries pass
the no-qualification-string check. The composed CPIO passes those checks,
contains root-owned mode-0755 `brick-shell`, `qrencode` and the complete
captive chain, contains the dedicated `brick-captive` user/group, and has no
missing direct ELF dependency. Its published image hashes all verify;
`release-check` again stops only because the firmware and UI externals are
dirty. A release candidate therefore requires committing the reviewed source
first and rerunning the exact composed command above from a nonexistent output
directory. Do not represent the 12-second cached package rebuild as a clean
image build.

### Phase 4: shell UX

Provide:

* a globally visible Wi-Fi toggle with `Off`, `Starting`, `On`, `Captive` and
  actionable error states;
* saved/available networks, signal and security, Connect/Forget, and a
  per-network Private Address toggle;
* an explicit best-effort roaming description—never a continuity promise on
  arbitrary APs;
* captive-login launch/cancel/retry plus manual URL/QR fallback; and
* progress that reflects authority state, not merely supplicant `COMPLETED`.

All UI actions must be idempotent and survive shell or authority restart.
Accessibility, localization, long SSIDs, hidden networks and non-UTF-8 scan
data are release requirements.

### Phase 5: release qualification

Run the complete matrix on at least three production Bricks and the supported
AP/security corpus. Required gates:

| Gate | Required evidence |
| --- | --- |
| Build | Reproducible image, hashes, SBOM, licenses, rollback boot |
| Toggle | 1,000 cycles; no stale process/address/route/rfkill state |
| Association | 1,000 saved and fresh joins; bounded recovery from every injected failure |
| Roaming | P0.1 thresholds and no DHCP loss on verified same-L2 handoff |
| Privacy | Stable per-profile MAC, boundary-only rotation, atomic forget/expiry |
| Captive | P0.4-P0.7 corpus, fault and soak gates |
| Security | Sandbox/fd audit, TLS rules, secret-redaction review, dependency scan |
| Power | External-meter policy acceptance with Bluetooth coexistence |
| Reliability | 72-hour mixed Wi-Fi/roam/captive/suspend soak and low-memory/full-disk faults |

Any mandatory gate failure is fixed in the selected architecture and rerun.
It does not activate an alternate browser, renderer, network manager or roam
engine.

## Delivery order

The shortest risk-aware path is:

1. run P0.1-P0.3 in parallel with P0.4-P0.6;
2. run P0.7-P0.8 once those paths are integrated;
3. land Phase 1 and Phase 2 behind default-off product flags;
4. land Phase 3 and Phase 4, then enable the feature only in qualification
   images; and
5. complete Phase 5 before enabling Wi-Fi/captive login by default.

P0.1, P0.2, P0.4 and P0.5 are correctness blockers. P0.3 determines the
default power/scan settings. P0.6-P0.8 are release blockers. None requires
reconsidering the single selected WPE implementation.
