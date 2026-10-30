"""Exercise profile selection, actual DT payloads and gadget startup gating."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import usb_profile
import scp_profile
import deploy


class ProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'config'
        self.config.write_bytes((ROOT / 'configs/brick_defconfig').read_bytes())
        scp_profile.configure(self.config, 'disabled')
        usb_profile.configure(self.config, 'peripheral', 'disabled')

    def test_default_and_host_selection_preserve_kernel_pin(self):
        before = self.config.read_text()
        self.assertEqual(usb_profile.profile(self.config), 'peripheral')
        usb_profile.configure(self.config, 'host')
        self.assertEqual(usb_profile.profile(self.config), 'host')
        usb_profile.configure(self.config, 'peripheral')
        self.assertEqual(self.config.read_text(), before)

    def test_profile_change_requires_new_output(self):
        usb_profile.check_output(self.config, 'peripheral')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            usb_profile.check_output(self.config, 'host')
        usb_profile.check_output(self.root / 'new-config', 'host')

    def test_ambiguous_or_unknown_dtb_is_rejected(self):
        self.config.write_text(usb_profile.KEY + '="other-board allwinner/sun50i-a133-trimui-brick"\n')
        with self.assertRaises(ValueError):
            usb_profile.profile(self.config)
        value = usb_profile.KEY + '="allwinner/sun50i-a133-trimui-brick"\n'
        self.config.write_text(value * 2)
        with self.assertRaises(ValueError):
            usb_profile.profile(self.config)

    def compile_dtb(self, role, gadget_status=None, wake_profile='disabled',
                    broken_timer=False):
        dtc, fdtget = shutil.which('dtc'), shutil.which('fdtget')
        if not dtc or not fdtget:
            self.skipTest('dtc and fdtget are required for real DT payload tests')
        gadget = gadget_status or ('okay' if role == 'peripheral' else 'disabled')
        host = 'okay' if role == 'host' else 'disabled'
        coordinator = '' if role != 'auto' else '''usb-role-switch {
 compatible = "trimui,brick-usb-role-switch";
 phys = <&usbphy 0>; phy-names = "usb";
 id-gpios = <&pio 7 8 0>; vbus-power-supply = <&pmic>;
 peripheral = <&musb>; ehci = <&ehci>; ohci = <&ohci>;
};'''
        phy_switch = 'usb-role-switch;' if role == 'auto' else ''
        wake = ''
        deep_context = ''
        nmi_parent = '&gic'
        if wake_profile in ('startup', 'deep'):
            timer_parent = '&wakeupgen' if broken_timer else '&gic'
            nmi_parent = '&wakeupgen' if wake_profile == 'deep' else '&gic'
            deep_context = 'allwinner,stock-suspend;' if wake_profile == 'deep' else ''
            wake = f'''
 interrupt-parent = <&wakeupgen>;
 wakeupgen: interrupt-controller {{
  compatible = "allwinner,sun50i-a133-native-wakeupgen";
  interrupt-controller; interrupt-parent = <&gic>; #interrupt-cells = <3>;
 }};
 timer {{ compatible = "arm,armv8-timer"; interrupt-parent = <{timer_parent}>;
  interrupts = <1 13 0xf08>, <1 14 0xf08>, <1 11 0xf08>, <1 10 0xf08>;
 }};
'''
        source = f'''/dts-v1/;
/ {{ #address-cells = <1>; #size-cells = <1>; {wake}
 {coordinator}
 pmic: power-supply {{ }};
 soc {{ #address-cells = <1>; #size-cells = <1>; ranges;
 ccu: clock@3001000 {{ reg = <0x3001000 0x1000>; {deep_context} }};
 rccu: clock@7010000 {{ reg = <0x7010000 0x400>; {deep_context} }};
 pio: pinctrl@300b000 {{ reg = <0x300b000 0x400>; {deep_context} }};
 rpio: pinctrl@7022000 {{ reg = <0x7022000 0x400>; {deep_context} }};
 usbphy: phy@5100400 {{ reg = <0x5100400 0x100>; #phy-cells = <1>; {phy_switch} }};
 gic: interrupt-controller@3021000 {{ reg = <0x3021000 0x1000>;
  interrupt-controller; #interrupt-cells = <3>; interrupt-parent = <&gic>;
  interrupts = <1 9 0xf04>;
 }};
 nmi: interrupt-controller@7010320 {{ reg = <0x7010320 0xc>;
  interrupt-controller; #interrupt-cells = <2>; interrupt-parent = <{nmi_parent}>;
  interrupts = <0 103 4>;
 }};
 musb: usb@5100000 {{ reg = <0x5100000 0x400>; status = "{gadget}"; dr_mode = "peripheral"; }};
 ehci: usb@5101000 {{ reg = <0x5101000 0x100>; status = "{host}"; dr_mode = "host"; }};
 ohci: usb@5101400 {{ reg = <0x5101400 0x100>; status = "{host}"; dr_mode = "host"; }};
 }};
}};
'''
        output = self.root / (usb_profile.expected_dtb(role, wake_profile) + '.dtb')
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', str(output)],
                       input=source, text=True, check=True, capture_output=True)
        return output, fdtget

    def test_host_payload_manifest_and_archive_are_consistent(self):
        usb_profile.configure(self.config, 'host')
        source, fdtget = self.compile_dtb('host')
        expected = source.read_bytes()
        canonical = self.root / (usb_profile.DTBS['peripheral'] + '.dtb')
        canonical.write_bytes(b'stale peripheral DTB')
        usb_profile.install(self.root, self.config, fdtget)
        self.assertEqual(canonical.read_bytes(), expected)
        for name in ['Image', 'rootfs.cpio.gz', 'u-boot-sunxi-with-spl.bin']:
            (self.root / name).write_bytes(name.encode())
        (self.root / 'kernel.release').write_text('test-kernel\n')
        subprocess.run([sys.executable, str(ROOT / 'tools/manifest.py'), '--payload',
                        str(self.root), str(self.config), 'test-revision'], check=True)
        manifest = json.loads((self.root / 'payload-manifest.json').read_text())
        self.assertEqual(manifest['usb_role'], 'host')
        self.assertNotIn('scp_profile', manifest)
        self.assertEqual(manifest['files'][canonical.name]['sha256'], hashlib.sha256(expected).hexdigest())
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        with (self.root / 'sdcard.img').open('wb') as card:
            card.seek(deploy.BOOTLOADER_OFFSET)
            card.write((self.root / deploy.BOOTLOADER).read_bytes())
        stage = self.root / 'stage'
        stage.mkdir()
        archive, _ = deploy.package(self.root, stage)
        with tarfile.open(archive) as contents:
            self.assertEqual(contents.extractfile('brick-usb.dtb').read(), expected)
        with self.assertRaises(deploy.ProtocolError):
            deploy.verify_boot_manifest(dict(manifest, usb_role='peripheral'), manifest)

    def test_default_dtb_is_unchanged(self):
        source, fdtget = self.compile_dtb('peripheral')
        before = source.read_bytes()
        usb_profile.install(self.root, self.config, fdtget)
        self.assertEqual(source.read_bytes(), before)

    def test_startup_profile_selects_and_admits_native_wake_hierarchy(self):
        scp_profile.configure(self.config, 'startup')
        usb_profile.configure(self.config, 'peripheral', 'startup')
        source, fdtget = self.compile_dtb('peripheral', wake_profile='startup')
        expected = source.read_bytes()
        usb_profile.install(self.root, self.config, fdtget)
        canonical = self.root / (usb_profile.DTBS['peripheral'] + '.dtb')
        self.assertEqual(canonical.read_bytes(), expected)

    def test_startup_profile_rejects_non_gic_timer_parent(self):
        scp_profile.configure(self.config, 'startup')
        usb_profile.configure(self.config, 'peripheral', 'startup')
        _, fdtget = self.compile_dtb('peripheral', wake_profile='startup',
                                     broken_timer=True)
        with self.assertRaisesRegex(ValueError, 'direct-GIC'):
            usb_profile.install(self.root, self.config, fdtget)

    def test_startup_profile_rejects_host_usb_role(self):
        with self.assertRaisesRegex(ValueError, 'peripheral'):
            usb_profile.configure(self.config, 'host', 'startup')

    def test_deep_profile_routes_nmi_wake_through_firmware(self):
        scp_profile.configure(self.config, 'deep')
        usb_profile.configure(self.config, 'peripheral', 'deep')
        source, fdtget = self.compile_dtb('peripheral', wake_profile='deep')
        expected = source.read_bytes()
        usb_profile.install(self.root, self.config, fdtget)
        canonical = self.root / (usb_profile.DTBS['peripheral'] + '.dtb')
        self.assertEqual(canonical.read_bytes(), expected)

    def test_deep_profile_rejects_direct_gic_nmi(self):
        scp_profile.configure(self.config, 'deep')
        usb_profile.configure(self.config, 'peripheral', 'deep')
        source, fdtget = self.compile_dtb('peripheral', wake_profile='startup')
        source.rename(self.root / (usb_profile.NATIVE_DEEP_DTB + '.dtb'))
        with self.assertRaisesRegex(ValueError, 'NMI wake parent'):
            usb_profile.install(self.root, self.config, fdtget)

    def test_deep_profile_rejects_incomplete_context_opt_in(self):
        scp_profile.configure(self.config, 'deep')
        usb_profile.configure(self.config, 'peripheral', 'deep')
        source, fdtget = self.compile_dtb('peripheral', wake_profile='deep')
        fdtput = shutil.which('fdtput')
        if not fdtput:
            self.skipTest('fdtput required for mutated DT test')
        subprocess.run([fdtput, '-d', str(source), '/soc/pinctrl@7022000',
                        'allwinner,stock-suspend'], check=True)
        with self.assertRaisesRegex(ValueError, 'deep-context contract'):
            usb_profile.install(self.root, self.config, fdtget)

    def test_host_profile_rejects_enabled_gadget_before_overwriting_payload(self):
        usb_profile.configure(self.config, 'host')
        _, fdtget = self.compile_dtb('host', gadget_status='okay')
        canonical = self.root / (usb_profile.DTBS['peripheral'] + '.dtb')
        canonical.write_bytes(b'previous')
        with self.assertRaises(ValueError):
            usb_profile.install(self.root, self.config, fdtget)
        self.assertEqual(canonical.read_bytes(), b'previous')

    def test_missing_host_dtb_cannot_reuse_default_dtb(self):
        usb_profile.configure(self.config, 'host')
        with self.assertRaises(FileNotFoundError):
            usb_profile.install(self.root, self.config, '/not-invoked')

    def test_auto_profile_has_owned_disabled_controllers(self):
        usb_profile.configure(self.config, 'auto')
        source, fdtget = self.compile_dtb('auto')
        data = source.read_bytes()
        usb_profile.install(self.root, self.config, fdtget)
        self.assertEqual((self.root / (usb_profile.DTBS['peripheral'] + '.dtb')).read_bytes(), data)

    def test_auto_profile_rejects_enabled_unowned_controller(self):
        usb_profile.configure(self.config, 'auto')
        _, fdtget = self.compile_dtb('auto', gadget_status='okay')
        with self.assertRaises(ValueError):
            usb_profile.install(self.root, self.config, fdtget)

    def test_auto_profile_rejects_missing_stock_id_input(self):
        usb_profile.configure(self.config, 'auto')
        source, fdtget = self.compile_dtb('auto')
        fdtput = shutil.which('fdtput')
        if not fdtput:
            self.skipTest('fdtput required for mutated DT test')
        subprocess.run([fdtput, '-d', str(source), '/usb-role-switch',
                        'id-gpios'], check=True)
        with self.assertRaisesRegex(ValueError, 'stock PH8'):
            usb_profile.install(self.root, self.config, fdtget)

    def test_auto_profile_rejects_wrong_controller_reference(self):
        usb_profile.configure(self.config, 'auto')
        source, fdtget = self.compile_dtb('auto')
        fdtput = shutil.which('fdtput')
        if not fdtput:
            self.skipTest('fdtput required for mutated DT test')
        ohci = subprocess.check_output([fdtget, '-t', 'x', str(source),
                                       '/soc/usb@5101400', 'phandle'], text=True).strip()
        subprocess.run([fdtput, '-t', 'x', str(source), '/usb-role-switch',
                        'peripheral', ohci], check=True)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            usb_profile.install(self.root, self.config, fdtget)

    def test_auto_profile_rejects_missing_owner_or_wrong_mode(self):
        usb_profile.configure(self.config, 'auto')
        source, fdtget = self.compile_dtb('auto')
        fdtput = shutil.which('fdtput')
        if not fdtput:
            self.skipTest('fdtput required for mutated DT test')
        original = source.read_bytes()
        destination = self.root / (usb_profile.DTBS['peripheral'] + '.dtb')
        for mutation in [['-r', '/usb-role-switch'],
                         ['-t', 's', '/soc/usb@5100000', 'dr_mode', 'host']]:
            with self.subTest(mutation=mutation):
                source.write_bytes(original)
                destination.write_bytes(b'previous')
                subprocess.run([fdtput, *mutation[:1], str(source), *mutation[1:]]
                               if mutation[0] == '-r' else
                               [fdtput, '-t', 's', str(source), *mutation[2:]], check=True)
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    usb_profile.install(self.root, self.config, fdtget)
                self.assertEqual(destination.read_bytes(), b'previous')
