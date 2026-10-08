import importlib.util
from pathlib import Path
import tempfile
import unittest
import shutil
from unittest.mock import Mock
from paa.cards import CardStore, fingerprint, read, save
from test_cards import fixture

path = Path(__file__).resolve().parents[1] / 'scripts/cloud_import.py'
spec = importlib.util.spec_from_file_location('cloud_import', path)
cloud_import = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_import)


class CloudSyncTests(unittest.TestCase):
    def test_sync_reuses_vectors_from_both_computers(self):
        with tempfile.TemporaryDirectory() as directory:
            current, incoming = Path(directory) / 'current', Path(directory) / 'incoming'
            for item in ('T1', 'T2'):
                save(current / f'cards/{item}.json', fixture(item))
            store = CardStore(current)
            store.vector_build('voyage', 'voyage-multimodal-3.5', lambda texts, role: [[1, 0] for _ in texts])
            shutil.copytree(current, incoming)
            for root, keep in ((current, 'T1'), (incoming, 'T2')):
                path = root / '.index/voyage.json'
                data = read(path)
                data.pop('checksum')
                index = data['ids'].index(keep)
                data.update(ids=[keep], vectors=[data['vectors'][index]],
                            text_hashes={keep: data['text_hashes'][keep]})
                save(path, {**data, 'checksum': fingerprint(data)})
            cloud_import.merge_card_vectors(incoming, current)
            encode = Mock(side_effect=AssertionError('unchanged cards must not call a provider'))
            result = CardStore(incoming, initialize=False).vector_build('voyage', 'voyage-multimodal-3.5', encode)
            self.assertEqual(result['reused'], 2)
            encode.assert_not_called()

    def test_removed_image_leaves_cloud_without_touching_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('kept.info', 'removed.info', 'unrelated'):
                folder = root / 'images' / name
                folder.mkdir(parents=True)
                (folder / 'test.txt').write_text('test')
            self.assertEqual(cloud_import.remove_absent_images(root, ['kept']), ['removed'])
            self.assertTrue((root / 'images/kept.info/test.txt').is_file())
            self.assertTrue((root / 'images/unrelated/test.txt').is_file())

    def test_empty_snapshot_cannot_erase_cloud(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'images/kept.info').mkdir(parents=True)
            with self.assertRaises(ValueError):
                cloud_import.remove_absent_images(root, [])
            self.assertTrue((root / 'images/kept.info').is_dir())
