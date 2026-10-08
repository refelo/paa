import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paa.cards import CardError, CardStore, fingerprint, read, save
from paa.distillation import Distillation, STATE, transcript_text
from test_cards import fixture


class DistillationTests(unittest.TestCase):
    def test_register_migration_duplicate_and_revision(self):
        from paa.source_registry import migrate, register
        before = (self.store / STATE).read_bytes()
        migrate(self.task)
        self.assertEqual((self.store / STATE).read_bytes(), before)
        fresh = self.input / 'new.txt'
        fresh.write_text('新资料：曝光与背景层次。', encoding='utf-8')
        planned = register(self.task, [fresh], collection='新的收藏夹')
        self.assertFalse(planned['applied'])
        applied = register(self.task, [fresh], collection='新的收藏夹', apply=True)
        sid = applied['new_sources'][0]
        self.assertEqual(register(self.task, [fresh], collection='新的收藏夹', apply=True)['new_sources'], [])
        self.assertTrue(any(r['id'] == sid for r in self.task.sources()))
        self.task.show(sid)
        batch = self.batch('NEW', sid, 'N1')
        self.task.stage(batch)
        first_video = read(self.store / 'video-aliases.json')['source_to_video'][sid]
        fresh.write_text('修订资料：曝光与背景层次，加条件。', encoding='utf-8')
        revised = register(self.task, [fresh], collection='新的收藏夹', apply=True)['new_sources'][0]
        self.assertEqual(read(self.store / 'video-aliases.json')['source_to_video'][revised], first_video)
        self.assertIn('新资料', self.task.show(sid)['text'])

    def test_srt_view_preserves_spoken_numbers_and_all_text(self):
        data = "1\n00:00:00,000 --> 00:00:01,000\n123\n第二行\n\n2\n00:00:01.000 --> 00:00:02.000\nA --> B是内容\n".encode()
        self.assertEqual(transcript_text(data, ".srt"), "123\n第二行\nA --> B是内容")
        self.assertEqual(transcript_text(data, ".txt"), data.decode())
        self.assertEqual(transcript_text(b'1\n00:00:0.00 --> 00:00:0.20\n123\n', '.srt'), '123')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "store"
        (self.store / "cards").mkdir(parents=True)
        c = fixture()
        c.pop("source_refs")
        c.pop("author")
        c.update(
            schema_version=2, video_ids=["v_" + "a" * 16], agent_notes=["领域：摄影"]
        )
        save(self.store / "cards/T1.json", c)
        save(
            self.store / "video-aliases.json",
            {"schema_version": 1, "source_to_video": {"OLD": "v_" + "a" * 16}},
        )
        save(self.store / "qa/technique-seeds.json", {"seeds": []})
        self.base = self.root / "base"
        (self.base / "reviews").mkdir(parents=True)
        self.input = self.root / "input"
        self.input.mkdir()
        self.inventory = {"root": str(self.input), "sources": {}}
        for sid, collection, owner in [
            ("D1", "甲的视频列表", "甲"),
            ("D2", "0_摄影-实战", None),
            ("D3", "excluded", None),
        ]:
            p = self.input / (sid + ".txt")
            p.write_text(
                "完整语境。条件和做法：" + sid + "。不保证结果。", encoding="utf-8"
            )
            self.inventory["sources"][sid] = {
                "source_id": sid,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "suffix": ".txt",
                "state": "unread",
                "text_chars": 25,
                "occurrences": [
                    {"path": str(p), "collection": collection, "owner": owner}
                ],
            }
        save(self.base / "inventory.json", self.inventory)
        save(self.base / "scope-current.json", {"excluded_collections": ["excluded"]})
        self.task = Distillation(self.store, self.base)
        self.task.initialize()

    def batch(self, bid="B1", sid="D1", did="TS1"):
        return {
            "batch_id": bid,
            "full_text_read": True,
            "sources": [{"id": sid, "decision": "read_pending_synthesis"}],
            "drafts": [
                {
                    "id": did,
                    "kind": "technique",
                    "domain": "摄影",
                    "title": "有条件的方法",
                    "content": "场景中的目标、做法和成立边界。",
                    "source_ids": [sid],
                }
            ],
        }

    def staged(self):
        self.task.show("D1")
        self.task.stage(self.batch())

    def plan(self, patch_value=None):
        return {
            "queue_etag": self.task.status()["queue_etag"],
            "operations": [
                {
                    "target_id": "T1",
                    "expected_etag": self.task.store.get("T1")["etag"],
                    "draft_ids": ["TS1"],
                    "patch": patch_value or {},
                }
            ],
        }

    def test_full_read_required_stage_never_loads_or_writes_old_cards(self):
        before = (self.store / "cards/T1.json").read_bytes()
        self.task.show("D1", 0, 5)
        with self.assertRaisesRegex(CardError, "Full source"):
            self.task.stage(self.batch())
        self.task.show("D1", 5)
        with patch.object(
            CardStore,
            "snapshot",
            side_effect=AssertionError("Reading stage must not retrieve cards"),
        ):
            self.task.stage(self.batch())
        self.assertEqual(before, (self.store / "cards/T1.json").read_bytes())
        self.assertEqual(
            self.task.queue()[0]["content"], self.batch()["drafts"][0]["content"]
        )
        self.assertNotIn("video_ids", self.task.queue()[0])
        self.assertTrue(self.task.stage(self.batch())["already_staged"])
        with self.assertRaises(CardError):
            self.task.stage(self.batch() | {"full_text_read": False})

    def test_preview_dedup_count_only_update_and_stale_plan(self):
        self.staged()
        self.task.store.vector_build(
            "test", "test", lambda texts, role: [[1, 0] for _ in texts]
        )
        before = self.task.store.get("T1")
        p = self.plan()
        self.assertFalse(self.task.merge(p)["applied"])
        self.assertEqual(before, self.task.store.get("T1"))
        self.task.merge(p, True)
        after = self.task.store.get("T1")
        self.assertEqual(after["card"]["mention_count"], 2)
        self.assertEqual(before["card"]["method"], after["card"]["method"])
        self.assertEqual(
            self.task.store.vector([1, 0], "test", "test")[0]["card"]["mention_count"],
            2,
        )
        self.assertEqual([], self.task.queue())
        with self.assertRaises(CardError):
            self.task.merge(p, True)

    def test_same_video_across_batches_does_not_increment_card_or_revision(self):
        self.staged()
        self.task.merge(self.plan(), True)
        before = self.task.store.get("T1")
        self.task.show("D2")
        b = self.batch("B2", "D2", "TS2")
        b["sources"][0]["same_video_as"] = "D1"
        self.task.stage(b)
        p = self.plan()
        p["operations"][0]["draft_ids"] = ["TS2"]
        self.task.merge(p, True)
        self.assertEqual(before, self.task.store.get("T1"))

    def test_one_contextual_draft_can_support_multiple_cards_in_one_merge(self):
        self.staged()
        p = self.plan()
        body = {k: v for k, v in self.task.store.get("T1")["card"].items()
                if k not in ("id", "revision", "status", "mention_count")}
        p["operations"].append({"target_id": "T2", "expected_etag": None,
                                "draft_ids": ["TS1", "TS1"], "patch": body})
        self.task.merge(p, True)
        self.assertEqual(self.task.store.get("T1")["card"]["mention_count"], 2)
        self.assertEqual(self.task.store.get("T2")["card"]["mention_count"], 1)
        d = self.task.state()["drafts"]["TS1"]
        self.assertEqual(d["merged_into"], ["T1", "T2"])
        self.assertEqual([], self.task.queue())
        with self.assertRaisesRegex(CardError, "consumed"):
            self.task.merge(self.plan(), True)

    def test_new_card_and_absorption_keep_conditional_branches_and_union(self):
        self.staged()
        body = {
            k: v
            for k, v in fixture().items()
            if k not in ("id", "revision", "status", "source_refs", "author")
        }
        body["agent_notes"] = ["领域：摄影"]
        body["method"] = ["条件A：保留暗部。", "条件B：补亮人脸。"]
        p = self.plan(body)
        op = p["operations"][0]
        op.update(
            target_id="T2",
            expected_etag=None,
            absorb=[{"id": "T1", "etag": self.task.store.get("T1")["etag"]}],
        )
        self.task.merge(p, True)
        self.assertEqual(body["method"], self.task.store.get("T2")["card"]["method"])
        self.assertEqual(self.task.store.get("T2")["card"]["mention_count"], 2)
        with self.assertRaises(CardError):
            self.task.store.get("T1")

    def test_interruption_blocks_partial_reads_and_recovers_whole_merge(self):
        self.staged()
        import paa.cards as module

        original = module.save

        def interrupted(path, data):
            if Path(path).resolve() == (self.store / "cards/T1.json").resolve():
                raise OSError("simulated interruption")
            original(path, data)

        with patch.object(module, "save", interrupted):
            with self.assertRaises(OSError):
                self.task.merge(self.plan(), True)
        with self.assertRaisesRegex(CardError, "transaction"):
            self.task.store.get("T1")
        self.assertTrue(self.task.store.recover()["recovered"])
        self.assertEqual(self.task.store.get("T1")["card"]["mention_count"], 2)
        self.assertEqual([], self.task.queue())

    def test_source_scope_change_and_collection_preference_are_rejected(self):
        with self.assertRaises(CardError):
            self.task.show("D3")
        (self.input / "D1.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(CardError):
            self.task.show("D1")
        self.task.show("D2")
        b = self.batch("B2", "D2")
        b["drafts"][0].update(kind="preference", author="甲")
        with self.assertRaises(CardError):
            self.task.stage(b)

    def test_catalog_is_small_and_drafts_never_enter_retrieval(self):
        self.staged()
        self.assertEqual([], self.task.store.keyword("成立边界"))
        c = self.task.store.catalog()[0]
        self.assertEqual(c["id"], "T1")
        self.assertNotIn("method", c)
        self.assertNotIn("video_ids", c)

    def test_kind_change_and_foreign_queue_rejected(self):
        self.staged()
        p = self.plan(
            {"kind": "preference", "author": "甲", "support_scope": "有限综合"}
        )
        with self.assertRaises(CardError):
            self.task.merge(p, True)
        state = read(self.store / STATE)
        foreign = copy.deepcopy(state)
        foreign["store_id"] = "other"
        save(self.store / STATE, foreign)
        with self.assertRaises(CardError):
            self.task.status()
        self.assertEqual(fingerprint(state), p["queue_etag"])
