import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('receive', Path(__file__).parents[1] / 'scripts/receive.py')
receive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receive)


class ReceiveIntegrityTests(unittest.TestCase):
    def fixture(self, root):
        payload = b'mtp fixture data\n'
        checks = (hashlib.sha256(payload).hexdigest() + '  weights.bin\n').encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as bundle:
            for name, data in [('package/weights.bin', payload), ('package/SHA256SUMS', checks)]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
        data = buffer.getvalue()
        (root / 'parts').mkdir()
        parts = []
        for i, offset in enumerate(range(0, len(data), 64)):
            chunk = data[offset:offset + 64]
            name = f'part-{i}'
            (root / 'parts' / name).write_bytes(chunk)
            parts.append({'part': i, 'batch': 0, 'name': name, 'bytes': len(chunk),
                          'sha256': hashlib.sha256(chunk).hexdigest()})
        return {'archive': 'fixture.tar.gz', 'archive_bytes': len(data),
                'archive_sha256': hashlib.sha256(data).hexdigest(), 'parts': parts,
                'batches': [{'batch': 0, 'image': 'example/data:fixture', 'digest': 'sha256:' + 'a' * 64}],
                'checksum_file': 'package/SHA256SUMS'}

    def test_valid_package_and_revalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.fixture(root)
            receive.validate_manifest(data)
            self.assertEqual(receive.assemble(data, root)['status'], 'verified')
            self.assertEqual(receive.assemble(data, root)['status'], 'verified')
            (root / 'extracted/package/weights.bin').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'Extracted file mismatch'):
                receive.assemble(data, root)
            self.assertFalse((root / 'TRANSFER-VERIFIED.json').exists())

    def test_corrupt_part_does_not_publish_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.fixture(root)
            (root / 'parts/part-0').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'Part mismatch'):
                receive.assemble(data, root)
            self.assertFalse((root / 'fixture.tar.gz').exists())
            self.assertFalse((root / 'TRANSFER-VERIFIED.json').exists())

    def test_unsafe_paths_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.fixture(Path(tmp))
            bad = copy.deepcopy(data)
            bad['parts'][0]['name'] = '../escape'
            with self.assertRaises(ValueError):
                receive.validate_manifest(bad)
            data['checksum_file'] = '/etc/passwd'
            with self.assertRaises(ValueError):
                receive.validate_manifest(data)


if __name__ == '__main__':
    unittest.main()
