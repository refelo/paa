import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from paa.library import LOCAL, SourceError, item_source, load_image, verify_record
from paa.search import Search, build_index


class FixtureEncoder:
    """Tiny deterministic vectors for source-integrity tests, not retrieval-quality evidence."""
    def encode(self, *, images=None, text=None):
        values = [np.asarray(image).mean(axis=(0, 1)) + 1 for image in images] if images is not None else [[1, 1, 255] for _ in text]
        vectors = np.asarray(values, dtype=np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


class SourceIntegrityTests(unittest.TestCase):
    def setUp(self):
        LOCAL.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.assertTrue(self.base.resolve().is_relative_to(LOCAL.resolve()))
        self.root = self.base / "test.library"
        self.local = self.base / "state"
        self.root.mkdir()
        self.settings = {"library": self.root, "allow_preview_to_agent": True}
        self.make_item("IMAGE1", "red")
        self.make_item("IMAGE2", "blue")

    def make_item(self, item_id, color):
        folder = self.root / "images" / f"{item_id}.info"
        folder.mkdir(parents=True)
        Image.new("RGB", (32, 48), color).save(folder / "sample.png")
        (folder / "metadata.json").write_text(json.dumps({"id": item_id, "name": "sample", "ext": "png", "isDeleted": False}), encoding="utf-8")

    def index(self):
        return build_index(self.settings, 2, self.local, FixtureEncoder())

    def digest_source(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}

    def test_index_and_preview_leave_source_unchanged_and_ids_remain_stable(self):
        before = self.digest_source()
        self.index()
        engine = Search(self.settings, self.local, FixtureEncoder())
        first = engine.search("蓝色", limit=2)["results"]
        blue_id = first[0]["asset_id"]
        self.assertEqual(first[0]["eagle_id"], "IMAGE2")
        self.assertEqual(engine.get(blue_id)["source_path"], first[0]["source_path"])
        following = engine.search(reference_id=blue_id, limit=2)["results"]
        self.assertTrue(all(r["asset_id"] != blue_id for r in following))
        self.assertEqual(before, self.digest_source())

    def test_replaced_file_with_same_timestamp_is_rejected(self):
        record, image = load_image(self.root, "IMAGE1")
        image.close()
        path, _ = item_source(self.root, "IMAGE1")
        stat = path.stat()
        Image.new("RGB", (32, 48), "green").save(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(SourceError):
            verify_record(self.root, record)

    def test_changed_library_cannot_reuse_old_index(self):
        self.index()
        another = self.base / "another.library"
        another.mkdir()
        engine = Search({**self.settings, "library": another}, self.local, FixtureEncoder())
        with self.assertRaisesRegex(SourceError, "其他素材源"):
            engine.snapshot()

    def test_deleted_image_is_not_returned_as_a_current_result(self):
        self.index()
        engine = Search(self.settings, self.local, FixtureEncoder())
        blue = engine.search("蓝色", limit=1)["results"][0]
        p = self.root / "images" / "IMAGE2.info" / "metadata.json"
        data = json.loads(p.read_text())
        data["isDeleted"] = True
        p.write_text(json.dumps(data))
        with self.assertRaises(SourceError):
            engine.get(blue["asset_id"])
        result = engine.search("蓝色", limit=2)
        self.assertEqual(result["skipped_stale"], 1)
        self.assertEqual([r["eagle_id"] for r in result["results"]], ["IMAGE1"])

    def test_metadata_cannot_escape_library(self):
        p = self.root / "images" / "IMAGE1.info" / "metadata.json"
        p.write_text(json.dumps({"id": "IMAGE1", "name": "..\\..\\outside", "ext": "png"}))
        with self.assertRaises(SourceError):
            item_source(self.root, "IMAGE1")

    def test_reference_does_not_return_an_identical_copy(self):
        self.make_item("IMAGE3", "blue")
        build_index(self.settings, 3, self.local, FixtureEncoder())
        engine = Search(self.settings, self.local, FixtureEncoder())
        first = engine.search("蓝色", limit=1)["results"][0]
        following = engine.search(reference_id=first["asset_id"], limit=3)["results"]
        self.assertEqual([r["eagle_id"] for r in following], ["IMAGE1"])

    def test_previews_require_permission_and_explicit_external_reference(self):
        self.index()
        blocked = Search({**self.settings, "allow_preview_to_agent": False}, self.local, FixtureEncoder())
        with self.assertRaisesRegex(SourceError, "未授权"):
            blocked.search("蓝色")
        (self.local / "references").mkdir()
        engine = Search(self.settings, self.local, FixtureEncoder())
        outside, _ = item_source(self.root, "IMAGE1")
        self.assertTrue(engine.search(reference_path=str(outside))['results'])
        with self.assertRaisesRegex(SourceError, "绝对文件路径"):
            engine.search(reference_path=str(outside.parent))

    def test_broad_retrieval_returns_twenty_and_can_continue_without_duplicates(self):
        for number in range(45):
            self.make_item(f"CANDIDATE{number:02}", (number + 1, 100, 200))
        for path in (self.root / 'images').glob('*/metadata.json'):
            data = json.loads(path.read_text())
            data['annotation'] = '蓝色插画参考'
            path.write_text(json.dumps(data), encoding='utf-8')
        build_index(self.settings, None, self.local, FixtureEncoder())
        engine = Search(self.settings, self.local, FixtureEncoder())
        for route in ('vector', 'metadata', 'hybrid'):
            with self.subTest(route=route):
                first = engine.search('蓝色插画参考', route=route)['results']
                self.assertEqual(len(first), 20)
                ids = [row['asset_id'] for row in first]
                more = engine.search('蓝色插画参考', route=route, exclude_ids=ids)['results']
                self.assertEqual(len(more), 20)
                self.assertFalse(set(ids) & {row['asset_id'] for row in more})
                hashes = [row['content_sha256'] for row in first + more]
                self.assertEqual(len(set(hashes)), 40)
                self.assertEqual(len(engine.search('蓝色插画参考', route=route, limit=40)['results']), 40)
        for invalid in (0, -1, 2.5, True):
            with self.assertRaises(ValueError):
                engine.search('蓝色', limit=invalid)

    def test_large_requests_are_not_capped_and_short_library_is_not_padded(self):
        for number in range(601):
            self.make_item(f"LARGE{number:03}", (number % 251 + 1, number // 251 + 1, 200))
        for path in (self.root / 'images').glob('*/metadata.json'):
            data = json.loads(path.read_text())
            data['annotation'] = '蓝色插画参考'
            path.write_text(json.dumps(data), encoding='utf-8')
        build_index(self.settings, None, self.local, FixtureEncoder())
        engine = Search(self.settings, self.local, FixtureEncoder())
        for route in ('vector', 'metadata', 'hybrid'):
            with self.subTest(route=route):
                rows = engine.search('蓝色插画参考', route=route, limit=600)['results']
                self.assertEqual(len(rows), 600)
                self.assertEqual(len({row['content_sha256'] for row in rows}), 600)
        rows = engine.search('蓝色插画参考', route='vector', limit=1000)['results']
        self.assertEqual(len(rows), 603)


if __name__ == "__main__":
    unittest.main()
