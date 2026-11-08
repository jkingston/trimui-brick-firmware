# P0.8 Wi-Fi persistence-fault results

Date: 2026-11-06

The initial fault matrix used only disposable `TemporaryDirectory` roots. A
follow-up device matrix used three exact, synthetic captive profile keys under
`/data/wpe-single-spike/tmp/captive-profile-qualification`; it did not read or
alter a real Wi-Fi or captive profile. Those test profiles and all exact
runner artifacts were removed afterward. The production paths were exercised
with command mocks at their normal supplicant boundary; the WebKit
cookie/local-storage files and filesystem rename/recovery ran on the A133
device. The wpa_supplicant change was also cross-compiled for AArch64.

## Production-path changes proved by the spike

* Private-MAC metadata remains a mode-0600 temporary file until complete. The
  file is globally synced, atomically renamed, and synced again before success.
  A startup under the existing state lock removes unpublished stale temporary
  files. This uses stock BusyBox `sync`: the selected BusyBox configuration has
  `CONFIG_SYNC=y` and does not have `CONFIG_FEATURE_SYNC_FANCY`.
* DHCP metadata is volatile `/run` state, so it requires atomicity rather than
  power-loss durability. Its hook now removes stale temporary files and traps
  normal termination around the temp-write/rename sequence.
* wpa_supplicant 2.12 already uses a private random temporary, `fdatasync()` and
  rename for `SAVE_CONFIG`. Patch 0002 now checks both writeback/close results
  and fsyncs the parent directory after rename. This is a deliberate durability
  improvement over the exact upstream 2.12 implementation; it does not change
  the credential format or radio configuration.
* `brick-wifi-forget` is the single Forget Network transaction. It durably
  publishes an intent before disabling/removing credentials, then saves the
  supplicant configuration, deletes private-MAC state by canonical network
  key, and rename-deletes the WPE profile. The intent is removed last. Recovery
  runs while S45wifi still holds RF blocked. Reused transient supplicant IDs
  are detected by recomputing the stable profile key before removal.
  Recovery now removes a tombstone left by interruption immediately after the
  atomic profile rename; previously it saw the missing published path and
  incorrectly left that tombstone behind.
* The captive directory is derived exactly as the production WPE launcher does:
  the canonical SHA-256 network identity is hashed once by the launcher. Raw
  SSIDs, credentials, URLs, cookies and MAC addresses are not logged.
* The launcher, supervisor and forget transaction now share
  `/data/captive-browser` and the same single additional SHA-256 directory
  derivation. The NetworkProcess receives only that current directory as its
  writable sandbox bind. Persistent cookies use an explicit per-profile
  SQLite store; session cookies remain session-only by design.

## Fault matrix and evidence

`python3 -m unittest -v tests.test_wifi_persistence tests.test_wifi_private_mac
tests.test_wifi_connectivity tests.test_wifi` passed **39/39** tests in 5.036 s.

| Boundary | Injected fault | Observed invariant | Result |
|---|---|---|---|
| Private MAC temp write | SIGKILL after complete temp; RLIMIT_FSIZE=0 during write | Last published inode byte-identical | Pass |
| Private MAC rename | SIGKILL immediately after rename | Complete parseable new record, never a partial record | Pass |
| DHCP temp write | SIGKILL after temp; RLIMIT_FSIZE=0 during write | Previous lease byte-identical | Pass |
| Forget intent temp | SIGKILL before intent rename | Credentials, MAC metadata and captive data all remain; retry completes | Pass |
| Forget intent write | RLIMIT_FSIZE=0 | No destructive step starts; all prior state remains | Pass |
| Forget intent rename | SIGKILL after durable intent | `recover` completes and a second `recover` is a no-op | Pass |
| Credential removal | SIGKILL after `REMOVE_NETWORK`, before `SAVE_CONFIG` | Recovery issues `SAVE_CONFIG`, then finishes deletion | Pass |
| Credential save | SIGKILL after `SAVE_CONFIG` | Recovery tolerates the absent network and finishes deletion | Pass |
| Private-MAC deletion | SIGKILL after delete | Recovery is idempotent | Pass |
| Captive rename/delete | SIGKILL on both sides of profile rename | Original path disappears atomically; recovery removes tombstone | Pass |

The patched `config_file.c` cross-compiled without warning using the project's
AArch64 Buildroot compiler and the exact wpa_supplicant 2.12 configuration.
The resulting test object SHA-256 was
`5c538e9f3a1a5441ccac41b97c9faa9ce8b16918e2626cf89c9feac0ae09003d`.
The patch also dry-applied cleanly after the dedicated-MAC patch.

## Device profile matrix

The same fixture origin produced these exact observations from separate WPE
sessions: A seed `A/A`, A normal reopen `A/A`, fresh B `missing/missing`, and C
after abrupt UI-process death `A/A` (cookie/local storage respectively). With
B populated as `B/B`, a fault-injected A forget stopped after profile rename;
recovery twice left A `missing/missing` and B `B/B`, with no tombstone, intent
or private-MAC record. The launcher cross-build containing the narrowed bind
and SQLite cookie store was warning-clean.

A second device pass bounded ENOSPC to a private 64 MiB tmpfs mounted only as
the synthetic captive-profile root; it did not fill `/data` or any global
persistent filesystem. After precreating A's directories and consuming the
tmpfs, the live page could still report its in-memory `A/A`, but SQLite logged
`database or disk is full`, its attempted files remained zero length, and a
reopen after freeing space correctly reported `missing/missing`. B's existing
database remained `B/B` before and after the fault.

Replacing only A's synthetic `cookies.sqlite` with random bytes reproduced
SQLite `file is not a database`; stock WebKit rendered but did not repair the
file. The launcher now performs a read-only `PRAGMA quick_check(1)` before
creating the network session and removes only a confirmed-corrupt current
profile cookie database plus its WAL/SHM. The first recovery was `missing/A`;
reseeding and reopening was `A/A`, while B remained `B/B`. Replacing only C's
local-storage SQLite file with random bytes was automatically rebuilt by
WebKit and reopened as `A/A`. A symlink substituted for the captive root was
rejected before mutation. The interrupted A forget then recovered
idempotently and B still reopened `B/B`.

Afterward the exact production tuple was restored: WPE library
`f771556f08fa198cb6f5da27b7d38bb9f1b706db402174f4c6fc00931d66c805`
and inner launcher
`f0be971872d703ecf14c0b22110dccebcb58796dce778fc0155ea7f87df14cee`.
There were zero browser/Bubblewrap processes, captive cgroups, captive mounts
and DRM clients; the shell was running, WPA was `COMPLETED` at
`192.168.68.70`, display nodes were root-owned 0660, and both synthetic profile
roots were absent. This proves bounded ENOSPC, database-corruption handling,
process interruption and rename-boundary recovery, not literal loss of device
power or a firmware-version migration.

## Clean-image/SBOM read-only audit

The clean build remains owned by the parent task; this spike did not modify or
restart it. Its generated configuration has Mesa 26.1.8 softpipe, GBM, EGL and
GLES enabled, `libglvnd` disabled, the stock `brick-gpu` package disabled, and
the custom WPE 2.54.1 package enabled. Therefore the selected clean image has
one EGL provider and cannot contain the earlier stock-PowerVR/Mesa collision.
At inspection time the target contained only 107 files and no final EGL/WPE
libraries, so final file-level collision evidence still depends on completion
of that clean build.

Buildroot 2026.08.1 supplies all three release inputs, but none was yet wired
into the firmware release output:

* `make legal-info` produces the license bundle and its SHA-256;
* `make show-info` piped to `utils/generate-cyclonedx` produces CycloneDX; and
* `pkg-stats` plus `support/scripts/cve-check` provides version/CVE evidence.

The custom WPE package declares the same LGPL-2.1+/BSD-2-Clause files as the
upstream Buildroot WPE package, but it lacks the upstream WPE CPE vendor/product
fields. Without those fields automated CVE association is weaker. There is no
checked-in vulnerability triage/update SLA, no release gate that archives
`legal-info`, SBOM and CVE results, and no completed rollback-image boot result.
Those are production-release gaps, not persistence ambiguity.

One additional integration gap was found read-only: the WPE package sets
`GBM_ALWAYS_SOFTWARE=true` and `GALLIUM_DRIVER=softpipe` only in its build
environment. The installed `brick-captive-run` does not set them at runtime.
The clean image therefore needs a runtime environment assertion/fix before its
browser boot result can satisfy P0.8; this spike did not touch the WPE files
because the WPE qualification agents own them.
