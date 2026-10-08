import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from paa.library import LOCAL, SourceError
from paa.search import Search, build_index
from paa.notes import apply_draft, compose
from test_source_integrity import FixtureEncoder


class UnindexedLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'images.library'
        self.state = self.base / 'runtime'
        self.settings = {'library': self.root, 'allow_preview_to_agent': True}
        self.make('A', 'red', '树林中的裙摆')
        self.engine = Search(self.settings, self.state, FixtureEncoder())

    def make(self, item_id, color, note=''):
        folder = self.root / 'images' / (item_id + '.info')
        folder.mkdir(parents=True)
        Image.new('RGB', (40, 60), color).save(folder / 'image.png')
        data = {'id': item_id, 'name': 'image', 'ext': 'png',
                'annotation': note, 'tags': ['姿态'], 'star': '1', 'comments': [], 'folders': []}
        (folder / 'metadata.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return folder

    def test_status_and_get_without_vector_index_and_stale_reference_rejected(self):
        status = self.engine.status()
        self.assertEqual(status['eligible'], 1)
        self.assertEqual(status['indexed'], 0)
        row = self.engine.get('A')
        self.assertEqual(self.engine.get(row['asset_id'])['eagle_id'], 'A')
        Image.new('RGB', (40, 60), 'green').save(Path(row['source_path']))
        with self.assertRaises(SourceError):
            self.engine.get(row['asset_id'])

    def test_metadata_search_covers_unindexed_items_and_hybrid_degrades(self):
        build_index(self.settings, 1, self.state, FixtureEncoder())
        self.make('B', 'blue', '白色拱廊')
        result = self.engine.search('拱廊', route='metadata')
        self.assertEqual([x['eagle_id'] for x in result['results']], ['B'])
        (self.state / 'index.sqlite3').unlink()
        with patch('paa.voyage.VoyageEncoder', side_effect=AssertionError('no provider needed')):
            result = self.engine.search('拱廊')
        self.assertEqual(result['effective_route'], 'metadata')
        self.assertEqual([x['eagle_id'] for x in result['results']], ['B'])
        self.assertTrue(result['warnings'])

    def test_note_writes_without_index_and_never_creates_history(self):
        draft = self.state / 'draft.json'
        draft.parent.mkdir(parents=True)
        draft.write_text(json.dumps({'asset_id': 'A', 'body': '人物与树林形成明暗层次。'}), encoding='utf-8')
        metadata = self.root / 'images/A.info/metadata.json'
        before = json.loads(metadata.read_text(encoding='utf-8'))
        calls = []
        def api(endpoint, payload=None):
            if endpoint == 'library/info':
                return {'library': {'path': str(self.root)}}
            self.assertEqual(set(payload), {'id', 'annotation'})
            calls.append(payload)
            current = json.loads(metadata.read_text(encoding='utf-8'))
            current['annotation'] = payload['annotation']
            metadata.write_text(json.dumps(current, ensure_ascii=False), encoding='utf-8')
        workspace = SimpleNamespace(images=self.engine, settings=self.settings)
        result = apply_draft(workspace, draft, apply=True, api=api)
        self.assertTrue(result['applied'])
        self.assertEqual(result['annotation'], before['annotation'] + '\n\n【ai生成】\n人物与树林形成明暗层次。')
        apply_draft(workspace, draft, apply=True, api=api)
        latest = json.loads(metadata.read_text(encoding='utf-8'))
        self.assertEqual(latest['annotation'].count('【ai生成】'), 1)
        for key in ('tags', 'star', 'comments', 'folders', 'name'):
            self.assertEqual(before[key], latest[key])
        self.assertFalse((self.state / 'note-history').exists())

    def test_gif_returns_multiple_actual_frames(self):
        folder = self.root / 'images/G.info'
        folder.mkdir()
        images = [Image.new('RGB', (20, 30), c) for c in ('red', 'blue', 'green')]
        images[0].save(folder / 'motion.gif', save_all=True, append_images=images[1:], duration=100, loop=0)
        for im in images:
            im.close()
        (folder / 'metadata.json').write_text(json.dumps({'id': 'G', 'name': 'motion', 'ext': 'gif'}), encoding='utf-8')
        row = self.engine.get('G')
        self.assertEqual(row['frame_count'], 3)
        self.assertEqual(len(row['frame_previews']), 3)
        pixels = []
        for frame in row['frame_previews']:
            with Image.open(frame['preview_path']) as im:
                pixels.append(im.getpixel((0, 0)))
        self.assertEqual(len(set(pixels)), 3)

    def test_note_format_preserves_human_prefix_and_replaces_ai_part(self):
        self.assertEqual(compose('人工\n\n【ai生成】\n旧AI', '新AI'), '人工\n\n【ai生成】\n新AI')
        self.assertEqual(compose('', '新AI'), '【ai生成】\n新AI')
