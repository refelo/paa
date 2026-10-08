import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paa.cards import CardError, CardStore, read, save


def fixture(card_id="T1", title="阴天窗边柔光"):
    return dict(
        id=card_id,
        title=title,
        author="讲者待确认",
        kind="technique",
        problem="人物太暗",
        method=["移到窗边观察侧光"],
        conditions=["窗光可用"],
        limits=["机位需要看图判断"],
        agent_notes=[],
        visual_dependency="partial",
        status="active",
        revision=1,
        source_refs=[
            dict(
                source_id="S1",
                line_start=1,
                line_end=3,
                source_sha256="a" * 64,
                title="原文标题不参与搜索",
                time_start="0s",
                time_end="9s",
            )
        ],
    )


class CardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "cards").mkdir()
        save(self.root / "cards/first.json", fixture())
        self.store = CardStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        return self.store.vector_build(
            "test", "test-model", lambda texts, role: [[1, 0] for _ in texts]
        )

    def test_raw_archive_and_evidence_are_never_searched_or_read(self):
        save(self.root / "provenance.json", {"raw_text": "ARCHIVEONLYTOKEN"})
        save(self.root / "qa/evidence.json", {"quote": "QUOTATIONONLYTOKEN"})
        import paa.cards as module

        original = module.read
        opened = []

        def tracked(path):
            opened.append(Path(path))
            return original(path)

        with patch.object(module, "read", tracked):
            self.assertEqual([], self.store.keyword("ARCHIVEONLYTOKEN"))
            self.assertEqual([], self.store.keyword("QUOTATIONONLYTOKEN"))
            self.store.get("T1")
            self.build()
            self.store.vector([1, 0], "test", "test-model")
        self.assertTrue(
            all(
                p.is_relative_to(self.root / "cards")
                or p.is_relative_to(self.root / ".index")
                or p == self.root / "store.json"
                for p in opened
            )
        )
        c = fixture()
        c["raw_text"] = "accidental import"
        save(self.root / "cards/first.json", c)
        with self.assertRaises(CardError):
            self.store.keyword("窗边")

    def test_rename_and_move_preserve_identity_but_content_replacement_invalidates(
        self,
    ):
        self.build()
        (self.root / "cards/nested").mkdir()
        (self.root / "cards/first.json").rename(self.root / "cards/nested/renamed.json")
        self.assertEqual("T1", self.store.vector([1, 0], "test", "test-model")[0]["id"])
        c = fixture(title="晴天硬光")
        save(self.root / "cards/nested/renamed.json", c)
        with self.assertRaises(CardError):
            self.store.vector([1, 0], "test", "test-model")
        self.build()
        self.assertEqual("晴天硬光", self.store.get("T1")["card"]["title"])

    def test_update_cas_history_deactivate_reactivate_and_rebuild(self):
        self.build()
        old = self.store.get("T1")
        now = self.store.update("T1", {"problem": "窗户直射光太硬"}, old["etag"])
        with self.assertRaises(CardError):
            self.store.update("T1", {"status": "inactive"}, old["etag"])
        with self.assertRaises(CardError):
            self.store.vector([1, 0], "test", "test-model")
        self.assertEqual(
            old["card"], read(next((self.root / "history/T1").glob("*.json")))
        )
        stopped = self.store.update("T1", {"status": "inactive"}, now["etag"])
        self.assertEqual([], self.store.keyword("窗边"))
        with self.assertRaises(CardError):
            self.store.get("T1")
        with self.assertRaises(CardError):
            self.store.vector([1, 0], "test", "test-model")
        self.store.update("T1", {"status": "active"}, stopped["etag"])
        self.build()
        self.assertEqual("T1", self.store.vector([1, 0], "test", "test-model")[0]["id"])

    def test_foreign_index_model_dimensions_tampering_and_deleted_cards_rejected(self):
        self.build()
        index_path = self.store.vector_path("test")
        index = read(index_path)
        with self.assertRaises(CardError):
            self.store.vector([1, 0], "test", "other-model")
        with self.assertRaises(CardError):
            self.store.vector([1, 0, 0], "test", "test-model")
        for field, value in [
            ("store_id", "other-store"),
            ("ids", ["T2"]),
            ("vectors", [[0, 1]]),
        ]:
            bad = copy.deepcopy(index)
            bad[field] = value
            save(index_path, bad)
            with self.assertRaises(CardError):
                self.store.vector([1, 0], "test", "test-model")
        save(index_path, index)
        (self.root / "cards/first.json").unlink()
        with self.assertRaises(CardError):
            self.store.vector([1, 0], "test", "test-model")

    def test_store_replaced_at_same_path_is_rejected(self):
        self.build()
        save(self.root / "store.json", {"schema_version": 1, "store_id": "new-store"})
        with self.assertRaises(CardError):
            self.store.get("T1")
        with self.assertRaises(CardError):
            CardStore(self.root).vector([1, 0], "test", "test-model")

    def test_duplicate_id_and_invalid_provenance_rejected(self):
        save(self.root / "cards/duplicate.json", fixture())
        with self.assertRaises(CardError):
            self.store.snapshot()
        (self.root / "cards/duplicate.json").unlink()
        c = fixture()
        c["source_refs"][0]["original_text"] = "hidden raw text"
        save(self.root / "cards/first.json", c)
        with self.assertRaises(CardError):
            self.store.snapshot()

    def test_concurrent_writer_and_changes_during_encoding_are_rejected(self):
        with self.store.lock():
            with self.assertRaises(CardError):
                self.store.update(
                    "T1", {"title": "新标题"}, self.store.get("T1")["etag"]
                )

        def racing(texts, role):
            c = fixture(title="外部编辑")
            save(self.root / "cards/first.json", c)
            return [[1, 0]]

        with self.assertRaises(CardError):
            self.store.vector_build("test", "test-model", racing)
        self.assertFalse(self.store.vector_path("test").exists())

    def test_v2_hides_video_ids_in_get_keyword_and_vector_but_counts_once(self):
        card = fixture()
        del card['source_refs']
        del card['author']
        card.update(schema_version=2, video_ids=['v_' + 'a' * 16, 'v_' + 'b' * 16])
        save(self.root / 'cards/first.json', card)
        self.build()
        rows = [self.store.get('T1'), self.store.keyword('窗边')[0],
                self.store.vector([1, 0], 'test', 'test-model')[0]]
        for row in rows:
            self.assertEqual(row['card']['mention_count'], 2)
            self.assertNotIn('source_refs', row['card'])
            self.assertNotIn('video_ids', row['card'])
            self.assertEqual(row['card']['method'], card['method'])
        self.assertEqual([], self.store.keyword('aaaaaaaaaaaaaaaa'))
        before = self.store.get('T1')
        # Two cards sharing one video must contribute three videos, not four.
        merged = sorted(set(card['video_ids']) | {'v_' + 'b' * 16, 'v_' + 'c' * 16})
        updated = self.store.update('T1', {'video_ids': merged}, before['etag'])
        self.assertEqual(updated['card']['mention_count'], 3)
        with self.assertRaises(CardError):
            self.store.update('T1', {'problem': 'stale edit'}, before['etag'])
        self.assertEqual(self.store.vector([1, 0], 'test', 'test-model')[0]['card']['mention_count'], 3)
        self.assertEqual(card, read(next((self.root / 'history/T1').glob('*.json'))))

    def test_v2_rejects_duplicate_video_ids_and_reintroduction_of_legacy_refs(self):
        card = fixture()
        refs = card.pop('source_refs')
        card.update(schema_version=2, video_ids=['v_' + 'a' * 16])
        save(self.root / 'cards/first.json', card)
        current = self.store.get('T1')
        for patch_value in ({'video_ids': card['video_ids'] * 2},
                            {'source_refs': refs}, {'mention_count': 50}):
            with self.assertRaises(CardError):
                self.store.update('T1', patch_value, current['etag'])
        self.assertEqual(current, self.store.get('T1'))

    def test_v2_preference_keeps_person_and_limited_support_scope(self):
        card = fixture()
        del card['source_refs']
        card.update(schema_version=2, video_ids=['v_' + 'a' * 16],
                    kind='preference', support_scope='有限字幕综合')
        save(self.root / 'cards/first.json', card)
        self.assertEqual(card['author'], self.store.get('T1')['card']['author'])
        del card['author']
        save(self.root / 'cards/first.json', card)
        with self.assertRaises(CardError):
            self.store.get('T1')


if __name__ == "__main__":
    unittest.main()
