import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from paa.cards import read, save
from paa.library import LOCAL, SourceError
from paa.note_batch import NoteBatch
from paa.notes import MARKER
from paa.search import Search


class NoteBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'fixture.library'
        for item_id in ('A', 'B', 'C'):
            directory = self.root / 'images' / (item_id + '.info')
            directory.mkdir(parents=True)
            Image.new('RGB', (20, 30), item_id == 'A' and 'red' or 'blue').save(directory / 'sample.png')
            save(directory / 'metadata.json', {'id': item_id, 'name': 'sample', 'ext': 'png',
                 'annotation': '  人工原文\n\n' + MARKER + '\n旧AI', 'tags': ['保留'], 'star': '1'})
        settings = {'library': self.root, 'allow_preview_to_agent': True}
        self.workspace = SimpleNamespace(settings=settings, images=Search(settings, self.base / 'runtime'))
        self.batch = NoteBatch(self.workspace, self.base / 'run')
        self.batch.initialize(self.root)
        self.updates = []

    def api(self, endpoint, data=None):
        if endpoint == 'library/info':
            return {'library': {'path': str(self.root)}}
        self.assertEqual(endpoint, 'item/update')
        self.assertEqual(set(data), {'id', 'annotation'})
        self.updates.append(data['id'])
        path = self.root / 'images' / (data['id'] + '.info') / 'metadata.json'
        value = read(path)
        value['annotation'] = data['annotation']
        save(path, value)

    def make_drafts(self, worker='writer-1', size=2):
        prepared = self.batch.prepare(worker, size)
        rows = read(prepared['input'])
        value = {'viewed_ids': [r['asset_id'] for r in rows],
                 'drafts': [{'asset_id': r['asset_id'], 'body': '新的充分观察。' * 520} for r in rows]}
        save(prepared['drafts'], value)
        return prepared, rows, value

    def test_all_existing_ai_included_and_scope_stays_frozen(self):
        self.assertEqual(self.batch.status()['scope_count'], 3)
        directory = self.root / 'images/D.info'
        directory.mkdir()
        Image.new('RGB', (20, 20)).save(directory / 'd.png')
        save(directory / 'metadata.json', {'id': 'D', 'name': 'd', 'ext': 'png'})
        self.batch.initialize(self.root)
        self.assertEqual(self.batch.status()['scope_count'], 3)

    def test_workers_do_not_overlap_and_same_worker_continues(self):
        prepared, rows, _ = self.make_drafts(size=2)
        other = self.batch.prepare('writer-2', 2)
        self.assertFalse(set(r['eagle_id'] for r in rows) & set(r['eagle_id'] for r in read(other['input'])))
        self.assertTrue(self.batch.prepare('writer-1')['resumed'])
        self.assertEqual(read(prepared['drafts'])['viewed_ids'], [r['asset_id'] for r in rows])
        self.batch.apply('writer-1', apply=True, api=self.api)
        self.assertEqual(self.batch.prepare('writer-1')['count'], 0)
        self.assertEqual(self.batch.status()['remaining_count'], 1)

    def test_preview_never_calls_api_and_apply_preserves_human_and_other_fields(self):
        prepared, rows, _ = self.make_drafts(size=1)
        api = Mock(side_effect=AssertionError('preview must not call API'))
        self.batch.apply('writer-1', api=api)
        api.assert_not_called()
        self.assertEqual(self.batch.status()['completed_count'], 0)
        self.batch.apply('writer-1', apply=True, api=self.api)
        self.batch.apply('writer-1', apply=True, api=self.api)
        self.assertEqual(self.updates, [rows[0]['eagle_id']])
        value = read(self.root / 'images' / (rows[0]['eagle_id'] + '.info') / 'metadata.json')
        self.assertTrue(value['annotation'].startswith('  人工原文\n\n' + MARKER))
        self.assertNotIn('旧AI', value['annotation'])
        self.assertGreater(len(value['annotation']), 3000)
        self.assertEqual(value['tags'], ['保留'])
        self.assertEqual(value['star'], '1')
        self.assertNotIn('body', json.dumps(self.batch.state()))
        next_batch = self.batch.prepare('writer-1', 1)
        self.assertNotEqual(read(next_batch['input'])[0]['eagle_id'], rows[0]['eagle_id'])
        self.assertEqual(read(prepared['drafts'])['drafts'], [])

    def test_bad_batch_fails_before_any_write(self):
        prepared, _, value = self.make_drafts()
        value['drafts'][1]['asset_id'] = value['drafts'][0]['asset_id']
        save(prepared['drafts'], value)
        with self.assertRaisesRegex(SourceError, '重复'):
            self.batch.apply('writer-1', apply=True, api=self.api)
        self.assertEqual(self.updates, [])

    def test_replaced_image_rejected_then_requires_fresh_draft(self):
        prepared, rows, _ = self.make_drafts(size=1)
        original = self.root / 'images' / (rows[0]['eagle_id'] + '.info') / 'sample.png'
        Image.new('RGB', (10, 10), 'green').save(original)
        with self.assertRaisesRegex(SourceError, '替换'):
            self.batch.apply('writer-1', apply=True, api=self.api)
        self.assertEqual(self.updates, [])
        self.batch.reconcile('writer-1')
        self.batch.prepare('writer-1')
        self.assertNotEqual(read(prepared['input'])[0]['asset_id'], rows[0]['asset_id'])
        self.assertEqual(read(prepared['drafts'])['drafts'], [])

    def test_interruption_after_write_is_read_back_without_repeating_write(self):
        _, rows, _ = self.make_drafts(size=1)
        def interrupted(endpoint, data=None):
            result = self.api(endpoint, data)
            if endpoint == 'item/update':
                raise OSError('connection lost after update')
            return result
        with self.assertRaises(OSError):
            self.batch.apply('writer-1', apply=True, api=interrupted)
        with self.assertRaisesRegex(SourceError, 'reconcile'):
            self.batch.prepare('writer-1')
        result = self.batch.reconcile('writer-1')
        self.assertEqual(result['completed_count'], 1)
        self.assertEqual(result['issues'], {})
        self.batch.apply('writer-1', apply=True, api=self.api)
        self.assertEqual(self.updates, [rows[0]['eagle_id']])

    def test_incomplete_view_declaration_and_wrong_library_do_not_write(self):
        prepared, _, value = self.make_drafts(size=1)
        value['viewed_ids'] = []
        save(prepared['drafts'], value)
        with self.assertRaisesRegex(SourceError, '覆盖'):
            self.batch.apply('writer-1', apply=True, api=self.api)
        self.assertEqual(self.updates, [])
        with self.assertRaisesRegex(SourceError, '不一致'):
            self.batch.initialize(self.base)
        with patch.dict(self.workspace.settings, {'library': self.base}):
            with self.assertRaisesRegex(SourceError, '其他图库'):
                self.batch.status()

    def test_lock_refuses_another_coordinator(self):
        with self.batch.lock():
            with self.assertRaisesRegex(SourceError, '协调者'):
                with self.batch.lock(wait_seconds=0):
                    pass

    def test_disjoint_workers_write_concurrently_without_losing_progress(self):
        first, first_rows, _ = self.make_drafts(worker='writer-1', size=1)
        second, second_rows, _ = self.make_drafts(worker='writer-2', size=1)
        self.assertNotEqual(first_rows[0]['eagle_id'], second_rows[0]['eagle_id'])
        together = Barrier(2)

        def simultaneous_api(endpoint, data=None):
            if endpoint == 'item/update':
                together.wait(timeout=5)
            return self.api(endpoint, data)

        with ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(self.batch.apply, 'writer-1', apply=True, api=simultaneous_api)
            right = pool.submit(self.batch.apply, 'writer-2', apply=True, api=simultaneous_api)
            self.assertEqual(left.result(timeout=10)['count'], 1)
            self.assertEqual(right.result(timeout=10)['count'], 1)

        self.assertEqual(set(self.updates), {first_rows[0]['eagle_id'], second_rows[0]['eagle_id']})
        self.assertEqual(self.batch.status()['completed_count'], 2)
        notes = read(self.workspace.images.local / 'notes-state.json')['completed']
        self.assertEqual(set(notes), set(self.updates))
        self.assertEqual(read(first['drafts'])['viewed_ids'], [first_rows[0]['asset_id']])
        self.assertEqual(read(second['drafts'])['viewed_ids'], [second_rows[0]['asset_id']])

    def test_overlapping_worker_assignments_never_write(self):
        _, rows, _ = self.make_drafts(worker='writer-1', size=1)
        state = self.batch.state()
        state['inflight']['writer-2'] = [rows[0]['eagle_id']]
        save(self.batch.path, state)
        with self.assertRaisesRegex(SourceError, '重复分配'):
            self.batch.apply('writer-1', apply=True, api=self.api)
        self.assertEqual(self.updates, [])


if __name__ == '__main__':
    unittest.main()
