import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paa.cards import CardError, CardStore, save
from paa.voyage import VoyageEncoder
from paa.workspace import Workspace
from test_cards import fixture


class CardChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        save(self.root / 'cards/T1.json', fixture())
        self.store = CardStore(self.root)
        self.workspace = Workspace.__new__(Workspace)
        self.workspace.settings = {'card_store': self.root, 'budget_usd': 0.5}
        self.workspace.images = SimpleNamespace(local=self.root, status=lambda: {})

    def build(self):
        self.store.vector_build('voyage', VoyageEncoder.model,
                                lambda texts, role: [[1] + [0] * 1023 for _ in texts])

    def test_missing_index_keeps_keywords_without_provider(self):
        with patch('paa.workspace.VoyageEncoder') as encoder:
            result = self.workspace.search_cards('窗边', route='hybrid', allow_online=True)
        encoder.assert_not_called()
        self.assertEqual(result['effective_route'], 'keyword')
        self.assertEqual(result['candidates'][0]['id'], 'T1')
        self.assertTrue(result['warnings'])

    def test_status_and_stale_or_corrupt_index_do_not_send(self):
        self.assertEqual(self.workspace.status()['card_vector_index']['state'], 'missing')
        self.build()
        self.assertEqual(self.workspace.status()['card_vector_index']['state'], 'ready')
        current = self.store.get('T1')
        self.store.update('T1', {'problem': '窗边光比'}, current['etag'])
        self.assertEqual(self.workspace.status()['card_vector_index']['state'], 'stale')
        for expected in ('stale', 'corrupt'):
            if expected == 'corrupt':
                self.store.vector_path('voyage').write_text('{broken', encoding='utf-8')
            with patch('paa.workspace.VoyageEncoder') as encoder:
                result = self.workspace.search_cards('窗边', route='hybrid', allow_online=True)
            encoder.assert_not_called()
            self.assertEqual(result['effective_route'], 'keyword')
            self.assertEqual(self.workspace.status()['card_vector_index']['state'], expected)

    def test_provider_failure_keeps_keywords_but_store_changes_fail(self):
        self.build()
        with patch('paa.workspace.VoyageEncoder') as encoder:
            encoder.return_value.side_effect = CardError('provider unavailable')
            result = self.workspace.search_cards('窗边', route='hybrid')
        self.assertEqual(result['effective_route'], 'keyword')
        self.assertTrue(result['warnings'])
        def change(*args):
            save(self.root / '.pending.json', {})
            raise CardError('provider unavailable')
        with patch('paa.workspace.VoyageEncoder') as encoder:
            encoder.return_value.side_effect = change
            with self.assertRaisesRegex(CardError, 'Interrupted'):
                self.workspace.search_cards('窗边', route='hybrid')

    def test_corrupt_card_is_not_masked(self):
        save(self.root / 'cards/T1.json', {'id': 'T1'})
        with self.assertRaises(CardError):
            self.workspace.search_cards('窗边', route='hybrid')

    def test_hybrid_deduplicates_and_retains_route_evidence(self):
        self.build()
        with patch('paa.workspace.VoyageEncoder') as encoder:
            encoder.return_value.return_value = [[1] + [0] * 1023]
            result = self.workspace.search_cards('窗边', route='hybrid')
        self.assertEqual(result['effective_route'], 'hybrid')
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['candidates'][0]['matched_by'], ['keyword', 'vector'])

    def test_rebuild_failure_preserves_index(self):
        self.build()
        before = self.store.vector_path('voyage').read_bytes()
        with patch('paa.voyage.VoyageEncoder.preflight', return_value={}):
            with patch('paa.voyage.VoyageEncoder.__call__', side_effect=CardError('failed')):
                with self.assertRaises(CardError):
                    self.workspace.index_cards(True, rebuild=True)
        self.assertEqual(before, self.store.vector_path('voyage').read_bytes())

    def test_preflight_checks_entire_input_before_sending(self):
        encoder = VoyageEncoder(self.root, allow_online=True, budget_usd=0.031)
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(CardError, '预算'):
                encoder.preflight(['card' + str(i) for i in range(9)], 'document')
            with self.assertRaises(CardError):
                encoder.preflight(['card'] * 8 + ['x' * 12001], 'document')
        opener.assert_not_called()
        self.assertFalse((self.root / 'usage.json').exists())

    def test_preflight_reuses_batches_without_key_or_ledger_changes(self):
        import io
        encoder = VoyageEncoder(self.root, allow_online=True)
        response = io.BytesIO(json.dumps({'data': [{'index': 0, 'embedding': [1] + [0] * 1023}]}).encode())
        with patch('paa.voyage.load_key', return_value='fixture-only'):
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = response
                encoder(['cached'], 'document')
        before = (self.root / 'usage.json').read_bytes()
        with patch('paa.voyage.load_key') as key:
            result = VoyageEncoder(self.root).preflight(['cached'], 'document')
        key.assert_not_called()
        self.assertEqual(result['new_requests'], 0)
        self.assertEqual(before, (self.root / 'usage.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
