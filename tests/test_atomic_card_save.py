import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paa.cards import read, save


class AtomicSaveSharingTests(unittest.TestCase):
    def test_transient_windows_reader_does_not_abort_atomic_save(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source-inventory.json"
            save(target, {"before": True})
            replace = os.replace
            calls = []

            def briefly_shared(source, destination):
                calls.append((source, destination))
                if len(calls) == 1:
                    error = PermissionError("File temporarily open by a reader")
                    error.winerror = 5
                    raise error
                return replace(source, destination)

            with patch("paa.cards.os.replace", side_effect=briefly_shared):
                save(target, {"after": True})
            self.assertEqual(read(target), {"after": True})
            self.assertGreater(len(calls), 1)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_persistent_denial_preserves_original_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source-inventory.json"
            save(target, {"before": True})
            error = PermissionError("Persistent access denial")
            error.winerror = 5
            with patch("paa.cards.os.replace", side_effect=error):
                with self.assertRaises(PermissionError):
                    save(target, {"after": True})
            self.assertEqual(read(target), {"before": True})
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
