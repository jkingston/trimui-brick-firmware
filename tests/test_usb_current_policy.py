from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UsbCurrentPolicyTest(unittest.TestCase):
    def test_stock_timing_data_session_and_reattach(self):
        header = ROOT / 'package/brick-power/usb-current-policy.h'
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include "POLICY"
int main(void)
{
    struct brick_usb_current_state state = { 0 };
    assert(brick_usb_current(&state, true, false, 1000) == 500000);
    assert(brick_usb_current(&state, true, false, 20999) == 500000);
    assert(brick_usb_current(&state, true, false, 21000) == 2500000);
    assert(brick_usb_current(&state, true, true, 22000) == 500000);
    assert(brick_usb_current(&state, true, false, 25000) == 500000);
    assert(brick_usb_current(&state, false, false, 26000) == 0);
    assert(brick_usb_current(&state, true, false, 30000) == 500000);
    assert(brick_usb_current(&state, true, false, 34999) == 500000);
    assert(brick_usb_current(&state, true, false, 35000) == 2500000);
    state = (struct brick_usb_current_state){ 0 };
    assert(brick_usb_current(&state, false, false, 40000) == 0);
    assert(brick_usb_current(&state, true, false, 41000) == 500000);
    assert(brick_usb_current(&state, true, false, 45999) == 500000);
    assert(brick_usb_current(&state, true, false, 46000) == 2500000);
    puts("PASS stock USB current boot/event timing, data latch and detach reset");
}
'''.replace('POLICY', str(header))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / 'test.c'
            binary = path / 'test'
            source.write_text(harness)
            subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra',
                            '-Werror', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=5)

    def test_service_and_build_install_are_complete(self):
        service = (ROOT / 'board/trimui/brick/rootfs-overlay/etc/init.d/S31usb-current')
        package = (ROOT / 'package/brick-power/brick-power.mk').read_text()
        source = (ROOT / 'package/brick-power/brick-usb-current.c').read_text()
        self.assertTrue(service.stat().st_mode & 0o111)
        self.assertIn('brick-stop-daemon usb-current', service.read_text())
        self.assertIn('$(@D)/brick-usb-current.c', package)
        self.assertIn('$(TARGET_DIR)/usr/sbin/brick-usb-current', package)
        self.assertIn('/sys/class/power_supply/axp2202-usb/input_current_limit', source)
        self.assertIn('/sys/class/udc', source)


if __name__ == '__main__':
    unittest.main()
