"""Small, file-backed card store. Retrieval never opens the source archive."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

TEXT_FIELDS = (
    "title",
    "author",
    "problem",
    "method",
    "conditions",
    "limits",
    "agent_notes",
)
FIELDS = set(TEXT_FIELDS) | {
    "id",
    "kind",
    "source_refs",
    "visual_dependency",
    "support_scope",
    "status",
    "revision",
}
V2_FIELDS = (FIELDS - {"source_refs"}) | {"schema_version", "video_ids"}
REF_FIELDS = {
    "source_id",
    "line_start",
    "line_end",
    "source_sha256",
    "title",
    "time_start",
    "time_end",
}


class CardError(ValueError):
    pass


class CardIndexUnavailable(CardError):
    """Only a derived index is unavailable; the card store remains authoritative."""

    def __init__(self, state, message):
        super().__init__(message)
        self.state = state


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError as error:
                # Windows readers/antivirus can briefly deny replacement even
                # though the single-writer lock is held. Keep the old file
                # intact, retry the same completed temporary file, then fail.
                if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def validate(card):
    if not isinstance(card, dict):
        raise CardError("Card must be an object")
    v2 = card.get("schema_version") == 2
    if "schema_version" in card and (type(card["schema_version"]) is not int or not v2):
        raise CardError("Unsupported card schema")
    fields = V2_FIELDS if v2 else FIELDS
    if set(card) - fields:
        raise CardError(
            "Unexpected card fields; raw transcripts/evidence do not belong in cards"
        )
    required = fields - {"support_scope"}
    if v2 and card.get("kind") == "technique":
        required -= {"author"}
    if required - set(card):
        raise CardError("Missing card fields")
    if not isinstance(card["id"], str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_-]{0,63}", card["id"]
    ):
        raise CardError("Invalid stable card ID")
    if card["status"] not in ("active", "inactive") or card["kind"] not in (
        "technique",
        "preference",
    ):
        raise CardError("Invalid card status or kind")
    if type(card["revision"]) is not int or card["revision"] < 1:
        raise CardError("Invalid revision")
    if card["visual_dependency"] not in ("none", "partial", "required"):
        raise CardError("Invalid visual dependency")
    for field in TEXT_FIELDS:
        if field == "author" and field not in card and v2:
            continue
        value = card[field]
        if field in ("method", "conditions", "limits", "agent_notes"):
            if not isinstance(value, list) or any(
                not isinstance(x, str) or not x.strip() for x in value
            ):
                raise CardError(f"Invalid {field}")
            if field != "agent_notes" and not value:
                raise CardError(f"Empty {field}")
        elif not isinstance(value, str) or not value.strip():
            raise CardError(f"Invalid {field}")
    if card["kind"] == "preference" and not isinstance(card.get("support_scope"), str):
        raise CardError("Preference requires evidence scope")
    if v2:
        ids = card["video_ids"]
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(i, str) or not re.fullmatch(r"v_[a-f0-9]{16}", i) for i in ids)
                or len(set(ids)) != len(ids)):
            raise CardError("Unique internal video IDs required")
    elif not isinstance(card["source_refs"], list) or not card["source_refs"]:
        raise CardError("Source references required")
    for ref in card.get("source_refs", []):
        if not isinstance(ref, dict) or set(ref) != REF_FIELDS:
            raise CardError("Invalid source locator; evidence text is excluded")
        if (
            type(ref["line_start"]) is not int
            or type(ref["line_end"]) is not int
            or not 1 <= ref["line_start"] <= ref["line_end"]
        ):
            raise CardError("Invalid source lines")
        if not re.fullmatch(r"[a-f0-9]{64}", ref["source_sha256"]):
            raise CardError("Source hash required")
    if len(retrieval_text(card).encode()) > 12000:
        raise CardError("Card too large; distill a bounded technique instead")


def retrieval_text(card):
    # Source titles, times, original excerpts, paths and QA notes are not embedded.
    return "\n".join(
        f"{k}: " + ("；".join(card[k]) if isinstance(card[k], list) else card[k])
        for k in ("kind",) + TEXT_FIELDS if k in card
    )


def public_card(card):
    """Keep deduplication IDs out of every retrieval response and embedding."""
    if card.get("schema_version") != 2:
        return card
    result = {k: v for k, v in card.items() if k not in ("video_ids", "schema_version")}
    result["mention_count"] = len(card["video_ids"])
    return result


def embedding_snapshot(store_id, cards):
    return fingerprint({'store_id': store_id, 'texts': {
        i: retrieval_text(c) for i, c in cards.items() if c['status'] == 'active'}})


def terms(text):
    result = []
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text.lower()):
        if "\u4e00" <= part[0] <= "\u9fff":
            result.extend(part[i : i + 2] for i in range(max(1, len(part) - 1)))
        else:
            result.append(part)
    return result


def keyword_scores(query, texts):
    """Shared lexical ranking for cards and current image metadata."""
    documents = {i: Counter(terms(text)) for i, text in texts.items()}
    df = Counter(t for doc in documents.values() for t in doc)
    n = len(documents)
    avg = sum(sum(d.values()) for d in documents.values()) / max(n, 1)
    result = {}
    for i, doc in documents.items():
        length = sum(doc.values())
        score = 0.0
        for t in set(terms(query)):
            tf = doc[t]
            if tf:
                score += (math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                          * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / max(avg, 1))))
        if score > 0:
            result[i] = score
    return result


class CardStore:
    def __init__(self, root, *, initialize=True):
        self.root = Path(root).resolve(strict=True)
        self.cards_dir = self.root / "cards"
        if not self.cards_dir.is_dir() or self.cards_dir.is_symlink():
            raise CardError("Explicit card directory required")
        self.control = self.root / "store.json"
        if not self.control.exists():
            if not initialize:
                raise CardError('卡库尚未初始化；请在PAA维护工作区处理。')
            with self.lock():
                if not self.control.exists():
                    save(
                        self.control,
                        {"schema_version": 1, "store_id": uuid.uuid4().hex},
                    )
        metadata = read(self.control)
        if metadata.get("schema_version") != 1 or not isinstance(
            metadata.get("store_id"), str
        ):
            raise CardError("Unsupported store")
        self.store_id = metadata["store_id"]

    @contextmanager
    def lock(self):
        target = self.root / ".writer.lock"
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise CardError(
                "Store has another writer; retry after it finishes"
            ) from None
        try:
            os.write(descriptor, str(os.getpid()).encode())
            yield
        finally:
            os.close(descriptor)
            target.unlink()

    def snapshot(self):
        if (self.root / '.pending.json').exists():
            raise CardError('Interrupted card transaction; run distill recover first')
        metadata = read(self.control)
        if (
            metadata.get("store_id") != self.store_id
            or metadata.get("schema_version") != 1
        ):
            raise CardError("Card store was replaced; reopen the intended store")
        cards, paths = {}, {}
        for path in sorted(self.cards_dir.rglob("*.json")):
            resolved = path.resolve(strict=True)
            if path.is_symlink() or not resolved.is_relative_to(
                self.cards_dir.resolve()
            ):
                raise CardError("Card escaped the card directory")
            card = read(resolved)
            validate(card)
            if card["id"] in cards:
                raise CardError("Duplicate stable card ID")
            cards[card["id"]], paths[card["id"]] = card, resolved
        token = fingerprint({"store_id": self.store_id, "cards": cards})
        return cards, paths, token

    def catalog(self):
        cards, _, token = self.snapshot()
        rows = []
        for card in cards.values():
            if card['status'] != 'active':
                continue
            scope = card['conditions'][0]
            rows.append({'id': card['id'], 'kind': card['kind'], 'title': card['title'],
                         'problem': card['problem'], 'scope_hint': scope[:160],
                         'more_conditions': len(card['conditions']) > 1 or len(scope) > 160})
        self.ensure_current(token)
        return rows

    def commit_files(self, values):
        """Caller holds the writer lock. A journal prevents partial batch reads."""
        if (self.root / '.pending.json').exists():
            raise CardError('Recover the pending transaction first')
        entries = []
        for name, after in values.items():
            target = (self.root / name).resolve()
            if not target.is_relative_to(self.root) or target == self.root or name == '.pending.json':
                raise CardError('Invalid transaction path')
            before = read(target) if target.exists() else None
            if before != after:
                entries.append({'path': name, 'before': before, 'after': after})
        if not entries:
            return
        save(self.root / '.pending.json', {'store_id': self.store_id, 'entries': entries})
        self._finish_pending()

    def _finish_pending(self):
        path = self.root / '.pending.json'
        pending = read(path)
        if pending['store_id'] != self.store_id:
            raise CardError('Foreign recovery journal')
        # Check every destination before writing anything, including a partially applied batch.
        for entry in pending['entries']:
            target = (self.root / entry['path']).resolve()
            if not target.is_relative_to(self.root) or target == path or target == self.root:
                raise CardError('Recovery path escaped store')
            current = read(target) if target.exists() else None
            if current != entry['before'] and current != entry['after']:
                raise CardError('Recovery would overwrite another edit')
        for entry in pending['entries']:
            save(self.root / entry['path'], entry['after'])
        path.unlink()

    def recover(self):
        with self.lock():
            if not (self.root / '.pending.json').exists():
                return {'recovered': False}
            self._finish_pending()
        return {'recovered': True}

    def ensure_current(self, token):
        if self.snapshot()[2] != token:
            raise CardError("Cards changed during operation; retry")

    def get(self, card_id, include_inactive=False):
        cards, _, token = self.snapshot()
        card = cards.get(card_id)
        if card is None or (card["status"] != "active" and not include_inactive):
            raise CardError("Card absent or inactive")
        self.ensure_current(token)
        return {"card": public_card(card), "etag": fingerprint(card)}

    def update(self, card_id, patch, expected_etag):
        if not patch or set(patch) - ((FIELDS | V2_FIELDS) - {"id", "revision", "schema_version"}):
            raise CardError(
                "Invalid patch; ID and revision are controlled by the store"
            )
        with self.lock():
            cards, paths, token = self.snapshot()
            old = cards.get(card_id)
            if old is None or fingerprint(old) != expected_etag:
                raise CardError(
                    "Card changed since read; review latest content before editing"
                )
            new = {**old, **patch, "revision": old["revision"] + 1}
            validate(new)
            self.ensure_current(token)
            if self.snapshot()[1].get(card_id) != paths[card_id]:
                raise CardError("Card moved during update; retry at its new location")
            save(
                self.root
                / "history"
                / card_id
                / f"{old['revision']}-{expected_etag}.json",
                old,
            )
            save(paths[card_id], new)
        return self.get(card_id, include_inactive=True)

    def keyword(self, query, limit=5):
        if not isinstance(query, str) or not query.strip() or not 1 <= limit <= 20:
            raise CardError("Nonempty query and limit 1..20 required")
        cards, _, token = self.snapshot()
        active = {i: c for i, c in cards.items() if c["status"] == "active"}
        scores = keyword_scores(query, {i: retrieval_text(c) for i, c in active.items()})
        scored = [{"id": i, "score": score, "card": public_card(active[i]), "etag": fingerprint(active[i])}
                  for i, score in scores.items()]
        self.ensure_current(token)
        return sorted(scored, key=lambda r: (-r["score"], r["id"]))[:limit]

    def vector_build(self, name, model, encode, *, rebuild=False):

        target = self.vector_path(name)
        cards, _, token = self.snapshot()
        ids = sorted(i for i, c in cards.items() if c["status"] == "active")
        if not ids:
            raise CardError("No active cards")
        hashes = {i: fingerprint(retrieval_text(cards[i])) for i in ids}
        cached = {}
        path = self.vector_path(name)
        if path.is_file() and not rebuild:
            previous = read(path)
            digest = previous.pop('checksum', None)
            if digest != fingerprint(previous) or previous.get('store_id') != self.store_id:
                raise CardError('旧卡片索引损坏或属于其他卡库。')
            if previous.get('model') == model:
                old_hashes = previous.get('text_hashes', {})
                if not old_hashes and previous.get('snapshot') == embedding_snapshot(self.store_id, cards):
                    old_hashes = hashes
                old_vectors = normalize(previous['vectors'], len(previous['ids']))
                cached = {i: v for i, v in zip(previous['ids'], old_vectors, strict=True)
                          if i in hashes and old_hashes.get(i) == hashes[i]}
        missing = [i for i in ids if i not in cached]
        if missing:
            new = normalize(encode([retrieval_text(cards[i]) for i in missing], 'document'), len(missing))
            cached.update(zip(missing, new, strict=True))
        values = normalize([cached[i] for i in ids], len(ids))
        with self.lock():
            self.ensure_current(token)
            payload = {
                "schema_version": 2,
                "store_id": self.store_id,
                "model": model,
                "snapshot": embedding_snapshot(self.store_id, cards),
                "text_hashes": hashes,
                "ids": ids,
                "vectors": values.tolist(),
            }
            save(target, {**payload, "checksum": fingerprint(payload)})
        return {"cards": len(ids), "dimension": values.shape[1], "model": model, "encoded": len(missing), "reused": len(ids) - len(missing)}

    def vector_path(self, name):
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", name):
            raise CardError("Invalid vector index name")
        return self.root / ".index" / (name + ".json")

    def vector(self, query_vector, name, model, limit=5):
        import numpy as np

        if not 1 <= limit <= 20:
            raise CardError("Limit must be 1..20")
        cards, index, matrix, token = self.prepare_vector(name, model)
        ids = index['ids']
        q = normalize([query_vector], 1)
        if q.shape[1] != matrix.shape[1]:
            raise CardError("Query model dimension mismatch")
        scores = matrix @ q[0]
        order = np.argsort(-scores, kind="stable")[:limit]
        result = [
            {
                "id": ids[j],
                "score": float(scores[j]),
                "card": public_card(cards[ids[j]]),
                "etag": fingerprint(cards[ids[j]]),
            }
            for j in order
        ]
        self.ensure_current(token)
        return result

    def prepare_vector(self, name, model, dimension=None):
        cards, _, token = self.snapshot()
        # Authoritative-store errors stay outside the index-only error boundary.
        target = self.vector_path(name)
        try:
            index = read(target)
            checksum = index.pop('checksum', None)
            if fingerprint(index) != checksum:
                raise CardIndexUnavailable('corrupt', '卡片向量索引校验失败。')
            ids = sorted(i for i, c in cards.items() if c['status'] == 'active')
            if (index.get('schema_version') != 2 or index.get('store_id') != self.store_id
                    or index.get('snapshot') != embedding_snapshot(self.store_id, cards)
                    or index.get('model') != model or index.get('ids') != ids):
                raise CardIndexUnavailable('stale', '卡片向量索引已过期或不属于当前卡库／模型，需显式重建。')
            matrix = normalize(index['vectors'], len(ids))
            if dimension is not None and matrix.shape[1] != dimension:
                raise CardIndexUnavailable('corrupt', '卡片向量索引维度错误。')
        except FileNotFoundError:
            raise CardIndexUnavailable('missing', '卡片向量索引缺失，需显式重建。') from None
        except CardIndexUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise CardIndexUnavailable('corrupt', '卡片向量索引无法读取或格式损坏。') from None
        self.ensure_current(token)
        return cards, index, matrix, token

    def vector_status(self, name, model, dimension=None):
        cards, _, token = self.snapshot()
        status = {'state': 'ready', 'active_cards': sum(c['status'] == 'active' for c in cards.values()),
                  'indexed_cards': 0, 'model': model, 'dimension': dimension, 'reason': None}
        try:
            _, index, matrix, _ = self.prepare_vector(name, model, dimension)
            status.update(indexed_cards=len(index['ids']), dimension=matrix.shape[1])
        except CardIndexUnavailable as error:
            status.update(state=error.state, reason=str(error))
        self.ensure_current(token)
        return status


def normalize(values, count):
    import numpy as np

    v = np.asarray(values, dtype=np.float32)
    if (
        v.ndim != 2
        or v.shape[0] != count
        or v.shape[1] == 0
        or not np.isfinite(v).all()
    ):
        raise CardError("Invalid vectors")
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    if (norms <= 0).any():
        raise CardError("Zero vector")
    return v / norms


def fuse(keyword, vector, limit=5):
    scores, entries = Counter(), {}
    for results in (keyword, vector):
        for rank, row in enumerate(results, 1):
            scores[row["id"]] += 1 / (60 + rank)
            if row["id"] in entries and row["etag"] != entries[row["id"]]["etag"]:
                raise CardError("Cards changed between retrieval routes")
            entries[row["id"]] = row
    return [
        {**entries[i], "score": scores[i]}
        for i in sorted(scores, key=lambda i: (-scores[i], i))[:limit]
    ]


def main():
    parser = argparse.ArgumentParser(description="Agent维护的知识卡；原文不参与召回")
    parser.add_argument("--store", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    searching = sub.add_parser("search")
    searching.add_argument("query")
    searching.add_argument("--limit", type=int, default=5)
    getting = sub.add_parser("get")
    getting.add_argument("id")
    getting.add_argument("--include-inactive", action="store_true")
    updating = sub.add_parser("update")
    updating.add_argument("id")
    updating.add_argument("--patch", type=Path, required=True)
    updating.add_argument("--etag", required=True)
    disabling = sub.add_parser("deactivate")
    disabling.add_argument("id")
    disabling.add_argument("--etag", required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    store = CardStore(args.store)
    if args.command == "search":
        result = {
            "candidates": store.keyword(args.query, args.limit),
            "applicability": "Agent must evaluate conditions; scores do not prove support",
        }
    elif args.command == "get":
        result = store.get(args.id, args.include_inactive)
    elif args.command == "update":
        result = store.update(args.id, read(args.patch), args.etag)
    elif args.command == "deactivate":
        result = store.update(args.id, {"status": "inactive"}, args.etag)
    else:
        cards, _, token = store.snapshot()
        result = {
            "store_id": store.store_id,
            "snapshot": token,
            "cards": len(cards),
            "active": sum(c["status"] == "active" for c in cards.values()),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
