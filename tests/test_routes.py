import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from paa.cards import CardError
from paa.library import LOCAL
from paa.search import Search, build_index
from test_source_integrity import FixtureEncoder


class DualRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / 'sample.library'
        for name, color in [('A', 'red'), ('B', 'blue'), ('C', 'cyan')]:
            folder = self.library / 'images' / (name + '.info')
            folder.mkdir(parents=True)
            Image.new('RGB', (30, 40), color).save(folder / 'image.png')
            (folder / 'metadata.json').write_text(json.dumps({
                'id': name, 'name': 'image', 'ext': 'png',
                'annotation': '特别的扇形裙摆' if name == 'A' else '',
            }, ensure_ascii=False), encoding='utf-8')
        self.settings = {'library': self.library, 'allow_preview_to_agent': True}
        self.state = self.root / 'state'
        build_index(self.settings, 3, self.state, FixtureEncoder())
        self.engine = Search(self.settings, self.state, FixtureEncoder())

    def test_default_keeps_independent_note_hit_outside_vector_top_two(self):
        vector = self.engine.search('扇形裙摆', limit=2, route='vector')
        self.assertNotIn('A', [r['eagle_id'] for r in vector['results']])
        mixed = self.engine.search('扇形裙摆', limit=2)
        self.assertEqual([r['eagle_id'] for r in mixed['results']], ['B', 'A'])
        self.assertEqual([r['selected_by'] for r in mixed['results']], ['vector', 'metadata'])
        self.assertEqual(mixed['results'][1]['route_ranks']['metadata'], 1)

    def test_reference_only_does_not_create_random_note_candidates(self):
        first = self.engine.search('anything', route='vector', limit=1)['results'][0]
        result = self.engine.search(reference_id=first['asset_id'], limit=2)
        self.assertTrue(all(r['selected_by'] == 'vector' for r in result['results']))
        self.assertTrue(all(r['asset_id'] != first['asset_id'] for r in result['results']))

    def test_vector_unavailable_still_returns_notes_with_explicit_warning(self):
        with patch.object(self.engine.encoder, 'encode', side_effect=CardError('cache unavailable')):
            result = self.engine.search('扇形裙摆', limit=2)
            self.assertEqual([r['eagle_id'] for r in result['results']], ['A'])
            self.assertTrue(result['warnings'])
            with self.assertRaises(CardError):
                self.engine.search('扇形裙摆', route='vector')

    def test_cli_output_cannot_overwrite_source_inside_project_local(self):
        from paa.__main__ import main
        config = self.root / 'settings.json'
        config.write_text(json.dumps({'library': str(self.library), 'index_dir': str(self.state),
                                      'allow_preview_to_agent': True}), encoding='utf-8')
        source = self.library / 'images/A.info/image.png'
        before = source.read_bytes()
        with patch('sys.argv', ['paa', '--settings', str(config), 'search', '裙摆',
                                '--route', 'metadata', '--output', str(source)]):
            with self.assertRaisesRegex(ValueError, '素材库'):
                main()
        self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
