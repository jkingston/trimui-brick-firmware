"""Contract tests for the P0.1/P0.2 hardware qualification helpers."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROAM = ROOT / "tools/wifi/brick-roam-guard"
RECOVER = ROOT / "tools/wifi/brick-wifi-recover"
WPA_PATCHES = ROOT / "board/trimui/brick/patches/wpa_supplicant"


def executable(path, text):
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o755)


class RoamGuardTests(unittest.TestCase):
    OLD = "02:11:22:33:44:55"
    NEW = "02:11:22:33:44:66"
    GW = "192.0.2.1"
    MAC = "02:aa:bb:cc:dd:ee"

    def fixture(self, root, neighbor_mac=None, arp_success=True):
        calls = root / "calls"
        arping = root / "arping"
        executable(arping, f'echo "arping $*" >> "$TEST_CALLS"\nexit {0 if arp_success else 1}\n')
        ip = root / "ip"
        line = f"{self.GW} dev wlan0 lladdr {neighbor_mac} REACHABLE" if neighbor_mac else ""
        executable(ip, f"echo '{line}'\n")
        cli = root / "wpa_cli"
        executable(cli, 'echo "wpa $*" >> "$TEST_CALLS"\n')
        date = root / "date"
        executable(date, 'echo "${TEST_NOW:-1000}"\n')
        sleep = root / "sleep"
        executable(sleep, ':\n')
        env = dict(os.environ, WIFI_ROAM_STATE_DIR=str(root / "state"),
                   WIFI_ARPING=str(arping), WIFI_IP=str(ip), WIFI_WPA_CLI=str(cli),
                   WIFI_DATE=str(date), WIFI_SLEEP=str(sleep), TEST_CALLS=str(calls))
        return env, calls

    def run_verify(self, env):
        return subprocess.run([str(ROAM), "verify", self.OLD, self.NEW, self.GW,
                               self.MAC, "192.0.2.10"], env=env,
                              capture_output=True, text=True, timeout=5)

    def approve(self, env):
        result = subprocess.run([str(ROAM), "approve", self.OLD, self.NEW,
                                 self.GW, self.MAC], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_l2_retains_lease_and_announces(self):
        with tempfile.TemporaryDirectory() as directory:
            env, calls = self.fixture(Path(directory), self.MAC)
            self.approve(env)
            result = self.run_verify(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "same-l2")
            self.assertIn("-U -q -b -c 2", calls.read_text())
            self.assertNotIn(" roam ", calls.read_text())

    def test_unverified_pair_requires_dhcp(self):
        with tempfile.TemporaryDirectory() as directory:
            env, calls = self.fixture(Path(directory), self.MAC)
            result = self.run_verify(env)
            self.assertEqual(result.returncode, 20)
            self.assertEqual(result.stdout.strip(), "unverified-l2")
            self.assertFalse(calls.exists())

    def test_changed_gateway_mac_requires_dhcp(self):
        with tempfile.TemporaryDirectory() as directory:
            env, calls = self.fixture(Path(directory), "02:aa:bb:cc:dd:ff")
            self.approve(env)
            result = self.run_verify(env)
            self.assertEqual(result.returncode, 20)
            self.assertEqual(result.stdout.strip(), "different-l2")
            self.assertNotIn(" roam ", calls.read_text())

    def test_dead_path_rolls_back_and_enters_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            env, calls = self.fixture(Path(directory), arp_success=False)
            self.approve(env)
            result = self.run_verify(env)
            self.assertEqual(result.returncode, 21)
            self.assertEqual(result.stdout.strip(), "rollback")
            self.assertIn(f"roam {self.OLD}", calls.read_text())
            check = subprocess.run([str(ROAM), "check", self.OLD, self.NEW], env=env,
                                   capture_output=True, text=True, timeout=5)
            self.assertEqual(check.returncode, 75)
            self.assertEqual(check.stdout.strip(), "cooldown")


class RecoveryTests(unittest.TestCase):
    def fixture(self, root, successful_stage):
        calls = root / "calls"
        count = root / "scan-count"
        scan_ok = root / "scan-ok"
        scan_seen = root / "scan-seen"
        cli = root / "wpa_cli"
        executable(cli, '''
echo "wpa $*" >> "$TEST_CALLS"
case "$*" in
*" status")
  if [ -f "$TEST_SCAN_OK" ] && [ ! -f "$TEST_SCAN_SEEN" ]; then
    touch "$TEST_SCAN_SEEN"; echo wpa_state=SCANNING
  elif [ -f "$TEST_SCAN_OK" ]; then
    rm -f "$TEST_SCAN_OK" "$TEST_SCAN_SEEN"; echo wpa_state=DISCONNECTED
  else
    echo wpa_state=DISCONNECTED
  fi
  exit 0 ;;
*" scan")
  n=0; [ ! -f "$TEST_COUNT" ] || n=$(cat "$TEST_COUNT")
  n=$((n + 1)); echo "$n" > "$TEST_COUNT"
  if [ "$n" -ge "$TEST_SUCCESS_STAGE" ]; then touch "$TEST_SCAN_OK"; exit 0; fi
  exit 1 ;;
esac
exit 1
''')
        service = root / "service"
        executable(service, 'echo "service $*" >> "$TEST_CALLS"\n')
        sleep = root / "sleep"
        executable(sleep, ':\n')
        driver = root / "driver"
        driver.mkdir()
        (driver / "unbind").touch()
        (driver / "bind").touch()
        env = dict(os.environ, WIFI_WPA_CLI=str(cli), WIFI_SERVICE=str(service),
                   WIFI_SLEEP=str(sleep), WIFI_SDIO_DRIVER=str(driver),
                   TEST_CALLS=str(calls), TEST_COUNT=str(count),
                   TEST_SCAN_OK=str(scan_ok), TEST_SCAN_SEEN=str(scan_seen),
                   TEST_SUCCESS_STAGE=str(successful_stage))
        return env, calls, driver

    def test_normal_station_reset_is_first_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            env, calls, _ = self.fixture(Path(directory), 1)
            result = subprocess.run([str(RECOVER)], env=env, capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "station-reset")
            self.assertNotIn("service ", calls.read_text())

    def test_supplicant_restart_precedes_driver_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            env, calls, _ = self.fixture(Path(directory), 2)
            result = subprocess.run([str(RECOVER)], env=env, capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "supplicant-restart")
            self.assertIn("service restart", calls.read_text())

    def test_driver_reset_requires_opt_in_and_orders_services(self):
        with tempfile.TemporaryDirectory() as directory:
            env, calls, driver = self.fixture(Path(directory), 3)
            denied = subprocess.run([str(RECOVER)], env=env, capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(denied.returncode, 22)
            # Reset the staged scan count before exercising the explicit path.
            Path(env["TEST_COUNT"]).unlink()
            result = subprocess.run([str(RECOVER), "--allow-driver-reset"], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "driver-reset")
            text = calls.read_text()
            self.assertLess(text.rfind("service stop"), text.rfind("service start"))
            self.assertEqual((driver / "unbind").read_text(), "mmc1:0001:1\n")
            self.assertEqual((driver / "bind").read_text(), "mmc1:0001:1\n")


class SecurityBuildPolicyTests(unittest.TestCase):
    def test_tkip_is_rejected_before_join(self):
        patch = (WPA_PATCHES /
                 "0002-build-disable-obsolete-TKIP.patch").read_text()
        self.assertIn("+CONFIG_NO_TKIP=y", patch)
        self.assertIn("wpa_supplicant/defconfig", patch)

    def test_wpa3_remains_out_of_product_configuration(self):
        config = (ROOT / "configs/brick_defconfig").read_text()
        self.assertNotIn("BR2_PACKAGE_WPA_SUPPLICANT_WPA3=y", config)


if __name__ == "__main__":
    unittest.main()
