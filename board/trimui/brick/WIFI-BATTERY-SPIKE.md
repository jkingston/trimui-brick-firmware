# Brick Wi-Fi battery spike

`WIFI-PRODUCTION-PLAN.md` turns this measurement matrix into production
entry/exit gates and fixes the order in which optional power work may land.

This note separates capabilities declared by XR829 and Linux from behaviour
that has actually been measured on the Brick.  It is a companion to
`WIFI-SPIKE.md`; it does not change the production Wi-Fi policy.

## What the current stack can do

* The XR829 data sheet declares a low-power clock used by the power controller
  during power-save modes.  Its DCDC figures at 3.6 V and 25 C are 0.12 mA for
  WLAN+Bluetooth sleep, 29--35 mA for 20 MHz WLAN receive, and 128--145 mA for
  the listed 20 MHz transmit cases.  These are chip-characterisation values,
  not measurements of the Brick or promises about scan current.
* Brick supplies the stock-equivalent clocks needed for that mode.  The device
  tree gives the radio power sequencer `lpo` and `ref` clocks, and U-Boot patch
  `0008-sunxi-initialize-Brick-radio-reference-clocks.patch` records the stock
  selection of `hosc32k` for the low-power output and the 24 MHz reference.
* The driver advertises mac80211 `SUPPORTS_PS` and `SUPPORTS_DYNAMIC_PS`.
  The initial implementation nevertheless read the obsolete
  `hw->conf.flags & IEEE80211_CONF_PS` path.  Linux 7.3's
  `net/mac80211/cfg.c` publishes user policy to a dynamic-PS driver as
  `vif->cfg.ps` plus `BSS_CHANGED_PS`; this is the exact in-tree API contract,
  not an inferred setting.  Runtime counters then exposed a second defect:
  the defensive XR829 command allowlist omitted WSM `SET_PM` (`0x0010`), so a
  corrected request was rejected with `-EOPNOTSUPP` before reaching firmware.
  The spike now consumes `vif->cfg.ps` and admits only the exact four-byte PM
  encodings produced by `station-ps.h`.  That helper selects normal 802.11 PS
  for a zero timeout or firmware fast PS for a positive timeout, in 500 us
  units capped at 127.5 ms.  Association also programs wakeup at the AP DTIM,
  except it uses every beacon when one DTIM interval would exceed one second.
* The installed service does **not** run `iw dev wlan0 set power_save on`.
  `iw` is in `brick_defconfig`, so this can be tested without adding software.
  Runtime inspection found it off.  Before the fixes, `iw` reported power save
  on without any firmware PM request; that observation is explicitly rejected
  as proof.  With instrumented module SHA-256
  `be5ad9928ba8344cb1ec040c2dd006b94f2c44e1ddb57063c8bb67d67fe5a357`,
  a controlled on/off cycle produced two requests, two distinct indications,
  two successes and no failures.  Debugfs recorded normal-PS bytes
  `01,00,c8,00` and off bytes `00,00,00,00`.  While enabled, 100 gateway pings
  at 100 ms delivered 100/100 at 0.237/0.303/0.399 ms minimum/average/maximum
  RTT.  A second run powered the Bluetooth controller and attempted discovery
  while PS was enabled: 100/100 packets arrived at 0.164/0.257/0.463 ms, after
  which Bluetooth and PS were restored off.  No discoverable peer was present,
  so this proves concurrent controller activity and WLAN traffic, not ACL/audio
  coexistence.  None of these functional results proves an idle energy saving
  or long-idle wake reliability.
* A final run of that exact candidate on 2026-11-06 exposed why functional
  confirmation is not sufficient to enable PS by default.  On the saved AP at
  -28 dBm, 100 pings at 100 ms intervals with PS enabled delivered 99/100 at
  6.466/118.604/630.383 ms min/average/maximum; the immediately following PS-off
  control delivered 100/100 at 3.335/9.283/87.387 ms.  At one ping per second,
  PS-on and PS-off both delivered 30/30, but averaged 23.911 ms and 12.985 ms
  respectively.  Firmware acknowledged both PM transitions in 4 ms and the
  8-second JOIN watchdog recorded zero false stall recoveries.  Current AP
  latency was variable, so these are an A/B rejection result rather than a
  general latency model: normal PS fails the proposed interactive-latency gate
  and remains default-off.  Fast-PS timing may be evaluated later, but only
  with the external energy measurement and full wake/multicast/DHCP matrix.
* Host-interface sleep is separate from 802.11 PS.  The current `bh.c` clears
  XR829's SDIO WUP bit only for system suspend.  There is no runtime-idle WUP
  path, so enabling 802.11 PS does not prove that SDIO/host-side consumption is
  minimal.  Runtime WUP gating is a later driver spike and needs wake-latency,
  traffic and firmware-recovery testing before use.
* The driver does not declare firmware CQM RSSI offload.  That is acceptable:
  mac80211 computes CQM crossings from received beacons and emits the signal
  event.  `bgscan=simple` can therefore switch intervals without a userspace
  RSSI poll loop.  This must still be verified on the device because a declared
  signal value does not prove event delivery.

## Cost of the scan policy

The associated-scan spike measured a 13-channel active scan at 560 ms and an
aggressive background policy at 5.52 s intervals.  No current was measured.
Using 35 mA, the data sheet's 20 MHz MCS7 receive figure, only as an
RX-equivalent model gives:

| Full-scan interval | RX-equivalent charge per hour |
| --- | ---: |
| 5.52 s | 3.55 mAh/h |
| 30 s | 0.65 mAh/h |
| 60 s | 0.33 mAh/h |
| 300 s | 0.065 mAh/h |
| 600 s | 0.033 mAh/h |

The model excludes probe TX, wake/setup time, SDIO and host CPU cost, and the
rest of the device.  It is useful only for comparing scan intervals.  It also
shows why the 5-second setting was a stress test, not a product setting.
Passive scan is not automatically cheaper: avoiding probe TX can be offset by
longer dwell and receiver-on time.  Compare active directed, active wildcard
and passive scans with an external meter before choosing one.

The AXP2202 battery driver exposes voltage, state of charge and charging state
but not battery `current_now`.  State-of-charge steps are too coarse for a scan
experiment.  Use a logging USB/battery power analyser or a temporary shunt;
do not present PMIC voltage or charge percentage as a current measurement.
No suitable analyser is attached to the current development host or Brick, so
the external-current cells and the 0.1 mAh/h scan gate remain unmeasured.  This
is a physical-instrument dependency, not a software pass.

## Minimum useful product policy

Start with the existing, upstream `bgscan=simple` controller rather than a new
daemon:

```
bgscan="simple:30:-68:300"
```

This requests a scan every five minutes with a healthy signal, switches to a
30-second interval below -68 dBm, and uses the module's 4 dB signal-monitor
hysteresis.  Its signal-change path can request an immediate scan on the weak
crossing, so the long interval does not mean waiting five minutes after the
link degrades.  The exact -68 dBm threshold is a starting hypothesis, not a
measured Brick roaming boundary.

Enable power save explicitly only after the A/B tests below pass:

```
iw dev wlan0 set power_save on
iw dev wlan0 get power_save
```

The first product setting should use the firmware/kernel default timing.  Do
not tune the PM bytes from data-sheet current figures.  Compare idle current,
interactive RTT, download throughput, multicast delivery, DHCP renewal,
captivity re-check and roam time with power save off/on.  In particular, the
current nl80211 `iw` operation does not expose a dynamic timeout; record the
actual four PM bytes and transition indication in driver debug output before
claiming fast-PS timing.

## Polished adaptive controller spike

`bgscan=simple` is a safe first step but has only one threshold and two
intervals.  A later supplicant module can use this state machine:

| State | Entry evidence | Full-scan ceiling | Exit guard |
| --- | --- | ---: | --- |
| Healthy | RSSI >= -62 dBm and low retry rate | 600 s | two weak samples |
| Marginal | -70 to -62 dBm, or retry trend rising | 120 s | 4 dB hysteresis |
| Weak | RSSI < -70 dBm | 30 s | candidate found or sustained recovery |
| Critical | beacon loss, repeated TX failure, or user opens Wi-Fi UI | immediate | one scan, then backoff |

Use signal and TX/retry events, not a one-second RSSI poll.  Defer a noncritical
scan while sustained game/download traffic is active because the measured
full scan can add roughly half a second of latency.  Never defer beacon-loss
recovery.  After a scan or roam, exponentially back off unsuccessful retries
to avoid remaining continuously awake at the edge of coverage.

Candidate-channel scans are a second-stage optimisation.  Cache channels per
ESS and scan those first, then periodically scan all permitted channels to
find changed topology.  Do not hard-code channels 1/6/11: 2.4 GHz APs may be
on any locally permitted channel.  `CONFIG_BGSCAN_LEARN` could provide a
reference implementation, but it is not currently enabled and must be checked
against XR829's maximum of two scan SSIDs and the driver's regulatory checks.

Screen state is a useful modifier, not the primary trigger.  With a healthy
link, lengthen the ceiling while the display is off; with marginal signal keep
event-driven recovery so downloads, time sync and captive-session traffic do
not silently stall.  Charging may permit more diagnostic scanning but should
not change roam correctness.

## Device experiment matrix and hooks

Run every row at least three times at a fixed AP/channel/RSSI and record kernel
version, module hash, AP beacon interval/DTIM and Bluetooth state:

| Variable | Levels | Record |
| --- | --- | --- |
| 802.11 PS | off / on | external average and peak current, RTT p50/p95/max, loss |
| Traffic | idle / 1 ping/s / download / interactive game | current, latency, throughput |
| Scan | none / 300 s / 30 s / forced single full scan | scan duration, current integral, latency |
| Scan type | directed active / wildcard active / passive | dwell, BSS yield, energy per useful result |
| Signal | strong / threshold crossing / weak | CQM event time, scan time, roam time |
| Screen | on / off | policy state and current; same network correctness checks |
| Coexistence | Bluetooth idle / controller traffic / audio if supported | Wi-Fi loss/latency and total current |

Useful target-side observations, none of which substitutes for the external
current measurement:

```
iw dev wlan0 get power_save
iw dev wlan0 link
wpa_cli -i wlan0 signal_poll
cat /proc/interrupts
cat /sys/class/net/wlan0/statistics/{rx_bytes,tx_bytes,rx_packets,tx_packets}
cat /sys/class/power_supply/axp2202-battery/{status,voltage_now,capacity}
```

For each scan, timestamp `CTRL-EVENT-SCAN-STARTED`,
`CTRL-EVENT-SCAN-RESULTS`, CQM signal changes, association and first successful
gateway packet.  Add temporary debug counters for PM command bytes,
PM-indication completion, WUP clear/set count and accumulated WUP-clear time.
Those counters are the smallest useful driver instrumentation before trying
runtime WUP gating.

### Driver spikes, in risk order

1. Expose read-only PM command/indication and SDIO-WUP counters in debugfs.
2. Validate power-save on/off and ordinary/fast-PS modes against several AP
   beacon/DTIM combinations, including multicast and DHCP.
3. Trial runtime WUP clear only after the TX queues are empty, no command or
   scan is pending, and firmware has confirmed PS.  Wake on queued TX/command;
   bound wake time and poison/recover on ambiguous transitions.
4. Only then consider firmware-assisted CQM or candidate-channel features.
   Existing mac80211 beacon-based CQM already avoids a polling daemon.

## Sources

* XR829 data sheet revision 1.4.1 (power table, low-power clock and host WUP):
  <https://img.eecart.com/dev/file/part/spec/XR829%20module%2024MHz-X-Powers%20Technology.pdf>
* XR829 product brief (declared 2.4 GHz, WLAN/BT coexistence and 32 kHz clock):
  <https://raw.githubusercontent.com/wiki/XradioTech/xradiotech-wiki/doc/XR829/XR829_Product_Brief.pdf>
* Linux mac80211 power-save contract:
  <https://docs.kernel.org/driver-api/80211/mac80211-advanced.html#powersave-support>
* Linux Wireless dynamic-power-save and `iw` usage:
  <https://wireless.docs.kernel.org/en/latest/en/users/documentation/dynamic-power-save.html>
  and <https://wireless.docs.kernel.org/en/latest/en/users/documentation/iw.html#power-save>
* hostap `bgscan_simple` implementation and interface:
  <https://w1.fi/wpa_supplicant/devel/bgscan__simple_8c.html> and
  <https://w1.fi/wpa_supplicant/devel/structbgscan__simple__data.html>
* In-tree reference: `drivers/net/wireless/st/cw1200/sta.c` uses the same WSM
  fast-PS encoding and documents the 127.5 ms field limit.  This is protocol
  precedent, not Brick runtime validation.
