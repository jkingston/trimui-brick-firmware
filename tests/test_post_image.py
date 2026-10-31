import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POST_IMAGE = ROOT / "board/trimui/brick/post-image.sh"
POST_BUILD = ROOT / "board/trimui/brick/post-build.sh"
GENIMAGE = ROOT / "board/trimui/brick/genimage.cfg"


class PostImageTests(unittest.TestCase):
    def test_combined_profile_is_installed_before_rootfs_manifest(self):
        script = POST_BUILD.read_text()
        usb_install = script.index('usb_profile.py" install')
        combined = script.index('combined_accelerator_profile.py')
        install = script.index('install "$BINARIES_DIR"', combined)
        verify = script.index('verify-images')
        manifest = script.index('"$python" "$BR2_EXTERNAL_BRICK_PATH/tools/manifest.py"')
        self.assertLess(usb_install, install)
        self.assertLess(install, verify)
        self.assertLess(verify, manifest)

    def test_combined_profile_is_verified_before_manifest(self):
        script = POST_IMAGE.read_text()
        combined = script.index('combined_accelerator_profile.py')
        verify = script.index('verify-images')
        manifest = script.index('"$python" "$BR2_EXTERNAL_BRICK_PATH/tools/manifest.py"')
        self.assertLess(verify, manifest)
        self.assertNotIn('install "$BINARIES_DIR"', script[combined:verify])

    def test_full_image_starts_with_rollback_disarmed(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            binaries = work / "images"
            build = work / "build"
            target = work / "target"
            external = work / "external"
            tools = external / "tools"
            fake_bin = work / "bin"
            for path in (binaries, build, target, tools, fake_bin):
                path.mkdir(parents=True)

            (binaries / "kernel-source.txt").write_text("revision\noverride\n")
            for name in (
                "Image",
                "sun50i-a133-trimui-brick.dtb",
                "rootfs.cpio.gz",
                "u-boot-sunxi-with-spl.bin",
            ):
                (binaries / name).write_bytes(name.encode())
            (work / "brick.config").write_text("")
            (tools / "manifest.py").write_text("# no-op test manifest\n")
            (fake_bin / "genimage").write_text(
                "#!/bin/sh\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = --outputpath ]; then shift; : > \"$1/sdcard.img\"; fi\n"
                "  shift\n"
                "done\n"
            )
            (fake_bin / "genimage").chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                BINARIES_DIR=str(binaries),
                BUILD_DIR=str(build),
                TARGET_DIR=str(target),
                BR2_EXTERNAL_BRICK_PATH=str(external),
                BR2_CONFIG=str(work / "brick.config"),
                PATH=str(fake_bin) + os.pathsep + environment["PATH"],
            )
            subprocess.run(
                ["/bin/sh", str(POST_IMAGE)],
                env=environment,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(
                (binaries / "brick-bootcount.good").read_bytes(),
                bytes((0xBD, 1, 0, 0)),
            )
            self.assertFalse((binaries / "brick-bootcount.pending").exists())
            config = GENIMAGE.read_text()
            self.assertIn('file brick-bootcount { image = "brick-bootcount.good" }', config)
            self.assertNotIn("brick-bootcount.pending", config)


if __name__ == "__main__":
    unittest.main()
