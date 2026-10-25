"""Run actual gadget service against filesystem/sysfs fixtures; no USB claim."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / 'board/trimui/brick/rootfs-overlay/etc/init.d/S30usb'

class GadgetServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'run/brick-usb'
        self.gadget = self.root / 'sys/kernel/config/usb_gadget/brick'
        # MUSB names the UDC after its dynamically allocated child device,
        # rather than after the parent controller's DT address.
        self.udc = self.root / 'sys/class/udc/musb-hdrc.1.auto'
        self.status = self.root / 'sys/firmware/devicetree/base/soc/usb@5100000/status'
        self.status.parent.mkdir(parents=True)
        self.status.write_bytes(b'okay\0')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'calls'
        for name, body in [('mountpoint', 'exit 0'), ('ifconfig', f'echo "$*" >> "{self.calls}"'), ('sleep', 'exit 0')]:
            f = self.bin / name
            f.write_text('#!/bin/sh\n' + body + '\n')
            f.chmod(0o755)
        self.script = self.root / 'service'
        self.script.write_text(SERVICE.read_text().replace('/run/brick-usb', str(self.state)).replace('/sys/', str(self.root / 'sys') + '/'))
        self.script.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'])

    def run_service(self, command, action='add', success=True):
        result = subprocess.run([str(self.script), command], env=dict(self.env, ACTION=action), capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def controller(self, ready=True):
        self.udc.mkdir(parents=True, exist_ok=True)
        if ready:
            (self.udc / 'gadget').mkdir(exist_ok=True)
        self.gadget.mkdir(parents=True, exist_ok=True)
        (self.gadget / 'UDC').write_text('')
        (self.root / 'sys/class/net/usb0').mkdir(parents=True, exist_ok=True)

    def test_late_device_repeated_events_stop_restart(self):
        self.status.write_bytes(b'disabled\0')
        auto = self.root / 'sys/firmware/devicetree/base/usb-role-switch'
        auto.mkdir()
        # Missing status is enabled in the real coordinator DTB.
        (auto / 'compatible').write_bytes(b'trimui,brick-usb-role-switch\0')
        self.run_service('start')
        self.assertFalse(self.gadget.exists())
        self.controller()
        self.run_service('hotplug')
        self.assertEqual((self.gadget / 'UDC').read_text().strip(), 'musb-hdrc.1.auto')
        self.assertTrue((self.gadget / 'configs/c.1/ecm.usb0').is_symlink())
        self.run_service('hotplug', 'change')
        self.run_service('start')
        self.assertIn('192.168.7.2', self.calls.read_text())
        self.run_service('stop')
        self.assertEqual((self.gadget / 'UDC').read_text().strip(), '')
        self.run_service('hotplug')
        self.assertEqual((self.gadget / 'UDC').read_text().strip(), '')
        self.run_service('start')
        self.assertEqual((self.gadget / 'UDC').read_text().strip(), 'musb-hdrc.1.auto')

    def test_fixed_host_and_invalid_dt_do_not_configure(self):
        for status, success in [(b'disabled\0', True), (b'unknown\0', False)]:
            self.status.write_bytes(status)
            self.run_service('start', success=success)
            self.assertFalse((self.state / 'enabled').exists())
            self.assertFalse(self.gadget.exists())

    def test_not_ready_is_bounded_and_next_event_retries(self):
        self.controller(ready=False)
        self.run_service('start', success=False)
        self.assertFalse((self.state / 'configured').exists())
        (self.udc / 'gadget').mkdir()
        self.run_service('hotplug')
        self.assertTrue((self.state / 'configured').exists())

    def test_absent_or_ambiguous_udc_is_not_configured(self):
        self.run_service('start')
        self.assertFalse(self.gadget.exists())

        self.udc.mkdir(parents=True)
        other = self.udc.parent / 'unexpected.0'
        other.mkdir()
        self.run_service('hotplug')
        self.assertFalse(self.gadget.exists())

        other.rmdir()
        (self.udc / 'gadget').mkdir()
        self.run_service('hotplug')
        self.assertTrue((self.state / 'configured').exists())

    def test_remove_event_and_events_before_start_are_ignored(self):
        self.controller()
        self.run_service('hotplug')
        self.assertFalse((self.state / 'configured').exists())
        self.run_service('start')
        self.run_service('hotplug', 'remove')
        self.assertEqual((self.gadget / 'UDC').read_text().strip(), 'musb-hdrc.1.auto')

    def test_existing_registered_gadget_is_not_rewritten(self):
        self.controller()
        self.run_service('start')
        (self.gadget / 'idProduct').write_text('sentinel')
        self.run_service('hotplug', 'bind')
        self.assertEqual((self.gadget / 'idProduct').read_text(), 'sentinel')

if __name__ == '__main__':
    unittest.main()
