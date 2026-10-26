import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest


MODULE = Path(__file__).parents[1] / "package/brick-scp/extract.py"
SPEC = importlib.util.spec_from_file_location("brick_scp_extract", MODULE)
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)


class ScpArtifactTests(unittest.TestCase):
    def test_toc_rejects_bad_extent(self):
        package = bytearray(2048)
        package[:13] = b"sunxi-package"
        struct.pack_into("<I", package, 16, 0x89119800)
        struct.pack_into("<II", package, 32, 4, len(package))
        names = ("u-boot", "monitor", "scp", "dtb")
        for index, name in enumerate(names):
            base = 64 + index * 368
            package[base:base + len(name)] = name.encode()
            struct.pack_into("<II", package, base + 64, 2048, 1)
            package[base + 364:base + 368] = b"IIE;"
        with self.assertRaisesRegex(ValueError, "extent"):
            extract.toc_records(package)

    def test_checked_official_archive(self):
        candidates = list((Path(__file__).parents[1] / "output/downloads").glob(
            "brick-*/TG3040_Brick_firmware_v1.1.1.zip"))
        if not candidates:
            self.skipTest("official archive not cached")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            extract.extract(candidates[0], target)
            for name, (size, expected, filename) in extract.ARTIFACTS.items():
                data = (target / filename).read_bytes()
                self.assertEqual(len(data), size, name)
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected, name)


if __name__ == "__main__":
    unittest.main()
