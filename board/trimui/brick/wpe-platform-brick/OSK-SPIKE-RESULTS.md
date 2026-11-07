# Trusted same-view OSK spike results

## Decision

Use one implementation: the existing WPE 2.54 launcher owns a native,
launcher-rendered keyboard and submits its ARGB SHM buffers through the portal
`WPEView`. There is one `WPEToplevel`, one DRM primary-plane presenter and one
WebProcess. There is no page keyboard, second browser/view, stock-shell IPC or
independent KMS commit.

The second-view design was rejected on the Brick: its toplevel was null, the
fullscreen call asserted, it created a second WebProcess, and browser-family
RSS rose to about 660 MiB. The DRM backend also has only one pending buffer,
one committed buffer and one page-flip callback, so another presenter could
misattribute completion and release the wrong buffer.

## Producer and trust contracts

* Editable user focus drives a `WebKitInputMethodContext` subclass. It honors
  `WEBKIT_INPUT_HINT_INHIBIT_OSK`; purpose/hints select text, numeric,
  password, email and URL behavior.
* The UI process renders only constant key labels, layout purpose and
  selection. Typed, preedit, surrounding and password text are never drawn,
  persisted or logged. Portal DOM cannot modify the pixels or controller
  model.
* Show first unmaps the portal view, then waits a 100 ms quiet interval. A
  late non-OSK `buffer-rendered` resets that barrier. OSK buffers are fresh
  ARGB SHM buffers submitted by `wpe_view_render_buffer()` through WPE DRM's
  existing page-flip state machine; the committed BO is never modified.
* WPE patch 0004 drops Web producer buffers that reach
  `AcceleratedBackingStore::renderPendingBuffer()` while the view is unmapped,
  returns `FrameDone`, and filters external OSK buffers out of Web producer
  `FrameDone` accounting.
* At most three already-submitted portal completions are tolerated while the
  barrier re-arms. A fourth event, or four render failures, restores portal
  visibility, moves the OSK to hidden, emits `failed`, and fails the captive
  session. Dispose also restores visibility before disconnecting callbacks.
* The intentional unmap causes WebKit focus-out/reset notifications. They are
  ignored only while the trusted handoff is active; navigation and Mode use an
  explicit dismiss path. This prevents the OSK from immediately hiding itself
  without suppressing real lifecycle cleanup.
* D-pad navigates; East emits preedit on press and commit on release; South
  deletes; View/Filter move the caret; Mode dismisses. All rendered layout
  punctuation has a legible native glyph and space is blank.

Portal spoofing is bounded but cannot be eliminated on a single full-screen
plane: portal content may draw a keyboard look-alike while the portal is
visible. Once the trusted OSK handoff begins, portal frames no longer own the
screen and its DOM receives no OSK controller events.

## Evidence, 2026-11-06

Host/build:

* Production and compile-time qualification launchers cross-linked as AArch64
  PIEs against the pinned WPE 2.54.1 headers/libraries with
  `-Wall -Wextra -Werror`.
* The model test covers navigation, password/non-ASCII commit, purposes,
  actions, Shift, uppercase hints and every rendered label. The focused source
  contract suite passes 3/3. Patch 0004 applies cleanly to pristine 2.54.1.
* Patch 0004's tested library SHA-256 was
  `feecb78c9b99f226c6e71d23cf8fff2b4a88109f2aa9a948b45c8d7371cfbfc0`.

Brick:

* A passive single-view launch produced `FRAME` and `LOADED`, one WebProcess,
  and about 430 MiB browser-family RSS rather than the rejected second-view
  result.
* A build-time-only qualifier focused the controlled fixture and called the
  same `brick_osk_context_handle_button()` path as the controller. The stock
  kernel has no uinput. WebKit correctly marks programmatic focus
  `SPELLCHECK|INHIBIT_OSK`; only this non-shipping qualifier masks that bit.
* Numeric, password, email, URL, UTF-8, uppercase, symbol and same-origin
  iframe cases each navigated to a controlled result reporting length 1. The
  fixture printed eight result lengths and no entered value. A 100-action
  alternating-navigation hammer passed.
* Input commits sometimes completed one or two already-submitted portal
  buffers. Each reset the quiet barrier and the OSK completed; no run reached
  the bounded terminal threshold. This validates the barrier rather than
  proving the producer is instantly quiescent.
* One UI process then completed 100 full portal-to-OSK-to-portal ownership
  sessions, with 100 `VISIBLE`, 100 `HIDDEN` and one terminal
  `OSK_SESSION_STRESS_PASS 100` marker. The 509 samples saw exactly one
  WebProcess, one NetworkProcess and one GPUProcess; summed browser-family RSS
  peaked at 543,984 KiB. This is an RSS sum rather than PSS and therefore
  double-counts shared pages. WebKit's configured memory-pressure relief ran
  110 times without an OOM or process replacement. Two late portal buffers
  reset the quiet barrier at counts one and two; neither reached the bounded
  failure threshold.
* DRM debugfs before and after contained zero clients and zero named GEMs,
  18 framebuffer inventory lines and stock primary-plane framebuffer 58.
  Peak live DRM clients was one. After cleanup there was no WPE or Bubblewrap
  process, the shell was live, display nodes were root:root mode 0660 and WPA
  remained `COMPLETED`. The staged production launcher was restored to SHA-256
  `a40cc6fe1784fd19dd9847b6db9a8a6dcbca0b6ada26997341c582d270315a5b`.
* A WebProcess kill during the bounded OSK run left no WPE/bwrap process. The
  cleanup restored `/usr/bin/brick-shell --run`, root:root mode 0660 display
  nodes and `wpa_state=COMPLETED`.
* The test tuple was removed. Final staged hashes were restored to launcher
  `a40cc6fe1784fd19dd9847b6db9a8a6dcbca0b6ada26997341c582d270315a5b`
  and WPE library
  `476a6d3c5949fe7977ec08f72603e25dd2a17725c43bd1da1c7c1f54e43b18ff`.

## Remaining production gates

The architecture and automated device path are resolved. Cross-origin iframe
focus and commit delivery passed between two distinct controlled origins with
no extra privilege or IPC. Before release, run the physical gamepad matrix
(including repeat, disconnect and a held button at process death) and visually
confirm key labels/selection and absence of blank or stale frames.

## Primary references

* <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/UIProcess/API/glib/WebKitInputMethodContext.h.in>
* <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/WPEPlatform/wpe/WPEView.cpp>
* <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/WPEPlatform/wpe/drm/WPEViewDRM.cpp>
* <https://github.com/WebKit/WebKit/blob/webkitglib/2.54/Source/WebKit/UIProcess/wpe/AcceleratedBackingStore.cpp>
