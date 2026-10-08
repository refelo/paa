import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from contextlib import closing
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image

from paa.library import LOCAL
from paa.search import Search, build_index
from paa.notes import apply_draft, compose, MARKER
from paa.cards import CardError, read, save
from paa.voyage import VoyageEncoder
from test_source_integrity import FixtureEncoder


class CooperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.state = base / 'source.library', base / 'state'
        self.metadata = self.root / 'images/A.info/metadata.json'
        self.metadata.parent.mkdir(parents=True)
        Image.new('RGB', (40, 60), 'blue').save(self.metadata.parent / 'a.png')
        self.data = {'id': 'A', 'name': 'a', 'ext': 'png', 'tags': ['光影'],
                     'annotation': '这里只参考裙摆，脸部处理不喜欢。',
                     'comments': [{'id': 'c1', 'x': 1, 'y': 2, 'width': 10,
                                   'height': 20, 'annotation': '这里的过渡'}]}
        self.write()
        self.settings = {'library': self.root, 'allow_preview_to_agent': True}
        build_index(self.settings, 1, self.state, FixtureEncoder())
        self.engine = Search(self.settings, self.state, FixtureEncoder())

    def write(self):
        self.metadata.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')

    def test_retrieved_image_includes_current_raw_manual_context(self):
        before = self.metadata.read_bytes()
        first = self.engine.search('蓝色')['results'][0]
        context = first['context']
        self.assertEqual(context['tags'], ['光影'])
        self.assertEqual(context['annotation'], self.data['annotation'])
        self.assertEqual(context['comments'], self.data['comments'])
        self.assertNotIn('annotation_authorship', context)
        self.assertEqual(before, self.metadata.read_bytes())
        self.data['annotation'] = '新表达：只参考动作'
        self.write()
        latest = self.engine.get(first['asset_id'])
        self.assertEqual(latest['context']['annotation'], self.data['annotation'])
        self.assertNotIn('etag', latest['context'])
        self.assertEqual(latest['asset_id'], first['asset_id'])

    def test_missing_fields_are_not_claimed_to_be_empty(self):
        del self.data['comments']
        self.write()
        context = self.engine.search('蓝色')['results'][0]['context']
        self.assertIn('comments', context['missing_fields'])
        self.assertIsNone(context['comments'])

    def test_native_joint_query_and_atomic_rebuild_failure(self):
        settings = {**self.settings, 'embedding_model': VoyageEncoder.model}
        build_index(settings, 1, self.state, FixtureEncoder())
        native = unittest.mock.Mock()
        native.encode.return_value = FixtureEncoder().encode(text=['blue'])
        engine = Search(settings, self.state, native)
        with closing(sqlite3.connect(self.state / 'index.sqlite3')) as db:
            asset_id = db.execute('SELECT asset_id FROM images').fetchone()[0]
        engine.search('换一个动作', reference_id=asset_id)
        self.assertEqual(native.encode.call_count, 1)
        self.assertEqual(native.encode.call_args.kwargs['text'], ['换一个动作'])
        self.assertEqual(len(native.encode.call_args.kwargs['images']), 1)
        before = (self.state / 'index.sqlite3').read_bytes()
        from paa.library import item_source
        original, _ = item_source(self.settings['library'], 'A')
        Image.new('RGB', (30, 40), 'orange').save(original)
        native.encode.side_effect = CardError('simulated provider failure')
        with self.assertRaises(CardError):
            build_index(settings, None, self.state, native, rebuild=True)
        self.assertEqual((self.state / 'index.sqlite3').read_bytes(), before)
        self.assertEqual(list(self.state.glob('index.build-*')), [])

    def test_lexical_tags_and_annotation_work_without_encoder(self):
        self.engine.encoder = None
        with patch('paa.voyage.VoyageEncoder', side_effect=AssertionError('must not load')):
            rows = self.engine.search('裙摆', route='metadata')['results']
            self.assertEqual(rows[0]['eagle_id'], 'A')
            self.assertEqual(rows[0]['matched_by'], ['metadata'])
            self.assertEqual(self.engine.search('', route='metadata', tags=['光影'])['results'][0]['eagle_id'], 'A')
            self.assertEqual(self.engine.search('裙摆', route='metadata', tags=['不存在'])['results'], [])
            self.data['annotation'] = '已经改写'
            self.write()
            self.assertEqual(self.engine.search('裙摆', route='metadata')['results'], [])

    def test_draft_preserves_original_and_only_writes_note(self):
        row = self.engine.search('蓝色')['results'][0]
        draft = self.state / 'draft.json'
        save(draft, {'asset_id': row['asset_id'], 'body': '裙摆的展开方向与人物转身形成呼应。'})
        workspace = SimpleNamespace(images=self.engine, settings=self.settings)
        calls = []
        def api(endpoint, data=None):
            calls.append((endpoint, data))
            if endpoint == 'library/info':
                return {'library': {'path': str(self.root)}}
            self.assertEqual(set(data), {'id', 'annotation'})
            self.data['annotation'] = data['annotation']
            self.write()
        preview = apply_draft(workspace, draft)
        self.assertFalse(preview['applied'])
        self.assertTrue(preview['annotation'].startswith(self.data['annotation']))
        result = apply_draft(workspace, draft, apply=True, api=api)
        self.assertTrue(result['applied'])
        self.assertEqual(self.data['tags'], ['光影'])
        apply_draft(workspace, draft, apply=True, api=api)
        self.assertEqual(self.data['annotation'].count(MARKER), 1)
        self.assertEqual(sum(endpoint == 'item/update' for endpoint, _ in calls), 2)

    def test_wrong_live_library_is_never_written(self):
        row = self.engine.search('蓝色')['results'][0]
        draft = self.state / 'draft.json'
        save(draft, {'asset_id': row['asset_id'], 'body': '临时分析'})
        api = unittest.mock.Mock(return_value={'library': {'path': str(self.state)}})
        with self.assertRaisesRegex(ValueError, '当前库'):
            apply_draft(SimpleNamespace(images=self.engine, settings=self.settings), draft, apply=True, api=api)
        api.assert_called_once_with('library/info')

    def test_note_replacement_preserves_human_prefix(self):
        original = '用户前文\n\n' + MARKER + '\n旧AI文字'
        value = compose(original, '新AI文字')
        self.assertEqual(value, '用户前文\n\n' + MARKER + '\n新AI文字')
        self.assertEqual(value.count(MARKER), 1)
        with self.assertRaises(ValueError):
            compose(original, '新内容' + MARKER)

    def test_expanded_observation_is_not_truncated_or_rejected(self):
        body = '这里展开图中关系及其具体借鉴方式。' * 260
        self.assertGreater(len(body), 3000)
        prefix = '  人工原文\n包含原有格式  \n\n'
        self.assertEqual(compose(prefix + MARKER + '\n旧段', body),
                         prefix + MARKER + '\n' + body)


class VoyageQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_explicit_unlimited_budget_preserves_accounting_and_offline_gate(self):
        import io
        save(self.root / 'usage.json', {'estimated_usd': 3, 'events': []})
        encoder = VoyageEncoder(self.root, allow_online=True, budget_usd=None)
        self.assertIsNone(encoder.preflight(['new'], 'document')['budget_usd'])
        response = io.BytesIO(json.dumps({'data': [{'index': 0, 'embedding': [1.] + [0.] * 1023}]}).encode())
        with patch('paa.voyage.load_key', return_value='fixture-only'):
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = response
                encoder(['new'], 'document')
                opener.assert_called_once()
        self.assertGreater(read(self.root / 'usage.json')['estimated_usd'], 3)
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaises(CardError):
                VoyageEncoder(self.root, budget_usd=None)(['uncached'], 'query')
            opener.assert_not_called()
        with patch('urllib.request.build_opener') as opener, patch('paa.voyage.load_key') as key:
            zero = VoyageEncoder(self.root, allow_online=True, budget_usd=0)
            self.assertEqual(zero(['new'], 'document').shape, (1, 1024))
            with self.assertRaises(CardError):
                zero(['zero-budget-uncached'], 'query')
            opener.assert_not_called()
            key.assert_not_called()
        for invalid in (True, False, -1, float('inf'), float('nan'), 'unlimited'):
            with self.subTest(invalid=invalid), self.assertRaises(CardError):
                VoyageEncoder(self.root, budget_usd=invalid)

    def test_offline_and_budget_errors_do_not_send(self):
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaises(CardError):
                VoyageEncoder(self.root)(['测试'], 'query')
            with patch.dict('os.environ', {'VOYAGE_API_KEY': 'fixture-only'}):
                with self.assertRaises(CardError):
                    VoyageEncoder(self.root, allow_online=True, budget_usd=0.0001)(['测试'], 'query')
            opener.assert_not_called()

    def test_large_image_batch_reuses_original_eight_image_caches(self):
        import numpy as np
        encoder = VoyageEncoder(self.root, budget_usd=None, role='document')
        images = [Image.new('RGB', (10, 10), (i, 0, 0)) for i in range(32)]
        self.addCleanup(lambda: [image.close() for image in images])
        def seed(contents, role):
            key, path, _ = encoder.cached(contents, role)
            vectors = np.zeros((len(contents), 1024))
            vectors[:, 0] = 1
            save(path, {'fingerprint': key, 'model': encoder.model, 'vectors': vectors.tolist()})
            return vectors
        with patch.object(encoder, 'embed', side_effect=seed):
            for start in range(0, 32, 8):
                encoder.encode(images=images[start:start + 8])
        with patch('urllib.request.build_opener') as opener:
            self.assertEqual(encoder.encode(images=images).shape, (32, 1024))
            opener.assert_not_called()
        self.assertTrue(encoder.last['cached'])
        self.assertFalse((self.root / 'usage.json').exists())

    def test_cache_replay_needs_no_key_and_failure_keeps_budget(self):
        import io
        response = io.BytesIO(json.dumps({'data': [{'index': 0, 'embedding': [1.0] + [0.0] * 1023}]}).encode())
        with patch.dict('os.environ', {'VOYAGE_API_KEY': 'fixture-only'}):
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = response
                online = VoyageEncoder(self.root, allow_online=True)
                self.assertEqual(online(['测试'], 'query').shape, (1, 1024))
                self.assertEqual(opener.return_value.open.call_count, 1)
            with patch('urllib.request.build_opener') as opener:
                self.assertEqual(VoyageEncoder(self.root)(['测试'], 'query').shape, (1, 1024))
                opener.assert_not_called()
                opener.return_value.open.side_effect = TimeoutError()
                with self.assertRaises(CardError):
                    online(['另一个查询'], 'query')
        ledger = read(self.root / 'usage.json')
        self.assertEqual(ledger['events'][-1]['status'], 'failed_or_uncertain')
        self.assertGreater(ledger['estimated_usd'], 0)
        self.assertNotIn('fixture-only', ''.join(p.read_text() for p in self.root.glob('*.json')))

    def test_card_document_batches_use_the_shared_client(self):
        import numpy as np
        encoder = VoyageEncoder(self.root)
        with patch.object(encoder, 'embed', side_effect=[np.ones((8, 1024)), np.ones((1, 1024))]) as embed:
            values = encoder([f'卡片{i}' for i in range(9)], 'document')
            self.assertEqual(values.shape, (9, 1024))
            self.assertEqual([len(call.args[0]) for call in embed.call_args_list], [8, 1])
            self.assertTrue(all(call.args[1] == 'document' for call in embed.call_args_list))


if __name__ == '__main__':
    unittest.main()
