"""V2 mechanical staging and merging. The Agent supplies all semantic decisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .cards import CardError, CardStore, TEXT_FIELDS, fingerprint, read, save, validate

STATE = "qa/distillation-v2.json"
DOMAINS = ("摄影", "绘画", "跨媒介")
PATCH_FIELDS = set(TEXT_FIELDS) | {"kind", "visual_dependency", "support_scope"}


def transcript_text(data, suffix):
    text = data.decode("utf-8-sig")
    if suffix != ".srt":
        return text
    lines = text.splitlines()
    timing = re.compile(
        r"\s*\d{1,2}:\d{1,2}:\d{1,2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{1,2}:\d{1,2}[,.]\d{1,3}.*"
    )
    return "\n".join(
        line.strip()
        for i, line in enumerate(lines)
        if line.strip()
        and not timing.fullmatch(line)
        and not (
            line.strip().isdigit()
            and i + 1 < len(lines)
            and timing.fullmatch(lines[i + 1])
        )
    )


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_-]{0,79}", value
    ):
        raise CardError("Invalid identifier")
    return value


class Distillation:
    def __init__(self, store, base=None):
        self.store = CardStore(store)
        self.base = Path(base).resolve(strict=True) if base is not None else None

    def inventory(self):
        from .source_registry import INVENTORY
        current = self.store.root / INVENTORY
        if current.exists():
            return read(current)
        if self.base is None:
            raise CardError('资料清单尚未迁移；在维护工作区显式使用 distill --base <旧目录> migrate。')
        return read(self.base / 'inventory.json')

    def scope(self):
        from .source_registry import SCOPE
        current = self.store.root / SCOPE
        if current.exists():
            return read(current)
        if self.base is None:
            raise CardError('资料范围尚未登记。')
        return read(self.base / 'scope-current.json')

    def state(self):
        if (self.store.root / ".pending.json").exists():
            raise CardError("Recover interrupted transaction first")
        state = read(self.store.root / STATE)
        if (
            state.get("schema_version") != 2
            or state.get("store_id") != self.store.store_id
        ):
            raise CardError("Foreign or unsupported distillation state")
        return state

    def initialize(self):
        """Import old progress/drafts once, without reading sources or merging cards."""
        with self.store.lock():
            if (self.store.root / STATE).exists():
                return self.status()
            inventory = self.inventory()
            progress = {i: s["state"] for i, s in inventory["sources"].items()}
            drafts = {}
            aliases = read(self.store.root / "video-aliases.json")["source_to_video"]
            reviews = [read(p) for p in sorted((self.base / "reviews").glob("*.json"))]
            for review in reviews:
                for source in review["sources"]:
                    progress[source["source_id"]] = source["decision"]
                if review.get("evidence_status") == "pending":
                    for item in review["evidence_additions"]:
                        did = review["batch_id"] + "-" + item["card_id"]
                        sids = {
                            sid
                            for unit in item["video_units"]
                            for sid in unit["source_ids"]
                        }
                        drafts[did] = {
                            "id": did,
                            "kind": "technique",
                            "domain": "摄影",
                            "title": item["card_id"] + "已审阅待归并",
                            "content": item["detail"],
                            "target_hint": item["card_id"],
                            "video_ids": sorted({aliases[s] for s in sids}),
                            "state": "candidate",
                        }
            for path in self.base.glob("creator-*.json"):
                for sid in read(path).get("pending_synthesis_resolved", {}):
                    if progress.get(sid) == "read_pending_synthesis":
                        progress[sid] = "covered_by_synthesis"
            for seed in read(self.store.root / "qa/technique-seeds.json")["seeds"]:
                content = "\n".join(
                    str(seed[k])
                    for k in ("hint", "potential_use", "reason_not_merged")
                    if seed.get(k)
                )
                draft = {
                    "id": seed["id"],
                    "kind": "technique",
                    "domain": seed.get("domain", "摄影"),
                    "title": seed["title"],
                    "content": content,
                    "gaps": seed.get("gaps", []),
                    "video_ids": sorted(
                        {aliases[r["source_id"]] for r in seed["source_refs"]}
                    ),
                    "state": seed["state"],
                }
                if seed.get("promoted_to"):
                    draft["merged_into"] = seed["promoted_to"]
                drafts[seed["id"]] = draft
            state = {
                "schema_version": 2,
                "store_id": self.store.store_id,
                "progress": progress,
                "shown": {},
                "drafts": drafts,
                "batches": {},
                "legacy_last_batch": reviews[-1]["batch_id"] if reviews else None,
            }
            self.store.commit_files({STATE: state})
        return self.status()

    def allowed(self, source):
        excluded = set(self.scope()["excluded_collections"])
        return [
            o
            for o in source["occurrences"]
            if o["collection"] not in excluded
            and (
                o["collection"].endswith("的视频列表")
                or o["collection"] == "0_摄影-实战"
                or source.get("explicitly_registered") is True
            )
        ]

    def source_bytes(self, source):
        inventory = self.inventory()
        allowed = self.allowed(source)
        if not allowed:
            raise CardError("Source outside authorized collections")
        archive = self.store.root.parent / 'sources' / identifier(source['source_id']) / ('source' + source['suffix'])
        if archive.is_file():
            data = archive.read_bytes()
            if hashlib.sha256(data).hexdigest() != source['sha256']:
                raise CardError('Archived source changed')
            return data
        roots = [Path(p).resolve() for p in inventory.get("roots", [inventory["root"]])]
        path = Path(allowed[0]["path"]).resolve(strict=True)
        if not any(path.is_relative_to(root) for root in roots):
            raise CardError("Source escaped authorized root")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise CardError("Source changed since inventory")
        return data

    def sources(self, owner="", limit=20):
        if not 1 <= limit <= 200:
            raise CardError("Source list limit must be 1..200")
        state = self.state()
        inventory = self.inventory()
        rows = []
        for sid, source in inventory["sources"].items():
            occ = self.allowed(source)
            if state["progress"][sid] != "unread" or not occ:
                continue
            if owner and not any(owner in o["collection"] for o in occ):
                continue
            rows.append(
                {
                    "id": sid,
                    "title": Path(occ[0]["path"]).stem,
                    "characters": source["text_chars"],
                    "owners": list(
                        dict.fromkeys(o["owner"] for o in occ if o.get("owner"))
                    ),
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def show(self, sid, start=0, size=12000):
        """Return all transcript text, removing only SRT indices and timing lines."""
        if start < 0 or not 1 <= size <= 50000:
            raise CardError("Invalid text range")
        source = self.inventory()["sources"][sid]
        text = transcript_text(self.source_bytes(source), source["suffix"])
        if start >= len(text) and not (start == 0 and not text and source.get('source_kind') == 'visual_only'):
            raise CardError("Start beyond source")
        end = min(len(text), start + size)
        with self.store.lock():
            state = self.state()
            shown = state["shown"].setdefault(
                sid,
                {
                    "sha256": source["sha256"],
                    "ranges": [],
                    "length": len(text),
                    "view": "transcript-v1",
                },
            )
            if (
                shown["sha256"] != source["sha256"]
                or shown.get("view") != "transcript-v1"
            ):
                raise CardError("Source reading identity changed")
            ranges = sorted(shown["ranges"] + [[start, end]])
            joined = []
            for a, b in ranges:
                if joined and a <= joined[-1][1]:
                    joined[-1][1] = max(joined[-1][1], b)
                else:
                    joined.append([a, b])
            shown["ranges"] = joined
            save(self.store.root / STATE, state)
        return {
            "source_id": sid,
            "start": start,
            "end": end,
            "total": len(text),
            "next_start": end if end < len(text) else None,
            "text": text[start:end],
        }

    def stage(self, batch):
        bid = identifier(batch["batch_id"])
        if batch.get("full_text_read") is not True:
            raise CardError(
                "Agent must attest full text actually read, including truncated output"
            )
        inventory = self.inventory()
        with self.store.lock():
            state = self.state()
            if bid in state["batches"]:
                if state["batches"][bid] == fingerprint(batch):
                    return {"batch_id": bid, "already_staged": True}
                raise CardError("Batch ID already used with different content")
            sources = batch["sources"]
            if not sources or len({x["id"] for x in sources}) != len(sources):
                raise CardError("Distinct reviewed sources required")
            aliases = read(self.store.root / "video-aliases.json")
            mapping = aliases["source_to_video"]
            allowed_sources = {}
            archives = []
            for entry in sources:
                sid = entry["id"]
                source = inventory["sources"][sid]
                if not self.allowed(source):
                    raise CardError("Excluded source")
                if state["progress"][sid] == "unread":
                    shown = state["shown"].get(sid, {})
                    if (
                        shown.get("ranges") != [[0, shown.get("length")]]
                        or shown.get("sha256") != source["sha256"]
                    ):
                        raise CardError(
                            "Full source has not been returned; finish missing segments"
                        )
                if entry["decision"] not in (
                    "read_pending_synthesis",
                    "distilled",
                    "no_useful_knowledge",
                    "duplicate_reviewed",
                ):
                    raise CardError("Invalid source disposition")
                data = self.source_bytes(source)
                archive = (
                    self.store.root.parent
                    / "sources"
                    / identifier(sid)
                    / ("source" + source["suffix"])
                )
                if (
                    archive.exists()
                    and hashlib.sha256(archive.read_bytes()).hexdigest()
                    != source["sha256"]
                ):
                    raise CardError("Archive already differs")
                archives.append((archive, data))
                allowed_sources[sid] = source
                same = entry.get("same_video_as")
                vid = (
                    mapping[same]
                    if same
                    else mapping.get(sid, "v_" + source["sha256"][:16])
                )
                if sid in mapping and mapping[sid] != vid:
                    raise CardError(
                        "Existing video identity differs; explicit correction needed"
                    )
                mapping[sid] = vid
                state["progress"][sid] = entry["decision"]
                state["shown"].pop(sid, None)
            for group in batch.get("video_groups", []):
                if len(group) < 2 or not set(group) <= allowed_sources.keys():
                    raise CardError("Episode groups must use this batch sources")
                old_ids = {mapping[s] for s in group}
                # Remapping previously committed cards requires a separate explicit correction.
                committed = {
                    v
                    for c in self.store.snapshot()[0].values()
                    for v in c.get("video_ids", [])
                }
                if len(old_ids) > 1 and (
                    old_ids & committed
                    or any(
                        old_ids & set(d["video_ids"]) for d in state["drafts"].values()
                    )
                ):
                    raise CardError(
                        "Episode group changes existing counts; review correction explicitly"
                    )
                canonical = min(old_ids)
                for sid, vid in list(mapping.items()):
                    if vid in old_ids:
                        mapping[sid] = canonical
            for item in batch.get("drafts", []):
                did = identifier(item["id"])
                if did in state["drafts"]:
                    raise CardError("Draft ID already exists")
                sids = item["source_ids"]
                if not sids or not set(sids) <= allowed_sources.keys():
                    raise CardError("Draft requires fully reviewed batch sources")
                if (
                    item["kind"] not in ("technique", "preference")
                    or item["domain"] not in DOMAINS
                ):
                    raise CardError("Invalid draft kind/domain")
                if (
                    not item.get("title", "").strip()
                    or not item.get("content", "").strip()
                ):
                    raise CardError("Draft needs a title and contextual content")
                if set(item) - {
                    "id",
                    "title",
                    "kind",
                    "domain",
                    "content",
                    "gaps",
                    "author",
                    "source_ids",
                }:
                    raise CardError("Draft has unnecessary or unsupported fields")
                if item["kind"] == "preference" and not all(
                    any(
                        o.get("owner") == item.get("author")
                        and o["collection"].endswith("的视频列表")
                        for o in self.allowed(allowed_sources[sid])
                    )
                    for sid in sids
                ):
                    raise CardError("Preference needs a matching UP collection owner")
                state["drafts"][did] = {
                    k: v for k, v in item.items() if k != "source_ids"
                } | {
                    "video_ids": sorted({mapping[s] for s in sids}),
                    "state": "candidate",
                }
            state["batches"][bid] = fingerprint(batch)
            # Archives are idempotent and outside recall. No reading state advances until commit.
            for path, data in archives:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        with temp.open("xb") as out:
                            out.write(data)
                            out.flush()
                            os.fsync(out.fileno())
                        os.replace(temp, path)
                    finally:
                        temp.unlink(missing_ok=True)
            self.store.commit_files({STATE: state, "video-aliases.json": aliases})
        return {
            "batch_id": bid,
            "staged": len(batch.get("drafts", [])),
            "cards_written": 0,
        }

    def queue(self):
        return [
            {k: v for k, v in d.items() if k != "video_ids"}
            | {"mention_count": len(d["video_ids"]), "etag": fingerprint(d)}
            for d in self.state()["drafts"].values()
            if d["state"] in ("candidate", "reserve")
        ]

    def status(self):
        state = self.state()
        inventory = self.inventory()
        unread = sum(
            state["progress"][i] == "unread" and bool(self.allowed(s))
            for i, s in inventory["sources"].items()
        )
        return {
            "schema_version": 2,
            "unread_sources": unread,
            "candidate_drafts": sum(
                d["state"] == "candidate" for d in state["drafts"].values()
            ),
            "reserve_drafts": sum(
                d["state"] == "reserve" for d in state["drafts"].values()
            ),
            "legacy_last_batch": state["legacy_last_batch"],
            "staged_batches": len(state["batches"]),
            "last_staged_batch": next(reversed(state["batches"]), None),
            "queue_etag": fingerprint(state),
        }

    def merge(self, plan, apply=False):
        """Preview by default; only explicit apply writes the Agent-authored decision."""
        with self.store.lock():
            state = self.state()
            if fingerprint(state) != plan["queue_etag"]:
                raise CardError("Draft queue changed; review current queue")
            cards, paths, token = self.store.snapshot()
            changed, used, touched = {}, set(), set()
            draft_targets = {}
            for op in plan["operations"]:
                cid = identifier(op["target_id"])
                old = cards.get(cid)
                if (fingerprint(old) if old else None) != op.get("expected_etag"):
                    raise CardError("Target card changed")
                if old and old["status"] != "active":
                    raise CardError("Cannot merge into an inactive card")
                if cid in touched or set(op.get("patch", {})) - PATCH_FIELDS:
                    raise CardError("Repeated card or unsupported patch")
                touched.add(cid)
                new = {**(old or {}), **op.get("patch", {})}
                videos = set(old.get("video_ids", [])) if old else set()
                inputs = []
                for did in op.get("draft_ids", []):
                    draft = state["drafts"][did]
                    if draft["state"] not in ("candidate", "reserve"):
                        raise CardError("Draft already consumed")
                    used.add(did)
                    inputs.append(draft)
                    videos.update(draft["video_ids"])
                    draft_targets.setdefault(did, set()).add(cid)
                for absorbed in op.get("absorb", []):
                    aid = absorbed["id"]
                    other = cards[aid]
                    if (
                        aid in touched
                        or other["status"] != "active"
                        or fingerprint(other) != absorbed["etag"]
                    ):
                        raise CardError(
                            "Absorbed card changed or overlaps another operation"
                        )
                    touched.add(aid)
                    inputs.append(other)
                    videos.update(other["video_ids"])
                    changed[aid] = {
                        **other,
                        "status": "inactive",
                        "revision": other["revision"] + 1,
                    }
                if not inputs:
                    raise CardError("Merge requires a draft or another card")
                for source in inputs + ([old] if old else []):
                    if source["kind"] != new.get("kind") or (
                        source["kind"] == "preference"
                        and source.get("author") != new.get("author")
                    ):
                        raise CardError(
                            "Keep technique/person and different people separate"
                        )
                new.update(
                    id=cid,
                    schema_version=2,
                    status="active",
                    video_ids=sorted(videos),
                    revision=old["revision"] if old else 1,
                )
                if not new.get("agent_notes") or new["agent_notes"][0] not in [
                    "领域：" + d for d in DOMAINS
                ]:
                    raise CardError("Leading domain label required")
                validate(new)
                if new != old:
                    if old:
                        new["revision"] += 1
                    changed[cid] = new
            for did, targets in draft_targets.items():
                state["drafts"][did].update(
                    state="promoted",
                    merged_into=next(iter(targets)) if len(targets) == 1 else sorted(targets),
                )
            for did in plan.get("reserve", []):
                draft = state["drafts"][did]
                if did in used or draft["state"] != "candidate":
                    raise CardError("Cannot reserve a consumed or resolved draft")
                draft["state"] = "reserve"
            for sid in plan.get("synthesized_sources", []):
                if state["progress"].get(sid) != "read_pending_synthesis":
                    raise CardError("Only pending creator synthesis can be completed")
                state["progress"][sid] = "covered_by_synthesis"
            values = {STATE: state}
            for cid, card in changed.items():
                previous = cards.get(cid)
                if previous:
                    values[
                        f"history/{cid}/{previous['revision']}-{fingerprint(previous)}.json"
                    ] = previous
                path = (
                    paths[cid].relative_to(self.store.root).as_posix()
                    if previous
                    else f"cards/{cid}.json"
                )
                values[path] = card
            self.store.ensure_current(token)
            if apply:
                self.store.commit_files(values)
            return {
                "applied": apply,
                "changed_cards": list(changed),
                "consumed_drafts": sorted(used),
                "reserved": plan.get("reserve", []),
                "mention_counts": {i: len(c["video_ids"]) for i, c in changed.items()},
            }


def main(argv=None, default_store=None):
    parser = argparse.ArgumentParser(
        description="V2全文阅读、雏形暂存与阶段归并；默认不执行归并"
    )
    parser.add_argument("--store", type=Path, default=default_store)
    parser.add_argument(
        "--base", type=Path, default=None
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "queue", "catalog", "recover", "migrate"):
        sub.add_parser(name)
    sources = sub.add_parser("sources")
    sources.add_argument("--owner", default="")
    sources.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show")
    show.add_argument("id")
    show.add_argument("--start", type=int, default=0)
    show.add_argument("--size", type=int, default=12000)
    stage = sub.add_parser("stage")
    stage.add_argument("file", type=Path)
    merge = sub.add_parser("merge")
    merge.add_argument("file", type=Path)
    merge.add_argument("--apply", action="store_true")
    registration = sub.add_parser('register')
    registration.add_argument('paths', nargs='+', type=Path)
    registration.add_argument('--collection')
    registration.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.store is None:
        parser.error("--store required")
    task = Distillation(args.store, args.base)
    if args.command == 'register':
        from .source_registry import register
        result = register(task, args.paths, collection=args.collection, apply=args.apply)
    elif args.command == 'migrate':
        from .source_registry import migrate
        result = migrate(task)
    elif args.command == "init":
        result = task.initialize()
    elif args.command == "status":
        result = task.status()
    elif args.command == "queue":
        result = {"drafts": task.queue(), "queue_etag": task.status()["queue_etag"]}
    elif args.command == "catalog":
        result = {"cards": task.store.catalog()}
    elif args.command == "recover":
        result = task.store.recover()
    elif args.command == "sources":
        result = task.sources(args.owner, args.limit)
    elif args.command == "show":
        result = task.show(args.id, args.start, args.size)
    elif args.command == "stage":
        result = task.stage(read(args.file))
    else:
        result = task.merge(read(args.file), args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
