# Captive WPE launcher spike

`brick-captive-wpe.c` is a minimal WPE 2.54 shell using the
`wpe-webkit-2.0` GLib API, not a
general browser. It deliberately delegates connectivity success to an
external authority and implements these boundaries:

* the authority supplies the initial HTTP(S) URI, an opaque stable
  per-network key, and a bidirectional file descriptor;
* the network key is SHA-256 encoded before constructing separate
  `$root/$hash/data` and `$root/$hash/cache` directories;
* `webkit_network_session_new(data, cache)` creates the persistent
  `WebKitWebsiteDataManager` for exactly that network;
* the sandbox bind marker names only `$root/$hash`, not the parent containing
  other networks. The NetworkProcess can therefore write its current data,
  cache and configuration without gaining filesystem access to sibling
  profiles;
* persistent cookies use `$root/$hash/data/cookies.sqlite`. Before creating
  the network session, the launcher runs SQLite `PRAGMA quick_check(1)` on an
  existing store. Confirmed corruption resets only that profile's database,
  WAL and SHM files; unrelated I/O errors fail the launch rather than deleting
  data. WebKit's local-storage database is independently contained in the same
  profile and recovered successfully by WebKit in the device fault probe;
* navigation permits HTTP, HTTPS and internal `about:blank`; allowed popup/IdP
  navigation is redirected into the one existing view, while non-web popups,
  unsupported MIME responses, downloads, credential-bearing URLs and all
  permissions are denied;
* page load events are observations sent to the authority. They never mean
  Internet access. Only an authority `SUCCESS` line exits successfully;
* `BrickOskContext` is a real `WebKitInputMethodContext` subclass. The launcher
  renders constant key labels and selection into fresh ARGB SHM buffers and
  submits them through the portal's existing `WPEView`; it creates no second
  view, WebProcess or KMS presenter. Entered, surrounding and password text is
  never rendered, stored or logged. Show unmaps the Web producer, waits a
  resettable 100 ms quiet barrier, then uses WPE DRM's buffer/page-flip state
  machine. Patch 0004 drops pending Web frames while unmapped and prevents OSK
  buffers from granting Web producer `FrameDone`. Persistent late frames or
  render failure restore the portal and fail the session.
* `--input-device` exclusively grabs the qualified Brick gamepad and injects
  bounded pointer, button, scroll and Enter events through WPEPlatform. It
  ignores the kernel-defined `SYN_DROPPED` interval, reconciles held buttons
  with `EVIOCGKEY`, and emits balanced releases on every teardown.

The launcher sets `WPE_PLATFORM=drm` before the first
`wpe_display_get_default()` call. For the live-proven Mesa software GBM route,
the supervisor must additionally export:

```sh
GBM_ALWAYS_SOFTWARE=true
GALLIUM_DRIVER=softpipe
GBM_BACKENDS_PATH=/usr/lib/gbm
```

`LIBGL_ALWAYS_SOFTWARE` must remain unset: WPE 2.54 uses it to suppress the
DRM device passed to the WebProcess, which forces an incompatible SHM buffer
on the direct-DRM backend. `GBM_ALWAYS_SOFTWARE` is the narrower Mesa control
that retains the DMA-BUF path qualified on the Brick.

The authority line protocol is intentionally tiny. The launcher sends
`READY`, `PAGE <uri>`, `LOADED <uri>` and `CLOSED success|failed`. It accepts
`SUCCESS`, `FAILED` or `CANCEL`. The descriptor should be a connected Unix
`SOCK_STREAM` or socketpair endpoint owned by the Wi-Fi connectivity service;
`--authority-fd` is mandatory so a page can never authorize itself.

`--network-key` means the authority's canonical opaque stable network identity,
not an already hashed directory name. The launcher hashes that identity
exactly once. Profile expiry/cleanup must call the same
`brick_captive_profile_id()` helper; passing its output back as a network key
would double-hash it and orphan the original profile.

The production root is `/data/captive-browser` in the launcher,
supervisor and Forget Network transaction. The root authority supplies the
canonical 64-character network identity; the supervisor and launcher both
derive the same single additional SHA-256 profile-directory component. The
supervisor assigns only that directory to the dedicated `brick-captive`
account before dropping privilege. Forget Network rename-deletes that exact
directory and recovery removes a tombstone left by interruption after rename.

## Tests and compile gates

Run the dependency-free host behavior model:

```sh
python3 -m unittest -v test_launcher_policy.py
```

`test-captive-policy.c` executes the actual shared C policy when GLib
development files are available. `meson.build` requires
`wpe-webkit-2.0 >= 2.54.0`; this intentionally prevents accidental compilation
against the repository's current Buildroot WPE 2.50.5 package.

The source has been audited against the exact 2.54.1 archive API headers and
MiniBrowser construction pattern. A compile/link result still requires the
generated public headers and libraries from the in-progress 2.54 Buildroot
build. This launcher has one display implementation: WPE 2.54's built-in DRM
platform with the narrow producer-isolation/linear-SHM patches and Mesa
softpipe/GBM. Automated device qualification covers OSK focus and commits;
runtime gates remain: physical controller delivery, TLS error UX, profile
cleanup/expiry, and external authority disconnect/reconnect behavior.

## API references

The implementation was checked against these primary 2.54 sources:

* WPE MiniBrowser's `wpe-webkit-2.0` network-session, WebView/display
  construction and toplevel setup:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Tools/MiniBrowser/wpe/main.cpp>
* WPEPlatform display selection (`WPE_PLATFORM`) and extension loading:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/WPEPlatform/wpe/WPEDisplay.cpp>
* `WebKitInputMethodContext` subclass contract:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/UIProcess/API/glib/WebKitInputMethodContext.h.in>
* WPE view visibility API used for the single-primary-plane handoff:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/WPEPlatform/wpe/WPEView.cpp>
* WPE DRM view presentation and primary-plane ownership:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/WPEPlatform/wpe/drm/WPEViewDRM.cpp>
* DRM display registration under the `drm` extension name:
  <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/WPEPlatform/wpe/drm/WPEDisplayDRM.cpp>
