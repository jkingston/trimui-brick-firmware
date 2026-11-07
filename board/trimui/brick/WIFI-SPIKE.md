# TrimUI Brick Wi-Fi spike

The consolidated implementation sequence, remaining qualification spikes and
release gates are in `WIFI-PRODUCTION-PLAN.md`.

Tested on 2026-11-05 and 2026-11-06 with Linux `7.3.0-brick-dev`,
wpa_supplicant 2.12 and
the attached production Brick.  The host's unused AX200 was used only as a
controlled WPA2/CCMP access point (channel 1 for the initial scan tests and
channel 11 for the two-BSSID roaming tests).  USB Ethernet remained the
recovery path.  SSIDs, BSSIDs and credentials from the surrounding network
are intentionally omitted.

## Declared and measured capability

The running XR829 integration advertises 2.4 GHz 802.11b/g/n, one spatial
stream, HT20, MCS 0-7, managed and monitor modes, at most two scan SSIDs, and
no scheduled-scan or scan-randomization feature.  The official XR829 product
brief declares 2.4 GHz b/g/n and HT20/HT40.  HT40 is therefore unfinished
driver support rather than a measured hardware limit.  It was not enabled in
this spike.

The current service reliably associates to WPA2/CCMP and DHCP works.  Five
baseline stop/start cycles all reassociated in 2-3 seconds.  Baseline stop
terminates wpa_supplicant and DHCP and clears the address, but leaves `wlan0`
up.  The service spike now writes the WLAN rfkill sysfs state as part of
stop/start; it does not require an `rfkill` utility.  Tested from `/tmp` on the
Brick, stop changed state/soft from 1/0 to 0/1 and dropped `UP`; start changed
it back to 1/0 and restored the original network, base address and DHCP lease
within seven seconds.

This is a real RF soft block, but not a proven rail power-off.  The module and
SDIO device remain present and SDIO runtime power still reports `unsupported`.
The normal driver stop path performs a firmware station reset but does not run
the MMC power sequence.  That power sequence asserts WLAN reset and gates two
shared radio clocks only for MMC power-off/reset; supplies and clocks are
shared with the XR829 Bluetooth function.  Product UI may accurately label the
new operation Wi-Fi off, but should not claim the XR829 rail is off without a
separate current measurement and Bluetooth coexistence test.

## Private address result

wpa_supplicant's dedicated per-ESS mode (`mac_addr=3` plus `mac_value`) works
with XR829.  Two locally administered addresses were applied while
disconnected; each associated and obtained a distinct DHCP lease.  A live
address change while associated correctly failed with `EBUSY`.

wpa_supplicant 2.12 parses `mac_value` but its text-file writer omits it, so
`SAVE_CONFIG` used to persist `mac_addr=3` with an all-zero value after
restart.  `patches/wpa_supplicant/0001-config-file-persist-dedicated-network-MAC.patch`
writes the value.  The cross-compiled binary was tested on the Brick: save,
terminate, reset the interface to its base address, restart, associate, and
confirm the same private address.  The normal profile and base address were
then restored.

Recommended product policy:

* Default to a persistent, locally administered unicast address per network
  profile (SSID plus security type), with an explicit per-network “private
  address” toggle.
* Never rotate an active connection.  Generate or rotate only before JOIN,
  then run DHCP again.
* Preserve the address for captive and secured networks.  This avoids
  repeated portal login and DHCP/parental-control breakage.
* If enhanced rotation is offered, use it for open, non-captive networks and
  follow Android's connection-boundary precedent: rotate when the address is
  over 24 hours old, or after the DHCP lease expired and the network has been
  unused for over four hours.  Keep timestamps in separate profile metadata;
  wpa_supplicant should consume, not invent, the address.

`usr/libexec/brick-wifi-private-mac` now spikes that policy with atomic
per-profile metadata keyed by SSID plus key-management type.  It supports
`device`, `private` and `enhanced` modes, generates all 46 available random
bits while forcing local/unicast, records address creation, lease expiry and
last disconnect, suppresses enhanced rotation for secured or known-captive
profiles, and refuses to alter the currently connected network.  Rotation is
performed only by `prepare`, immediately before a new connection.  Forgetting
a profile deletes its metadata.

The utility was exercised against a disabled in-memory profile through the
Brick's real wpa_supplicant control socket.  It applied dedicated-per-ESS mode,
kept the first address, rotated it at 24 hours, retained the rotated address
after marking the profile captive, and selected the device-address mode on
request.  The original connection remained completed with its base address;
the test profile and metadata were not saved.

The service integration now closes the automatic-selection race with rfkill.
For a fresh start it keeps WLAN soft-blocked, starts wpa_supplicant and its
event monitor, prepares every enabled network through wpa_supplicant's own
configuration parser, publishes the event-generation token, and only then
unblocks RF.  A preparation failure stops both processes and leaves RF
blocked.  This avoids duplicating the supplicant text grammar and makes
association impossible while an address is being changed.

That exact sequence was run on the Brick with temporary metadata and
`SAVE_CONFIG` disabled.  Supplicant initially reported the radio soft-block,
the enabled saved profile received a new locally administered address, then
associated and reached `COMPLETED`.  Stopping the spike and starting the
installed service after unblocking its older rfkill-unaware implementation
restored the original profile, base address and completed connection.

The DHCP hook now records the active profile's lease expiry.  A supplicant
disconnect event records last-disconnect time, and a captive classification
marks the profile captive without changing its active address.  Those events
complete the metadata needed for connection-boundary enhanced rotation;
changing an associated interface remains incorrect and is deliberately
rejected.

## Associated scan and roaming result

The starting driver rejected every associated scan with `EBUSY`.  The XR829
wire ABI and the reference xradio driver both define forced background scan;
the latter explicitly says foreground scan must not be used while joined.
The reference also warns that scanning after JOIN but before association can
confuse firmware.

The opt-in driver spike therefore:

* accepts scanning only when both joined and associated;
* emits exactly background type plus force-background flag;
* validates the exact serialized `0x41` type/flag byte;
* retains the joined-but-not-associated rejection;
* uses the reference 15-35 ms active and 50-110 ms passive dwell times;
* refuses active scanning where the channel's regulatory power differs from
  the station's firmware-confirmed power; and
* reports background beacon/probe-response copies to the scan cache while
  preserving normal station RX handling.

The capability is guarded by the read-only module parameter
`allow_background_scan` and remains off by default.

Measured on the Brick while associated on channel 6:

| Test | Result | Link effect |
| --- | --- | --- |
| Passive channel-1 scan | 9 cached/fresh BSS entries during development | 0/30 ping loss; 172 ms maximum |
| Directed active channel-1 scan with cache flush | controlled AP found at -30 dBm | association retained |
| Active 13-channel scan with cache flush | 560 ms, 7 BSSes on channels 1/6/11 | 0/50 ping loss; 554 ms maximum |
| `wpa_cli scan` integration | `OK`, 8 fresh results including controlled AP | 0/40 ping loss; 631 ms maximum |
| Automatic `bgscan="simple:5:-20:10"` | 3 logged 13-channel active batches at 5.52 s intervals in a 22 s window | association stayed `COMPLETED`; 0/100 ping loss; 466 ms maximum |

An RX-routing regression test after the spike was tightened returned 11 fresh
BSS entries from a 13-channel associated scan with no new station-decoder
rejection messages.  Background beacon/probe-response copies now enter the
scan cache without being decoded a second time as ordinary station traffic.

The automatic row used an intentionally aggressive five-second interval to
exercise the scheduler, not as a product recommendation.  It proves that
wpa_supplicant's bgscan module repeatedly reaches the XR829 forced-background
command without manual scan requests and without losing the link.  A phone-like
policy should use a much longer steady-signal interval and shorten it only
below an RSSI threshold.  The test module and temporary profile setting were
removed, and the installed stock module was reloaded afterward.

The controlled host AP was then bridged onto the same Ethernet/L2 network as
the existing consumer-router AP, using the same SSID and WPA2/CCMP security.
This provided two BSSIDs on channels 11 and 6 while retaining USB Ethernet as
the recovery path.  The NAS Wi-Fi module was also configured as an AP, but it
was outside usable XR829 RF range at the Brick and was restored without being
used for timing claims.

Associated scans found both usable BSSIDs, and `wpa_cli roam` selected either
one when the scan cache was fresh.  Measured radio reassociation took
0.20-0.34 seconds.  A host-AP-to-router-AP handoff delivered all 120 continuous
pings, with a 1043 ms maximum RTT as queued traffic drained.  The reverse
direction was asymmetric: one ordinary handoff lost about 25% of pings and
produced an approximately three-second visible outage even though association
completed quickly.

The first explicit-disconnect experiment exposed two independent delays.  The
service treated every transient supplicant `DISCONNECTED` event as a complete
network loss: it stopped `udhcpc`, removed the address and route, then waited
for a new DHCP exchange after association.  Repeated cycles showed radio
association complete while all IP paths remained absent for over 4.5 seconds;
another cycle reacquired its lease in 270 ms.  That variable DHCP path
accounted for some apparent forwarding outages and is not appropriate for a
same-profile BSSID roam.

`S45wifi` now gives a disconnect a five-second grace period.  If `CONNECTED`
returns on the same profile while DHCP is still alive, it retains the address,
route and lease and sends two RFC 5227-style ARP announcements.  A different
profile still deconfigures and starts DHCP normally, while a genuine link loss
does so after the grace period.  Thirty focused service, private-address and
connectivity tests pass.  Live pinned-BSSID cycles confirmed that the address
and default route now survive the handoff.

That fix revealed a real second phase.  In one ten-handoff run, association
usually completed in about two seconds, but nine transitions needed another
9-12 one-second gateway probes before data returned.  On a delayed
router-to-host handoff the Brick could ping the host AP immediately while ARP
to the gateway received no reply.  Flushing the client neighbor entry did not
help.  Gratuitous ARP request plus reply repaired one occurrence but not the
others.  A 0, 250 ms, 1 s or 3 s pause between departure and reconnect merely
reduced the post-association part of the delay: service returned at roughly
the same 12-13-second point after leaving the old AP.  This is measured
station/forwarding ageing, not a benefit from dwelling longer.

The driver also lacked mac80211's `.flush` callback.  mac80211 queues the
departure frame and calls the non-dropping flush specifically before station
teardown, but without the callback the core returned immediately.  The spike
now fences firmware-owned TX slots for both dropping and non-dropping flushes;
software TXQs are already purged by mac80211 in the dropping case.  It was
cross-built against the running 7.3.0 configuration, loaded on the Brick and
survived ten handoffs.  Before this fix the controlled host AP could retain an
authorized, inactive station for over 113 seconds after the Brick had moved;
afterward the host station entry was gone immediately on departure.  This is a
driver-ordering correctness improvement, but the A/B still produced several
9-12-second gateway outages, so it does not make independent AP forwarding
coherent.

For polished roaming, keep the associated-scan work but make scans adaptive,
not continuous: a long steady-signal interval, a short interval only below an
RSSI threshold, and an immediate scan for link degradation or user-requested
network UI.  A fresh candidate cache is the use case; constant off-channel
scanning wastes power and creates avoidable latency.  Add hysteresis and a
minimum dwell time to suppress ping-pong.  On managed installations use APs
sharing the same L2 network and mobility configuration, then qualify 802.11k
neighbor reports, 802.11v transition hints and 802.11r fast transition as
separate features.  FT reduces authentication time; coordinated AP forwarding
is what addresses stale delivery state.  For arbitrary independent APs,
expose a best-effort handoff rather than promising phone-like continuity.

The product controller should verify the replacement path rather than equate
`COMPLETED` with a successful roam.  Probe the local AP/gateway immediately;
if the gateway is still unavailable after a short bounded window, reconnect
the previous known-good BSSID and cool down that BSSID pair.  ARP announcement
is worth retaining because it repaired one measured occurrence and is cheap,
but it is not a correctness guarantee.  Keep IP/DHCP state only when both
BSSIDs are verified to be the same ESS and L2 domain, otherwise reconnect as
a network change.  The next driver diagnostic should expose the subtype,
firmware confirmation, ACK result and latency of the final departure frame in
debugfs so AP-specific failures can be separated from acknowledged departures.

wpa_supplicant is already built with `CONFIG_BGSCAN_SIMPLE` and
`CONFIG_IEEE80211R`.  An attempted NetworkManager transition-mode hotspot
(WPA1/WPA2/SAE) reached 802.11
authentication but stalled before association and left XR829 firmware unable
to scan or retry; restarting wpa_supplicant did not recover it.  Reloading the
module and narrowing the AP to RSN/WPA2-PSK/CCMP recovered immediately and
associated.  This makes mixed/transition security and failed-JOIN recovery a
required compatibility test before enabling automatic roaming broadly.

Battery policy and its measurement limits are detailed in
`WIFI-BATTERY-SPIKE.md`.  The safe first scan controller is
`bgscan="simple:30:-68:300"`: five minutes while healthy, 30 seconds below the
threshold, with an immediate weak-signal crossing rather than continuous
scanning.  Runtime power save was off on the installed system.  The first
`iw`-only test was later shown to be a false positive: the old driver never
sent firmware PM.  The instrumented, corrected path recorded a matching
firmware indication for normal-PS bytes `01,00,c8,00` and delivered 100/100
gateway pings at 100 ms intervals with 0.237/0.303/0.399 ms
minimum/average/maximum RTT before PS was restored off.
`WIFI-BATTERY-SPIKE.md` records the driver fixes, module hash and
Bluetooth-controller coexistence run.  These validate transition and traffic
correctness, not idle savings; the AXP2202 exposes no current channel, so
idle/scan energy still needs an external meter.  Runtime SDIO WUP gating
remains an unimplemented driver opportunity separate from 802.11 power save.

## Captive portal result and design

On the isolated host AP, the Brick associated using WPA2/CCMP, obtained
`10.42.0.127`, installed route and DNS, resolved an external probe name, and
reached the gateway.  Forwarded HTTP was blocked and timed out, producing a
measured “connected, DNS available, no internet” state.  The host firewall was
not broadened.

A loopback-only SSH tunnel over the USB recovery link then exposed controlled
responses to the target's actual BusyBox wget.  HTTP 204 returned success with
an empty body; a 302 was followed automatically but both 302 and final 200
headers remained in `wget -S` output; direct HTML returned success with a body;
404 returned failure.  This validates response parsing, not an end-to-end
captive network.  A real captive AP or second narrowly controlled AP is still
needed to validate interception and post-login reprobe over Wi-Fi.

The image now selects curl 8.22.0 with mbedTLS 3.6.7 and the Buildroot CA
bundle.  The initial probe-only build disabled proxy, cookie, WebSocket and
extra-protocol support.  Its stripped runtime closure was 1,992,736 bytes
before filesystem compression:
curl, libcurl, the three mbedTLS libraries, two 5,688-byte curve helpers and
the 180,744-byte CA bundle.  This deliberately replaces the unsuitable
BusyBox internal TLS path, whose configuration help states that it does not
validate peer certificates.

The captive-browser build enables libcurl cookie and proxy support because
NetSurf configures both APIs even for a direct connection.  Reusing the older
probe-only library made `CURLOPT_COOKIE` fail with curl status 48; rebuilding
libcurl from the updated configuration fixed navigation.  WebSocket support
and unused protocols remain disabled.

The runtime was copied to `/tmp` and exercised on the Brick.  A public HTTPS
204 endpoint passed certificate and hostname validation.  A self-signed
endpoint and a valid endpoint requested under the wrong hostname both failed
closed with curl status 60; a missing CA file failed with status 77.  With the
network's working resolver selected, independent HTTP and HTTPS probes both
returned authenticated empty 204 responses and classified `online`.  With the
DHCP-advertised but non-responsive DNS servers, both probes timed out during
resolution and classified `limited`, not captive.  This is a useful real-world
case where association, gateway reachability and public-IP reachability were
all healthy but name service was not.

`usr/libexec/brick-wifi-connectivity` now implements the transport and
classification spike.  It never follows redirects, caps response bodies,
accepts online status from an authenticated empty HTTPS 204, distinguishes
DNS/connect/timeout/TLS failures, and atomically publishes state under
`/run/brick-wifi`.  Controlled device-side tests classified an HTTP 302 as
`captive/http-intercept` and a non-empty HTTP 200 page as
`captive/http-content` while HTTPS was unavailable.

BusyBox `udhcpc` accepts numeric `-O 114` and exports unknown received options
as hex (`opt114`).  The service requests it, and
`usr/share/udhcpc/default.script.d/50-brick-wifi` now decodes printable option
114 values, accepts only HTTPS API URIs or the standardized unrestricted URN,
records lease metadata and starts the classifier without blocking DHCP.
The classifier requires TLS and `application/captive+json`, parses only a
unique top-level boolean `captive` member, preserves valid JSON for the UI and
falls back to probes on invalid API data.  A locally trusted HTTPS CAPPORT
server was tested end to end on the Brick: `captive:true` produced
`captive/capport-api`, and `captive:false` produced `online/capport-api`.

### Captive-login renderer spike

The product selects exactly one implementation: pinned WPE WebKit 2.54.1,
its built-in DRM WPEPlatform backend, Mesa softpipe/GBM and the four bounded
integration patches recorded in `wpe-platform-brick/DECISION.md`. NetSurf,
the custom shared-memory/fbdev WPEPlatform experiment and the no-EGL WebKit
patch are not build options or fallbacks. Their experiments remain evidence
only.

The stock PowerVR stack cannot support this route: a live Brick probe obtained
an EGL display handle but `eglInitialize()` failed with `EGL_NOT_INITIALIZED`.
Mesa 26.1.8 closed the decisive hardware uncertainty.  With
`GBM_ALWAYS_SOFTWARE=true`, softpipe rendered a 1024x768 XRGB8888 GBM surface,
exported its linear DMA-BUF, and re-imported it with the same
`GBM_BO_IMPORT_FD_MODIFIER | GBM_BO_USE_SCANOUT` contract used by WPE's DRM
view.  The 120-frame probe measured about 48.5 fps and 64 MiB maximum RSS.  The
stripped private Mesa and libdrm runtime is 17.79 MiB.  These measurements
qualify the buffer path; they do not substitute for the full WPE runtime gate.

KMS ownership is separately device-proven: controlled set/page-flip/restore,
forced presenter `SIGKILL` recovery, and 100 complete ownership sessions all
restored the original shell framebuffer without leaked DRM objects. Real WPE
pixels, the auxiliary-process sandbox, same-view trusted OSK and bounded
memory policy are also device-proven. The remaining product gates are the
final clean-image audit, physical input/visual and long reliability matrices,
real captive-portal compatibility, two-AP roaming, and external battery
measurement. Failure of any gate blocks the feature rather than activating
another renderer.

NetSurf 3.10 was useful as a discarded comparison: its Brick-native fbdev
experiment proved forms, cookies, basic JavaScript and controller input at
about 10-11 MiB RSS, but its 2020 engine creates unacceptable long-term portal
compatibility risk.  Its product configuration and patches have therefore
been removed.

Use this order in the product:

1. Consume DHCP option 114 and RFC 8908 Captive Portal API JSON when present.
2. Otherwise run independent HTTP and HTTPS probes.  Classify expected empty
   success as online, redirect/HTML as captive, inconsistent success as
   partial connectivity, and DNS/timeout/refusal as no internet—not captive.
3. Keep the network connected while the login UI is open.  Prefer a small
   isolated browser process; if a full on-device browser is too expensive,
   show the portal URL and QR code for a phone.  Re-probe after login.
4. Do not rotate the private address for a captive profile unless the user
   explicitly asks to forget/refresh it.

## References and divergences

* Android MAC behavior:
  <https://source.android.com/docs/core/connect/wifi-mac-randomization-behavior>
  supplies the persistent/non-persistent policy and rotation thresholds.  We
  use it as product precedent, not as evidence about XR829.
* Android captive portal flow:
  <https://source.android.com/docs/core/connect/android-custom-tabs-captive-portal>
  describes DHCP option 114, RFC 8908 JSON, and HTTP/HTTPS fallback probing.
* RFC 8910: <https://www.rfc-editor.org/rfc/rfc8910.html> defines DHCPv4
  option 114, its HTTPS API URI and the unrestricted URN.  RFC 8908:
  <https://www.rfc-editor.org/rfc/rfc8908.html> defines the authenticated
  `application/captive+json` API and its required top-level `captive` boolean.
* hostap's pregenerated per-network MAC patch:
  <https://lists.infradead.org/pipermail/hostap/2021-November/040054.html>
  intentionally leaves generation/persistence policy outside supplicant.
* The current hostap text backend still writes `mac_addr` without
  `mac_value`:
  <https://android.googlesource.com/platform/external/wpa_supplicant_8/+/refs/heads/main/wpa_supplicant/config_file.c>.
  Persisting the paired value is a correctness fix, validated on device.
* Reference xradio implementation:
  <https://github.com/Icenowy/xradio/blob/master/scan.c> uses forced
  background scan when joined and documents the unsafe pre-association
  window.  Our implementation keeps the Brick driver's stricter ownership,
  regulatory and recovery checks rather than copying the reference wholesale.
* Linux mac80211 authentication/association/deauthentication flow:
  <https://wireless.docs.kernel.org/en/latest/en/developers/documentation/mac80211/auth-assoc-deauth.html>
  defines departure-frame transmission and station teardown ordering.  The
  mac80211 driver API at
  <https://wireless.docs.kernel.org/en/latest/en/developers/documentation/mac80211/api.html>
  requires accurate TX status; the Brick runtime's firmware-confirmed ACK
  counters were checked rather than inferring delivery from the call path.
* RFC 5227: <https://www.rfc-editor.org/rfc/rfc5227.html> defines ARP
  announcements, including the new-base-station case.  Linux bridge learning
  and unknown-unicast behavior are documented at
  <https://docs.kernel.org/networking/bridge.html>.  These support the
  post-roam announcement experiment, but neither requires a consumer AP's
  separate station/offload table to accept an immediate MAC move.
* WPE WebKit 2.54 architecture:
  <https://wpewebkit.org/about/architecture.html> documents the built-in
  Wayland, DRM/KMS and headless WPEPlatform implementations, SHM fallback and
  custom-platform extension point.  The official 2.54 release notes are at
  <https://wpewebkit.org/release/wpewebkit-2.54.0.html>.  The custom fbdev/SHM
  proposal is a Brick-specific inference, not a validated upstream backend.
* Official XR829 capability brief:
  <https://www.allwinnertech.com/uploads/download_source/20260303162657a4.pdf>.
  Its HT40 declaration is recorded as unfinished support; only HT20 behavior
  was measured here.

All device test profiles, temporary binaries/modules, servers and the host AP
were removed after testing.  The Brick was left on its original network with
the installed stock module, base address and DHCP lease.
