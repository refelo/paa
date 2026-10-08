import json
from pathlib import Path
import tempfile
import unittest

from paa.cards import CardStore, CardError
from paa.library import LOCAL
from paa.search import build_index
from test_source_integrity import FixtureEncoder
from PIL import Image


class CountingEncoder(FixtureEncoder):
    def __init__(self):
        self.count = 0

    def encode(self, *, images=None, text=None):
        self.count += len(images or text or [])
        return super().encode(images=images, text=text)


class LifecycleTests(unittest.TestCase):
    def test_concurrent_identical_queries_send_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch, Mock
        from io import BytesIO
        import time
        from paa.voyage import VoyageEncoder
        requests = []

        def reply(*args, **kwargs):
            requests.append(1)
            time.sleep(0.2)
            return BytesIO(json.dumps({'data': [{'index': 0, 'embedding': [1.0] * 1024}]}).encode())

        opener = Mock()
        opener.open.side_effect = reply
        with patch('paa.voyage.load_key', return_value='test-only'), patch('paa.voyage.urllib.request.build_opener', return_value=opener):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(VoyageEncoder(self.root / 'cache', allow_online=True), ['same query'], 'query') for _ in range(2)]
                for future in futures:
                    self.assertEqual(future.result().shape, (1, 1024))
        self.assertEqual(len(requests), 1)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'sample.library'
        (self.library / 'images').mkdir(parents=True)
        self.settings = {'library': self.library, 'index_dir': self.root / 'runtime'}
        self.encoder = CountingEncoder()

    def add(self, item, color='red'):
        folder = self.library / 'images' / (item + '.info')
        folder.mkdir(exist_ok=True)
        Image.new('RGB', (20, 20), color).save(folder / 'image.png')
        (folder / 'metadata.json').write_text(json.dumps({'id': item, 'ext': 'png', 'name': 'image'}))

    def test_incremental_and_partial_never_shrink(self):
        self.add('A')
        self.add('B', 'green')
        build_index(self.settings, encoder=self.encoder)
        self.assertEqual(self.encoder.count, 2)
        build_index(self.settings, encoder=self.encoder)
        self.assertEqual(self.encoder.count, 2)
        self.add('C', 'blue')
        result = build_index(self.settings, limit=1, encoder=self.encoder)
        self.assertGreaterEqual(result['indexed'], 2)
        result = build_index(self.settings, encoder=self.encoder)
        self.assertEqual(result['indexed'], 3)
        self.assertEqual(self.encoder.count, 3)
        self.add('A', 'black')
        build_index(self.settings, encoder=self.encoder)
        self.assertEqual(self.encoder.count, 4)

    def test_metadata_change_reuses_vectors_and_deleted_removed(self):
        self.add('A')
        build_index(self.settings, encoder=self.encoder)
        p = self.library / 'images/A.info/metadata.json'
        value = json.loads(p.read_text())
        value['annotation'] = 'new'
        p.write_text(json.dumps(value))
        build_index(self.settings, encoder=self.encoder)
        self.assertEqual(self.encoder.count, 1)
        self.add('B')
        build_index(self.settings, encoder=self.encoder)
        value['isDeleted'] = True
        p.write_text(json.dumps(value))
        self.assertEqual(build_index(self.settings, encoder=self.encoder)['indexed'], 1)

    def test_readonly_store_does_not_initialize(self):
        root = self.root / 'cards'
        (root / 'cards').mkdir(parents=True)
        with self.assertRaises(CardError):
            CardStore(root, initialize=False)
        self.assertFalse((root / 'store.json').exists())


if __name__ == '__main__':
    unittest.main()
