"""Exercise the shared runtime inventory through its actual read-only validators."""
from pathlib import Path
import tempfile
import unittest

from hyadex.inventory import FILES
from hyadex.installation import validate_source
from scripts.package_release import validate_release_files

ROOT = Path(__file__).resolve().parents[1]


class ReleaseFileManifestTests(unittest.TestCase):
    def test_inventory_is_complete_and_unique(self):
        self.assertEqual(len(FILES), len(set(FILES)))
        self.assertTrue({'LICENSE', 'NOTICE', 'hyadex/inventory.py', 'hyadex/installation.py'} <= set(FILES))
        runtime = {str(p.relative_to(ROOT)) for p in (ROOT / 'hyadex').rglob('*.py')}
        self.assertTrue(runtime <= set(FILES), runtime - set(FILES))
        validate_release_files(ROOT)
        self.assertEqual(validate_source(ROOT), (ROOT / 'VERSION').read_text().strip())

    def test_missing_and_linked_members_reject_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('1.0.0\n' if name == 'VERSION' else 'fixture\n')
            outside = root / 'outside'
            outside.write_text('preserve\n')
            for name in FILES:
                path = root / name
                original = path.read_bytes()
                path.unlink()
                with self.subTest(name=name):
                    with self.assertRaises(SystemExit):
                        validate_release_files(root)
                    with self.assertRaises(ValueError):
                        validate_source(root)
                    path.symlink_to(outside)
                    with self.assertRaises(SystemExit):
                        validate_release_files(root)
                    with self.assertRaises(ValueError):
                        validate_source(root)
                path.unlink()
                path.write_bytes(original)
            directory = root / 'hyadex'
            directory.rename(root / 'saved-package')
            directory.symlink_to(root / 'saved-package', target_is_directory=True)
            with self.assertRaises(ValueError):
                validate_source(root)
