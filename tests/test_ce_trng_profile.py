"""Exercise the isolated A133 CE/TRNG qualification profile."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ce_trng_profile
import emmc_recovery
import usb_profile


class CeTrngProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / '.config'
        self.config.write_text(
            'BR2_LINUX_KERNEL_INTREE_DTS_NAME="allwinner/sun50i-a133-trimui-brick"\n'
            'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=""\n'
            '# BR2_PACKAGE_BRICK_EMMC_RECOVERY is not set\n'
            '# BR2_PACKAGE_BRICK_SCP is not set\n'
            '# BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND is not set\n'
            '# BR2_BRICK_G2D_QUALIFICATION is not set\n'
            '# BR2_BRICK_VE_QUALIFICATION is not set\n'
            '# BR2_BRICK_VP9_QUALIFICATION is not set\n'
            '# BR2_BRICK_CE_TRNG_QUALIFICATION is not set\n')

    def tools(self):
        bindir = ROOT / 'output/build-default/host/bin'
        tools = tuple(shutil.which(name) or bindir / name
                      for name in ('dtc', 'fdtget', 'fdtput'))
        if not all(Path(tool).is_file() for tool in tools):
            self.skipTest('dtc, fdtget and fdtput are required')
        return tuple(map(str, tools))

    def compile_dtb(self, *, status='disabled', irq=92, edge=1,
                    clocks=(39, 38, 53), reset=4, iommus=False,
                    iommu_status='disabled', g2d='disabled', ve='disabled',
                    vp9='disabled'):
        dtc, _, _ = self.tools()
        iommu = 'iommus = <&iommu 7>;' if iommus else ''
        source = f'''/dts-v1/;
/ {{ #address-cells = <1>; #size-cells = <1>; chosen {{ }};
 soc {{ #address-cells = <1>; #size-cells = <1>; ranges;
  ccu: clock@3001000 {{ compatible = "allwinner,sun50i-a100-ccu";
   reg = <0x03001000 0x1000>; #clock-cells = <1>; #reset-cells = <1>; }};
  iommu: iommu@30f0000 {{ compatible = "allwinner,sun50i-a133-iommu";
   reg = <0x030f0000 0x1000>; status = "{iommu_status}";
   #iommu-cells = <1>; }};
  g2d@6480000 {{ compatible = "allwinner,sun50i-a133-g2d";
   reg = <0x06480000 0x40000>; status = "{g2d}"; }};
  video-codec@1c0e000 {{ compatible = "allwinner,sun50i-a133-video-engine";
   reg = <0x01c0e000 0x1000>; status = "{ve}"; }};
  video-codec@1c00000 {{ compatible = "allwinner,sun50i-a133-vpu-g2";
   reg = <0x01c00000 0x1000>; status = "{vp9}"; }};
  crypto@1904000 {{ compatible = "allwinner,sun50i-a133-crypto";
   reg = <0x01904000 0xa0>; interrupts = <0 {irq} {edge}>;
   clocks = <&ccu {clocks[0]}>, <&ccu {clocks[1]}>, <&ccu {clocks[2]}>;
   clock-names = "bus", "mod", "ram"; resets = <&ccu {reset}>;
   {iommu} status = "{status}"; }};
 }};
}};
'''
        output = self.root / ce_trng_profile.DTB
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', str(output)],
                       input=source, text=True, check=True, capture_output=True)
        return output

    def test_default_is_disabled_and_configuration_is_reversible(self):
        before = self.config.read_text()
        self.assertFalse(ce_trng_profile.enabled(self.config))
        ce_trng_profile.configure(self.config, 'qualification')
        self.assertTrue(ce_trng_profile.enabled(self.config))
        self.assertEqual(ce_trng_profile._fragment(self.config),
                         ce_trng_profile.FRAGMENT)
        ce_trng_profile.configure(self.config, 'disabled')
        self.assertEqual(self.config.read_text(), before)

    def test_profile_change_requires_fresh_output(self):
        ce_trng_profile.check_output(self.config, 'disabled')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            ce_trng_profile.check_output(self.config, 'qualification')
        ce_trng_profile.check_output(self.root / 'new-config', 'qualification')

    def test_install_enables_only_ce_and_marks_zero_credit(self):
        dtb = self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        ce_trng_profile.configure(self.config, 'qualification')
        ce_trng_profile.install(self.root, self.config, fdtget, fdtput)
        ce_trng_profile.validate(fdtget, dtb, True, marker=True)

    def test_wrong_stock_contract_or_iommu_is_rejected_without_mutation(self):
        _, fdtget, fdtput = self.tools()
        ce_trng_profile.configure(self.config, 'qualification')
        cases = (
            ({'irq': 91}, 'SPI 92'),
            ({'edge': 4}, 'edge-rising'),
            ({'clocks': (39, 38, 52)}, 'clocks'),
            ({'reset': 3}, 'reset'),
            ({'iommus': True}, 'must not acquire'),
        )
        for arguments, message in cases:
            with self.subTest(message=message):
                dtb = self.compile_dtb(**arguments)
                before = dtb.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    ce_trng_profile.install(self.root, self.config,
                                            fdtget, fdtput)
                self.assertEqual(dtb.read_bytes(), before)

    def test_enabled_peer_accelerator_is_rejected(self):
        _, fdtget, fdtput = self.tools()
        ce_trng_profile.configure(self.config, 'qualification')
        for name, arguments in (('IOMMU', {'iommu_status': 'okay'}),
                                ('G2D', {'g2d': 'okay'}),
                                ('video', {'ve': 'okay'}),
                                ('VP9', {'vp9': 'okay'})):
            with self.subTest(name=name):
                dtb = self.compile_dtb(**arguments)
                before = dtb.read_bytes()
                with self.assertRaisesRegex(ValueError, 'isolate'):
                    ce_trng_profile.install(self.root, self.config,
                                            fdtget, fdtput)
                self.assertEqual(dtb.read_bytes(), before)

    def test_profile_requires_peripheral_usb_and_no_other_qualification(self):
        self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        ce_trng_profile.configure(self.config, 'qualification')
        cases = []
        usb_profile.configure(self.config, 'host')
        cases.append(('USB gadget', self.config.read_text()))
        usb_profile.configure(self.config, 'peripheral')
        emmc_recovery.configure(self.config, '1')
        cases.append(('eMMC', self.config.read_text()))
        emmc_recovery.configure(self.config, '0')
        text = self.config.read_text().replace(
            '# BR2_PACKAGE_BRICK_SCP is not set', 'BR2_PACKAGE_BRICK_SCP=y')
        cases.append(('SCP', text))
        text = self.config.read_text().replace(
            '# BR2_BRICK_G2D_QUALIFICATION is not set',
            'BR2_BRICK_G2D_QUALIFICATION=y')
        cases.append(('peer accelerator', text))
        for message, text in cases:
            with self.subTest(message=message):
                self.config.write_text(text)
                with self.assertRaisesRegex(ValueError, message):
                    ce_trng_profile.install(self.root, self.config,
                                            fdtget, fdtput)

    def test_kernel_verifier_requires_builtins_and_reviewed_fail_closed_source(self):
        ce_trng_profile.configure(self.config, 'qualification')
        kernel = self.root / 'kernel'
        source = kernel / 'drivers/crypto/allwinner/sun50i-a133-ce/core.c'
        source.parent.mkdir(parents=True)
        (source.parent / 'core.o').write_bytes(b'object')
        hwrng = kernel / 'drivers/char/hw_random/core.c'
        hwrng.parent.mkdir(parents=True)
        (hwrng.parent / 'core.o').write_bytes(b'object')
        helper = hwrng.parent / 'core-internal.h'
        (kernel / '.config').write_text(''.join(
            f'{key}=y\n' for key in (
                'CONFIG_CRYPTO_DEV_SUN50I_A133_CE',
                'CONFIG_CRYPTO_DEV_SUN50I_A133_CE_TRNG', 'CONFIG_HW_RANDOM',
                'CONFIG_CRYPTO_USER_API_HASH', 'CONFIG_CRYPTO_USER_API_SKCIPHER')))
        source.write_text('''
if (device_iommu_mapped(dev))
    return -EOPNOTSUPP;
dma_set_mask_and_coherent(dev, DMA_BIT_MASK(32));
ce->trng.no_entropy_credit = true;
memchr_inv(s->output + req->bytes, 0xa5,
max = min_t(size_t, max, 8192);
max = round_down(max, 32);
#define CE_TIMEOUT_MS 1000
ce_recovery_fault(&ce->recovery, flow->index, cause,
''')
        hwrng.write_text('''
static ssize_t rng_quality_store(
{
ret = hwrng_validate_quality(rng, quality);
if (ret)
    goto out;
rng->quality = quality;
}
static DEVICE_ATTR_RW(rng_current);
if (!rng->no_entropy_credit && current_quality != rng->quality)
    rng->quality = current_quality;
quality = rng->no_entropy_credit ? 0 : rng->quality;
if (rng->no_entropy_credit)
\t\trng->quality = 0;
''')
        helper.write_text('''
if (quality > 1024)
\t\treturn -EINVAL;
if (rng->no_entropy_credit && quality)
\t\treturn -EPERM;
''')
        (hwrng.parent / 'Kconfig').write_text('config HW_RANDOM_KUNIT_TEST\n')
        (hwrng.parent / 'Makefile').write_text(
            'obj-$(CONFIG_HW_RANDOM_KUNIT_TEST) += core-test.o\n')
        (hwrng.parent / 'core-test.c').write_text('''
hwrng_quality_normal_source_test
hwrng_quality_zero_credit_source_test
hwrng_validate_quality(&rng, 0)
hwrng_validate_quality(&rng, 1)
hwrng_validate_quality(&rng, 1024)
hwrng_validate_quality(&rng, 1025)
''')
        documentation = kernel / 'Documentation/admin-guide/hw_random.rst'
        documentation.parent.mkdir(parents=True)
        documentation.write_text(
            'The core rejects attempts to assign\n'
            'such a source a nonzero ``rng_quality`` value.\n')
        ce_trng_profile.verify_kernel(self.config, kernel)
        source.write_text(source.read_text().replace(
            'return -EOPNOTSUPP;', 'return 0;'))
        with self.assertRaisesRegex(ValueError, 'rejects IOMMU'):
            ce_trng_profile.verify_kernel(self.config, kernel)

    def test_kernel_verifier_rejects_zero_credit_core_bypasses(self):
        ce_trng_profile.configure(self.config, 'qualification')
        kernel = self.root / 'kernel'
        ce = kernel / 'drivers/crypto/allwinner/sun50i-a133-ce/core.c'
        ce.parent.mkdir(parents=True)
        (ce.parent / 'core.o').write_bytes(b'object')
        ce.write_text('''
if (device_iommu_mapped(dev))
    return -EOPNOTSUPP;
dma_set_mask_and_coherent(dev, DMA_BIT_MASK(32));
ce->trng.no_entropy_credit = true;
memchr_inv(s->output + req->bytes, 0xa5,
max = min_t(size_t, max, 8192);
max = round_down(max, 32);
#define CE_TIMEOUT_MS 1000
ce_recovery_fault(&ce->recovery, flow->index, cause,
''')
        hwrng = kernel / 'drivers/char/hw_random/core.c'
        hwrng.parent.mkdir(parents=True)
        (hwrng.parent / 'core.o').write_bytes(b'object')
        helper = hwrng.parent / 'core-internal.h'
        helper.write_text('''
if (quality > 1024)
\t\treturn -EINVAL;
if (rng->no_entropy_credit && quality)
\t\treturn -EPERM;
''')
        (hwrng.parent / 'Kconfig').write_text('config HW_RANDOM_KUNIT_TEST\n')
        (hwrng.parent / 'Makefile').write_text(
            'obj-$(CONFIG_HW_RANDOM_KUNIT_TEST) += core-test.o\n')
        (hwrng.parent / 'core-test.c').write_text('''
hwrng_quality_normal_source_test
hwrng_quality_zero_credit_source_test
hwrng_validate_quality(&rng, 0)
hwrng_validate_quality(&rng, 1)
hwrng_validate_quality(&rng, 1024)
hwrng_validate_quality(&rng, 1025)
''')
        documentation = kernel / 'Documentation/admin-guide/hw_random.rst'
        documentation.parent.mkdir(parents=True)
        documentation.write_text(
            'The core rejects attempts to assign\n'
            'such a source a nonzero ``rng_quality`` value.\n')
        (kernel / '.config').write_text(''.join(
            f'{key}=y\n' for key in (
                'CONFIG_CRYPTO_DEV_SUN50I_A133_CE',
                'CONFIG_CRYPTO_DEV_SUN50I_A133_CE_TRNG', 'CONFIG_HW_RANDOM',
                'CONFIG_CRYPTO_USER_API_HASH', 'CONFIG_CRYPTO_USER_API_SKCIPHER')))
        valid = '''
static ssize_t rng_quality_store(
{
ret = hwrng_validate_quality(rng, quality);
if (ret)
    goto out;
rng->quality = quality;
}
static DEVICE_ATTR_RW(rng_current);
if (!rng->no_entropy_credit && current_quality != rng->quality)
    rng->quality = current_quality;
quality = rng->no_entropy_credit ? 0 : rng->quality;
if (rng->no_entropy_credit)
\t\trng->quality = 0;
'''
        cases = (
            ('hwrng_validate_quality(rng, quality)',
             'hwrng_validate_quality(rng, 0)', 'before assignment'),
            ('!rng->no_entropy_credit && ', '', 'override zero-credit'),
            ('rng->no_entropy_credit ? 0 : rng->quality', 'rng->quality',
             'override zero-credit'),
            ('if (ret)\n    goto out;\n', '', 'before assignment'),
        )
        for old, new, message in cases:
            with self.subTest(contract=old):
                hwrng.write_text(valid.replace(old, new))
                with self.assertRaisesRegex(ValueError, message):
                    ce_trng_profile.verify_kernel(self.config, kernel)


class CeTrngTargetTests(unittest.TestCase):
    def script_function(self, script, name):
        match = re.search(
            rf'^{name}\(\)\n\{{\n.*?^\}}\n', script, re.M | re.S)
        self.assertIsNotNone(match, f'missing {name} function')
        return match.group(0)

    def test_target_harness_compiles_strictly_and_script_is_bounded(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler is required')
        package = ROOT / 'package/brick-ce-trng-qualification'
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-pthread', package / 'ce-qualify.c', '-o',
                            Path(directory) / 'ce-qualify'], check=True)
        script = (package / 'brick-qualify-ce-trng').read_text()
        busybox = (ROOT / 'board/trimui/brick/busybox.fragment').read_text()
        subprocess.run(['/bin/sh', '-n', package / 'brick-qualify-ce-trng'],
                       check=True)
        self.assertIn('CONFIG_SETSID=y', busybox.splitlines())
        for required in ('timeout 180 "$tool" kat',
                         'concurrency 1024', 'bs=8192 count=1',
                         'rng_quality', 'iommu_group', 'driver/unbind',
                         'setsid "$tool" concurrency 4294967295',
                         'kill -TERM -"$pgid"', 'kill -KILL -"$pgid"',
                         'A133 TRNG survived CE unbind',
                         'select_and_probe_trng rebind',
                         'select_and_probe_trng resume',
                         'of="$out/trng-$phase.bin" bs=32 count=1',
                         '"$(wc -c < "$out/trng-$phase.bin")" -eq 32',
                         "printf 'freeze\\n' > /sys/power/state",
                         'post-resume-kat.log', 'post-reset-kat.log'):
            self.assertIn(required, script)
        unbind = script.index('> "$driver/unbind"')
        trng_absent = script.index('A133 TRNG survived CE unbind')
        rebind = script.index('> "$driver/bind"')
        rebind_probe = script.index('select_and_probe_trng rebind')
        self.assertLess(unbind, trng_absent)
        self.assertLess(trng_absent, rebind)
        self.assertLess(rebind, rebind_probe)
        self.assertLess(script.index("printf 'freeze\\n' > /sys/power/state"),
                        script.index('select_and_probe_trng resume'))

    def test_suspend_worker_must_survive_both_liveness_checks(self):
        package = ROOT / 'package/brick-ce-trng-qualification'
        source = (package / 'brick-qualify-ce-trng').read_text()
        functions = '\n'.join(self.script_function(source, name) for name in (
            'stress_exit', 'stress_require_alive', 'stress_stop'))
        harness = f'''#!/bin/sh
set -eu
stress_pid=
stress_group=1
{functions}
setsid false &
stress_pid=$!
sleep 0.05
if stress_require_alive "before suspend"; then exit 10; fi
setsid sleep 0.05 &
stress_pid=$!
sleep 0.01
stress_require_alive "before suspend"
sleep 0.1
if stress_require_alive "after resume"; then exit 11; fi
child_file=$(mktemp)
setsid /bin/sh -c 'sleep 30 & echo $! > "$1"; wait' worker "$child_file" &
stress_pid=$!
while [ ! -s "$child_file" ]; do sleep 0.01; done
child_pid=$(cat "$child_file")
stress_require_alive "before suspend"
stress_require_alive "after resume"
stress_stop
[ -z "$stress_pid" ]
if kill -0 "$child_pid" 2>/dev/null; then exit 12; fi
'''
        result = subprocess.run(['/bin/sh'], input=harness, text=True,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('exited before suspend (status=1)', result.stderr)
        self.assertIn('exited after resume (status=0)', result.stderr)


if __name__ == '__main__':
    unittest.main()
