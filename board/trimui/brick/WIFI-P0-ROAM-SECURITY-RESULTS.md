# P0.1/P0.2 roaming and security qualification results

Date: 2026-11-06.  These results distinguish device measurements from policy
unit tests and from tests that the available AP hardware cannot perform.

## Exact test context

The station was the production Brick XR829 on Linux `7.3.0-brick-dev`, reached
independently over USB Ethernet at `192.168.7.2`.  Its installed WLAN module was
SHA-256 `0bf694fdfb9d673e54359c19cceb06e74b69376dc27f5794cae90c79da865dc6`.
The live scan/flush/telemetry spike was an AArch64 module built from the same
7.3.0 source and configuration, SHA-256
`27c3230748504aec70d756bf2de3f6d7a927e1c0565e0a9ee7bb5815e144404a`.
It added the already-spiked forced-background-scan and TX flush changes plus a
read-only `xr829-station/traffic` departure record.  The test module was loaded
with `allow_background_scan=1`; that remains a qualification setting, not a
new production default.

The controlled AP was this host's Wi-Fi device on channel 1.  It used an
isolated NetworkManager shared network (`10.42.0.0/24`), while the saved
consumer AP was on channel 6 (`192.168.68.0/22`).  USB Ethernet was never part
of either Wi-Fi data path.  SSIDs, credentials and client addresses are omitted
from this report.

Linux documents that a driver's `flush` callback must leave the hardware queue
empty and may drop frames only when `drop` is true.  That is why the spike
waits for firmware-owned TX confirmations before station teardown rather than
treating software queue removal as completion:
<https://docs.kernel.org/driver-api/80211/mac80211.html>.

## Reproducible controller prototypes

`tools/wifi/brick-roam-guard` implements the Phase-0 decision boundary:

* an SSID match never proves a shared L2;
* a BSSID pair is lease-retention eligible only after an earlier DHCP-backed
qualification records the gateway address and MAC;
* an eligible pair must answer ARP with the same gateway MAC after association;
* a changed or unqualified pair returns code 20, requiring DHCP;
* a qualified but dead path returns code 21, asks wpa_supplicant to roam back
  to the old BSSID and stores a directed-pair cooldown; and
* a successful verification sends two ARP announcements and retains the lease.

`tools/wifi/brick-wifi-recover` implements a bounded failed-JOIN sequence.  It
first requests normal station teardown, but accepts recovery only after it has
observed a new `SCANNING` state; cached `SCAN_RESULTS` are deliberately not
evidence.  It next recreates supplicant.  An explicit `--allow-driver-reset`
final stage stops userspace, unbinds only WLAN function `mmc1:0001:1` from the
`xr829` SDIO driver, rebinds it and starts userspace.  This order prevents stale
key commands racing a fresh firmware epoch.  The helper is a qualification
prototype and is not installed in the target image.

Seven contract tests cover same-L2 retention, unqualified and changed-L2
DHCP decisions, rollback/cooldown, all recovery stages, opt-in reset and
stop/unbind/bind/start ordering.  Together with existing service tests the
focused suite is:

```
python3 -m unittest -v tests.test_wifi_qualification tests.test_wifi
```

## P0.1 measured roaming results

The earlier two-BSSID same-L2 experiment remains valid: ten forced handoffs
survived with the lease and route retained, but independent-AP forwarding took
9--12 seconds in several directions.  That is evidence that the AP pair does
not meet the 3/5-second gate, not a reason to extend the client wait.  The new
prototype therefore rolls back at three failed one-second gateway probes and
cools the candidate pair.

One instrumented departure during the current run was a deauthentication frame
(`fc=0x00c0`).  Firmware returned status zero, reported it ACKed, and completed
12 ms after enqueue.  This proves the departure reached that AP; it does not
prove all APs update bridge forwarding promptly.

A deliberately unqualified old/new BSSID pair returned `unverified-l2`/20
before probing and therefore required DHCP.  On the isolated AP a foreground
BusyBox DHCP exchange then obtained `10.42.0.127/24` from `10.42.0.1`; returning
to the saved network obtained a `192.168.68.0/22` lease.  Thus the different-L2
decision and exchange work.  One event-driven background DHCP attempt emitted
DISCOVER and received an OFFER at the AP but did not install the lease within
15 seconds; the foreground exchange immediately completed DISCOVER/OFFER/
REQUEST/ACK.  The service's initial burst was consequently changed from one to
three one-second attempts (`udhcpc -b -t 3 -T 1`) before background retry.  A
repeat through the real event service then installed `10.42.0.127/24` and its
new default route three seconds after selection.

The P0.1 release gate is **not passed**.  This site currently has only one
controlled AP in XR829 range; the NAS AP was previously measured out of range.
It cannot supply the required 100 handoffs in both directions, coordinated
bridge and independent-AP cases, or an attenuator.  The host profile also
cannot configure WPA2/WPA3 transition or coordinated 802.11r/k/v.  Upstream
hostapd requires a common mobility domain and key-holder configuration for
802.11r and separately enables neighbor reports and BSS transition management:
<https://github.com/usnistgov/hostap/blob/master/hostapd/hostapd.conf>.
Those tests require two in-range hostapd/OpenWrt APs with packet capture and RF
attenuation; they cannot be replaced by more cycles on this single radio.

## P0.2 measured security and recovery results

| Case | Device result | Decision |
| --- | --- | --- |
| Open AP | Associated in about 2 seconds with `key_mgmt=NONE` | Pass for advertised scope |
| WPA2-PSK/RSN/CCMP | Associated in about 2 seconds; saved network remained usable | Pass for advertised scope |
| Wrong WPA2 credential | Reached four-way handshake, disconnected, AP remained visible | Pass; no reboot/reset |
| AP removed during JOIN | `SCANNING -> AUTHENTICATING -> DISCONNECTED`; fresh scan found 10 BSSes and saved WPA2 reconnected | Pass; no reboot/reset |
| WPA/TKIP-only AP | Correctly failed to complete, but left firmware joined after authentication and before association | Unsupported mode rejected, recovery required |
| WPA2/WPA3 transition | Host AP backend cannot advertise transition mode | Not run |
| SAE-only/WPA3 | Host backend failed to create a usable SAE AP | Not run; remains unadvertised |
| Malformed/deauth storm | No monitor/injection radio was available | Not run |

The TKIP case reproduced the important firmware wedge.  wpa_supplicant showed
`DISCONNECTED`, but the driver log stopped after authentication and firmware
remained joined to the failed BSS.  A scan command returned `OK`, while no new
`SCANNING` state or firmware scan command occurred; previously cached results
made the old check a false positive.  Explicit profile selection and a full
supplicant restart did not recover association.

The explicit WLAN-function reset stopped supplicant, confirmed station reset,
stopped the BH, reloaded XR829 firmware into a fresh epoch, and restored the
saved WPA2/CCMP association.  The complete three-stage helper took 14 seconds
in this diagnostic build and association completed one second after it
returned.  No stale temporary profile or key was present after restart.  The
Bluetooth HCI interface was not active at the time, so this run does not claim
active-controller coexistence across WLAN reset.

P0.2 is **partially passed, not release-complete**.  Every exercised case
returned scan/association capability without reboot, including the reproducible
TKIP wedge after the explicit WLAN reset.  Production must reject TKIP/WPA3/
transition profiles before JOIN, wire the bounded reset into the single
connectivity authority, and reduce/measure its deadline.  Transition mode,
SAE, malformed management storms, 100-cycle repetition and active-Bluetooth
reset coexistence still require the AP/injection hardware listed above.

The follow-up candidate module, SHA-256
`be5ad9928ba8344cb1ec040c2dd006b94f2c44e1ddb57063c8bb67d67fe5a357`,
also contains an 8-second JOIN-to-association watchdog that requests a fresh
firmware epoch rather than leaving a failed JOIN indefinitely.  A live saved
WPA2 association completed normally and recorded `stall_recoveries=0`; PM
on/off and 160 gateway packets likewise produced no false watchdog recovery.
This validates the non-failure path, not the deadline itself.  The TKIP-only
fixture must be repeated with this exact module to prove automatic recovery,
followed by the 100-cycle unsupported-security matrix before the watchdog can
replace the explicit qualification helper.

## Required next rig action

Do not expand policy based on these unavailable cells.  Acquire or place a
second in-range 2.4 GHz hostapd/OpenWrt AP, a monitor/injection radio and two
programmable attenuators.  Run the exact P0.1 4-by-feature matrix and P0.2
100-cycle security corpus with this report's helpers and departure telemetry.
Until then, keep 802.11r/k/v, WPA3, transition mode and unsupported ciphers out
of the UI, and treat all unqualified BSSID pairs as network changes requiring
DHCP.

## Saved-network fallback measurement

On 2026-11-07 the Brick was explicitly connected to a temporary WPA2/CCMP AP
on this host while its ordinary `quietnet_Guest` profile remained enabled.
USB Ethernet was the observation path.  After the temporary AP was disabled,
the Brick automatically returned to `quietnet_Guest`, restored its existing
`192.168.68.0/22` address and default route, and delivered five of five final
gateway probes.

The path recovered, but not within a polished interactive bound.  The station
noticed beacon loss and entered scanning about four seconds after AP shutdown.
It then selected the now-absent, higher-priority test BSS from stale candidate
state.  The XR829 firmware rejected that JOIN after 6.15 seconds; station
teardown returned `-EIO`, so the driver's synchronous fresh-firmware recovery
ran.  The saved network authenticated at about 14.8 seconds, reported
`COMPLETED` at about 15.1 seconds, and had its saved address/default route back
about 0.5 seconds later.  This is a **functional pass and latency failure**.

The evidence does not justify hiding the delay with a longer UI timeout.  The
authority should publish `Reconnecting` after the normal bound, and candidate
selection should avoid retrying a BSS absent from a completed fresh scan.  Any
change to wpa_supplicant BSS expiry or current-network retry policy needs an
A/B test against transient beacon loss so a brief interruption does not cause
unnecessary network switching.  The temporary supplicant profile and host AP
were removed, the profile priority was restored, and both host and Brick were
left on their original networks.

A runtime-only A/B then set wpa_supplicant's documented
`bss_expiration_scan_count=1` and `ignore_old_scan_res=1`.  With the same AP,
profile priority and shutdown sequence, beacon-loss detection still took about
4.1 seconds, but supplicant no longer attempted the absent BSS.  It moved from
the completed scan directly to `quietnet_Guest`; authentication took about
0.6 seconds and the address, default route and successful gateway probe were
present about 6.3 seconds after AP shutdown.  The driver log contained no new
JOIN rejection, teardown failure or firmware restart.  This A/B isolates about
nine seconds of the baseline outage to stale-BSS selection and its consequent
XR829 recovery rather than DHCP or the replacement AP.

The faster policy remains a candidate, not a selected production default.
Before changing the stock supplicant settings, repeat at least 100 hard AP-loss
cycles plus brief beacon-loss, congested-scan, hidden-network and multi-BSSID
cases.  One-scan expiry must not cause needless profile switching when a scan
miss is transient.  Both runtime values were restored to their stock defaults
(`2` and `0`) after the A/B.

### Stale-result policy isolation

A follow-up isolated the two settings with a fresh temporary AP/profile for
each trial.  Reusing one fixture back-to-back was rejected because the first
failed JOIN could contaminate the next XR829 epoch.  Results from AP shutdown
to the saved fallback SSID were:

| BSS expiry scans | Ignore pre-request results | Fallback |
| ---: | ---: | ---: |
| 2 (stock) | 0 (stock) | 15.12 s |
| 2 (stock) | 1 | **5.78 s** |
| 1 | 0 (stock) | 22.26 s |
| 1 | 1 | 6.33 s |

The correct narrow change is therefore `ignore_old_scan_res=1` while retaining
the stock two-scan expiration.  The upstream option exists specifically for a
driver cache returning results older than the scan request.  Source inspection
also confirmed that supplicant already removes the current BSS after a
disconnect; XR829's returned stale scan result was adding it back.  Flushing
the whole BSS table, expiring every one-scan miss, or shortening the firmware
JOIN deadline all act later or discard useful alternative candidates.

With the selected `2/1` policy, one-second and three-second AP interruptions
both ended associated to the original temporary AP after eight seconds, rather
than needlessly settling on the fallback.  These NetworkManager AP restarts are
useful boundary checks, not RF-attenuator coverage.  The reproducible controller
is `tools/wifi/spike-stale-bss-matrix`; it traps cleanup, removes the temporary
profile/AP, restores the stock runtime values, and re-enables saved networks.

This closes the mechanism decision but not the release-volume gate.  Set
`ignore_old_scan_res=1` in the Wi-Fi authority for production, retain
`bss_expiration_scan_count=2`, and qualify 100 hard losses plus partial,
aborted, hidden-SSID and multi-BSSID scans.  Do not change the XR829's 20-beacon
loss threshold or eight-second JOIN watchdog from this evidence: the former
controls initial loss detection, while the latter is only containment after an
incorrect JOIN has already begun.
