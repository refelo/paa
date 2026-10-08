import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paa.credentials import save_key, load_key


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
class CredentialTests(unittest.TestCase):
    def test_encrypted_roundtrip_and_environment_override(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'credential.dpapi'
            with patch('paa.credentials.KEY_FILE', path), patch.dict(os.environ, {}, clear=True):
                save_key('fixture-only-not-a-real-secret')
                self.assertNotIn(b'fixture-only', path.read_bytes())
                self.assertEqual(load_key(), 'fixture-only-not-a-real-secret')
                with patch.dict(os.environ, {'VOYAGE_API_KEY': 'environment-fixture'}):
                    self.assertEqual(load_key(), 'environment-fixture')
                self.assertEqual(list(Path(temp).glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
