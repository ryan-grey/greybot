import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from greybot_control.local_archive import LocalArchive
from greybot_control.store import Store


class LocalArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state" / "db")
        self.directory = self.root / "archive"
        self.directory.mkdir()
        self.archive = LocalArchive(self.directory)
        self.store.append("test", "1", "ADMIN_LOGIN", "2", {})

    def test_verified_copy_and_idempotent_retry_after_lost_receipt(self):
        with patch.object(self.store, "receipt", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.archive.flush(self.store)
        self.assertEqual(len(self.store.pending()), 1)
        self.archive.flush(self.store)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(len(list(self.directory.iterdir())), 1)
        self.assertTrue(self.store.verify())

    def test_conflict_is_not_overwritten_or_receipted(self):
        with patch.object(self.store, "receipt", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.archive.flush(self.store)
        target = next(self.directory.iterdir())
        target.chmod(0o600)
        target.write_text("modified by NAS owner")
        with self.assertRaises(RuntimeError):
            self.archive.flush(self.store)
        self.assertEqual(target.read_text(), "modified by NAS owner")
        self.assertEqual(len(self.store.pending()), 1)

    def test_missing_mount_does_not_acknowledge_records(self):
        self.directory.rmdir()
        with self.assertRaises(RuntimeError):
            self.archive.flush(self.store)
        self.assertEqual(len(self.store.pending()), 1)
