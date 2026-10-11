"""Exercise installed blkid/parted on disposable regular files, never /dev."""
import os
import shutil
import subprocess
import tempfile
import unittest

from test_layout import block, device, merge, probe, select


@unittest.skipUnless(shutil.which('blkid') and shutil.which('parted'),
                     'requires blkid and parted')
class InstalledProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='localdisk-probe-', dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.path = os.path.join(self.temporary.name, 'disk.img')
        with open(self.path, 'wb') as stream:
            stream.truncate(16 * 1024 * 1024)

    def probe_image(self):
        result = subprocess.run(['blkid', '-p', '-o', 'export', self.path],
                                capture_output=True, text=True, timeout=15)
        # Only translate the temporary file's identity; retain real export data.
        stdout = result.stdout.replace('DEVNAME=' + self.path, 'DEVNAME=/dev/nvme0n1')
        return probe(rc=result.returncode, stdout=stdout, stderr=result.stderr)

    def test_installed_blkid_empty_file(self):
        result = self.probe_image()
        self.assertEqual(result['rc'], 2)
        chosen = select(merge([device()], [result]), [], [], '/mnt/localdisk')['devices'][0]
        self.assertEqual(chosen['layout'], 'individual')
        self.assertEqual(chosen['partition_table'], '')

    def test_installed_blkid_empty_gpt_must_not_be_reinitialized(self):
        subprocess.run(['parted', '--script', self.path, 'mklabel', 'gpt'],
                       check=True, capture_output=True, text=True, timeout=15)
        result = self.probe_image()
        self.assertEqual(result['rc'], 0)
        chosen = select(merge([device()], [result]), [], [], '/mnt/localdisk')['devices'][0]
        self.assertEqual(chosen['partition_table'], 'gpt')
        self.assertEqual(chosen['layout'], 'unsupported_partition_layout')

    def test_installed_blkid_gpt_with_partition(self):
        subprocess.run(['parted', '--script', self.path, 'mklabel', 'gpt',
                        'mkpart', 'localscratch', '1MiB', '100%'],
                       check=True, capture_output=True, text=True, timeout=15)
        blocks = [block('/dev/nvme0n1'), block('/dev/nvme0n1p1', 'part')]
        chosen = select(merge([device(blocks=blocks)], [self.probe_image()]),
                        [], [], '/mnt/localdisk')['devices'][0]
        self.assertEqual(chosen['partition_table'], 'gpt')
        self.assertTrue(chosen['partition_exists'])


if __name__ == '__main__':
    unittest.main()
