"""Data partition growth must preserve the boot partition and payload."""
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "package/brick-data-grow/brick-grow-data.c"


class DataGrowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.binary = self.root / "brick-grow-data"
        subprocess.run(["cc", "-std=c11", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror",
                        str(SOURCE), "-o", str(self.binary)], check=True)

    def image(self, data_size=131072, fourth=False):
        path = self.root / "card.img"
        sectors = 2 * 1024 * 1024
        with path.open("wb") as image:
            image.truncate(sectors * 512)
        mbr = bytearray(512)
        mbr[510:512] = b"\x55\xaa"
        mbr[446:462] = struct.pack("<BBBBBBBBII", 0x80, 0, 0, 0, 0x0c, 0, 0, 0,
                                   8192, 516096)
        mbr[462:478] = struct.pack("<BBBBBBBBII", 0, 0, 0, 0, 0x83, 0, 0, 0,
                                   524288, 917504)
        mbr[478:494] = struct.pack("<BBBBBBBBII", 0, 0, 0, 0, 0x83, 0, 0, 0,
                                   1441792, data_size)
        if fourth:
            mbr[494:510] = struct.pack("<BBBBBBBBII", 0, 0, 0, 0, 0x83, 0, 0, 0,
                                       1572864, 1024)
        with path.open("r+b") as image:
            image.write(mbr)
            image.seek(4096)
            image.write(b"boot-payload")
        return path, sectors

    def test_extends_only_third_partition_length_and_is_idempotent(self):
        image, sectors = self.image()
        with image.open("rb") as source:
            before = source.read(4108)
        first = subprocess.run([self.binary, image], check=True, capture_output=True, text=True)
        with image.open("rb") as source:
            after = source.read(4108)
        self.assertIn("Extended data partition", first.stdout)
        self.assertEqual(after[:490], before[:490])
        self.assertEqual(struct.unpack_from("<I", after, 490)[0], sectors - 1441792)
        self.assertEqual(after[494:], before[494:])
        self.assertEqual(after[4096:4108], b"boot-payload")
        second = subprocess.run([self.binary, image], check=True, capture_output=True, text=True)
        self.assertIn("already spans", second.stdout)

    def test_refuses_layout_with_a_fourth_partition(self):
        image, _ = self.image(fourth=True)
        with image.open("rb") as source:
            before = source.read(4108)
        result = subprocess.run([self.binary, image], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("partitions after data are not empty", result.stderr)
        with image.open("rb") as source:
            self.assertEqual(source.read(4108), before)


if __name__ == "__main__":
    unittest.main()
