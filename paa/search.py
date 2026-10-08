from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .library import (
    LOCAL, MODEL_ID, SourceError, catalog, load_image, source_key, verify_record, item_context, frame_previews,
    asset_id as current_asset_id,
)


class IndexUnavailable(SourceError):
    pass


def build_index(settings, limit=None, local=None, encoder=None, allow_online=False, rebuild=False):
    from .incremental import build_index as update
    return update(settings, limit, local, encoder, allow_online, rebuild)


class Search:
    def __init__(self, settings: dict, local: Path | None = None, encoder=None):
        self.settings, self.local, self.encoder = settings, Path(local or settings.get('index_dir', LOCAL)), encoder

    def snapshot(self) -> tuple[dict, list[dict], np.ndarray]:
        database = self.local / "index.sqlite3"
        if not database.is_file():
            raise IndexUnavailable("当前图库尚无向量索引；仍可取图、文字检索和写备注。")
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            summary = {k: json.loads(v) for k, v in connection.execute("SELECT key, value FROM meta")}
            if summary.get("source_key") != source_key(self.settings["library"]):
                raise IndexUnavailable("索引属于其他素材源，不能返回。")
            expected_model = self.settings.get('embedding_model', MODEL_ID)
            if summary.get("schema") != 2 or summary.get("model") != expected_model:
                raise IndexUnavailable("索引版本或图文模型已变化。")
            rows = connection.execute("SELECT record, vector FROM images ORDER BY asset_id").fetchall()
        records = [json.loads(row[0]) for row in rows]
        vectors = np.stack([np.frombuffer(row[1], dtype=np.float32) for row in rows]) if rows else np.empty((0, summary['dimension']), dtype=np.float32)
        from .cards import normalize
        vectors = normalize(vectors, len(records))
        if summary.get('dimension', vectors.shape[1]) != vectors.shape[1]:
            raise SourceError('索引向量维度错误。')
        return summary, records, vectors

    def vector_summary(self) -> dict:
        database = self.local / 'index.sqlite3'
        if not database.is_file():
            raise IndexUnavailable('当前图库尚无向量索引。')
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
                summary = {k: json.loads(v) for k, v in connection.execute('SELECT key, value FROM meta')}
        except (sqlite3.Error, ValueError) as error:
            raise IndexUnavailable('向量索引不可读。') from error
        if summary.get('source_key') != source_key(self.settings['library']):
            raise IndexUnavailable('向量索引属于其他素材源。')
        if summary.get('schema') != 2 or summary.get('model') != self.settings.get('embedding_model', MODEL_ID):
            raise IndexUnavailable('向量索引版本或模型不匹配。')
        return summary

    def status(self) -> dict:
        if self.settings.get('library') is None:
            return {'library': None, 'eligible': 0, 'indexed': 0,
                    'vector_index': {'state': 'not_configured', 'reason': '尚未选择图片库；知识卡可独立使用。'},
                    'preview_sharing_allowed': False}
        ids, skipped = catalog(self.settings['library'])
        try:
            summary = self.vector_summary()
            vector_index = {'state': 'ready', 'reason': None}
        except IndexUnavailable as error:
            summary = {'indexed': 0}
            vector_index = {'state': 'unavailable', 'reason': str(error)}
        return {**summary, 'library': str(self.settings['library']), 'eligible': len(ids),
                'skipped': skipped, 'vector_index': vector_index,
                'preview_sharing_allowed': self.settings.get('allow_preview_to_agent') is True}

    def require_sharing(self):
        if self.settings.get('library') is None:
            raise SourceError('尚未配置图片库；请明确选择获准的 Eagle 图库。')
        if self.settings.get("allow_preview_to_agent") is not True:
            raise SourceError("未授权向在线 Agent 返回图片预览。")

    def result(self, record: dict, rank: int, score: float | None = None, image=None) -> dict:
        context = item_context(self.settings['library'], record['item_id'])
        image = image if image is not None else verify_record(self.settings["library"], record)
        try:
            previews = frame_previews(self.settings['library'], record, image, self.local)
        finally:
            image.close()
        return {
            "number": rank, "asset_id": current_asset_id(self.settings['library'], record['item_id'], record['sha256']),
            "eagle_id": record["item_id"], "content_sha256": record['sha256'],
            "preview_path": previews[0]['preview_path'],
            "frame_count": record.get('frame_count', 1), "frame_previews": previews,
            "source_path": (self.settings["library"] / record["relative_path"]).as_posix(),
            "width": record["width"], "height": record["height"],
            "similarity": round(score, 4) if score is not None else None,
            "context": context,
        }

    def get(self, asset_id: str) -> dict:
        self.require_sharing()
        root = self.settings['library']
        if asset_id.startswith('eagle:'):
            parts = asset_id.split(':')
            if len(parts) != 4 or parts[1] != source_key(root)[:16]:
                raise SourceError('图片编号不属于当前图库。')
            record, image = load_image(root, parts[2])
            if record['asset_id'] != asset_id:
                image.close()
                raise SourceError('图片已替换，旧图片编号不可使用。')
            return self.result(record, 1, image=image)
        # An explicit Eagle ID locates the current item without a vector index.
        if asset_id and all(c.isascii() and (c.isalnum() or c in '_-') for c in asset_id):
            if (root / 'images' / (asset_id + '.info')).is_dir():
                record, image = load_image(root, asset_id)
                return self.result(record, 1, image=image)
        _, records, _ = self.snapshot()
        record = next((r for r in records if r["asset_id"] == asset_id), None)
        if record is None:
            raise SourceError("当前索引中不存在这个图片编号，不能猜测或替换它。")
        return self.result(record, 1)

    def _metadata_search(self, query, limit, exclude_ids, tags):
        from .cards import keyword_scores
        ids, _ = catalog(self.settings['library'])
        contexts = {}
        for item_id in ids:
            try:
                context = item_context(self.settings['library'], item_id)
            except (OSError, ValueError):
                continue
            if not tags or set(tags).issubset(set(context.get('tags') or [])):
                contexts[item_id] = context
        texts = {item_id: '\n'.join([*(c.get('tags') or []), c.get('annotation') or '',
                    *[str(v.get('annotation', '')) for v in (c.get('comments') or []) if isinstance(v, dict)]])
                 for item_id, c in contexts.items()}
        scores = keyword_scores(query, texts) if query.strip() else {item_id: 1 for item_id in contexts}
        order = sorted(scores, key=lambda item_id: (-scores[item_id], item_id))
        excluded = set(exclude_ids or [])
        seen = {value.rsplit(':', 1)[-1] for value in excluded if value.startswith('eagle:')}
        rows, stale = [], 0
        for rank, item_id in enumerate(order, 1):
            if item_id in excluded:
                continue
            try:
                row = self.get(item_id)
            except (OSError, ValueError, Image.DecompressionBombError):
                stale += 1
                continue
            if row['asset_id'] in excluded or row['content_sha256'][:16] in seen:
                continue
            row.update(number=len(rows)+1, matched_by=['metadata'], selected_by='metadata',
                       route_ranks={'metadata': rank}, metadata_score=round(scores[item_id], 4))
            rows.append(row)
            seen.add(row['content_sha256'][:16])
            if len(rows) == limit:
                break
        try:
            indexed = self.vector_summary()['indexed']
        except IndexUnavailable:
            indexed = 0
        return {'query': query, 'reference_id': None, 'route': 'metadata', 'effective_route': 'metadata',
                'results': rows, 'indexed': indexed, 'eligible': len(ids), 'skipped_stale': stale,
                'embedding': None, 'warnings': [], 'notice': '文字仅用于召回；必须实际看图并读同图全部信息。'}

    def search(self, query='', reference_id=None, reference_path=None, limit=20,
               exclude_ids=None, route='hybrid', tags=None, allow_online=False):
        if allow_online is None:
            allow_online = self.settings.get('query_online_default', False)
        self.require_sharing()
        if type(limit) is not int or limit < 1 or len(query) > 300:
            raise ValueError('limit须为正整数，查询不超过300字。')
        if route not in ('vector', 'metadata', 'hybrid'):
            raise ValueError('route应为vector、metadata或hybrid。')
        if tags is not None and (not isinstance(tags, list) or any(not isinstance(t, str) or not t for t in tags)):
            raise ValueError('tags必须是非空字符串列表。')
        if reference_id and reference_path:
            raise ValueError('一次只提供一个参考图片来源。')
        if route == 'metadata' and (reference_id or reference_path):
            raise ValueError('metadata路线只检索文字。')
        if not query.strip() and not (reference_id or reference_path or (route == 'metadata' and tags)):
            raise ValueError('请提供文字、参考图或明确标签。')
        if allow_online and self.settings.get('allow_paid_api') is False:
            raise SourceError('当前任务不允许新增付费Provider调用。')
        if route == 'metadata':
            return self._metadata_search(query, limit, exclude_ids, tags)
        if route == 'vector':
            result = self._indexed_search(query, reference_id, reference_path, limit, exclude_ids, route, tags, allow_online)
            result['effective_route'] = 'vector'
            return result
        from .cards import CardError
        try:
            # Detect absent/wrong-source vectors before constructing a provider.
            self.vector_summary()
            vector = self._indexed_search(query, reference_id, reference_path, limit, exclude_ids, 'vector', tags, allow_online)
            warnings = []
        except (IndexUnavailable, CardError) as error:
            vector = {'results': [], 'indexed': 0, 'embedding': None, 'skipped_stale': 0}
            warnings = ['向量路线不可用，仅返回文字命中：' + str(error)]
        excluded = list(exclude_ids or []) + ([reference_id] if reference_id else [])
        metadata = self._metadata_search(query, limit, excluded, tags) if query.strip() else {'results': []}
        from itertools import zip_longest
        selected, seen = [], {}
        for pair in zip_longest(vector['results'], metadata['results']):
            for source, row in zip(('vector', 'metadata'), pair):
                if row is None:
                    continue
                key = row['content_sha256']
                if key in seen:
                    previous = seen[key]
                    previous['matched_by'] = sorted(set(previous['matched_by'] + row['matched_by']))
                    previous['route_ranks'].update(row['route_ranks'])
                    continue
                copy = {**row, 'selected_by': source, 'number': len(selected)+1}
                seen[key] = copy
                selected.append(copy)
        return {'query': query, 'reference_id': reference_id, 'route': 'hybrid',
                'effective_route': 'metadata' if warnings else ('hybrid' if query.strip() else 'vector'),
                'results': selected[:limit], 'indexed': vector['indexed'],
                'eligible': metadata.get('eligible', vector.get('eligible', 0)),
                'skipped_stale': vector['skipped_stale'] + metadata.get('skipped_stale', 0),
                'embedding': vector.get('embedding'), 'warnings': warnings,
                'notice': '召回后必须实际看图并读取全部人工信息；星级和标签只按当前用户的说明理解。'}

    def _indexed_search(self, query: str = "", reference_id: str | None = None,
               reference_path: str | None = None,
               limit: int = 20, exclude_ids: list[str] | None = None,
               route: str = 'hybrid', tags: list[str] | None = None,
               allow_online: bool = False) -> dict:
        self.require_sharing()
        if type(limit) is not int or limit < 1:
            raise ValueError("limit 须为正整数。")
        if len(query) > 300:
            raise ValueError("请把查询改写成 300 字以内的具体正向视觉描述。")
        if reference_id and reference_path:
            raise ValueError("一次只提供一个参考图来源。")
        if route not in ('vector', 'metadata', 'hybrid'):
            raise ValueError('route应为vector、metadata或hybrid。')
        if tags is not None and (not isinstance(tags, list) or any(not isinstance(t, str) or not t for t in tags)):
            raise ValueError('tags必须是非空字符串列表。')
        if route == 'metadata' and (reference_id or reference_path):
            raise ValueError('metadata路线只检索文字；参考图请使用vector或hybrid。')
        if not query.strip() and not (reference_id or reference_path or (route == 'metadata' and tags)):
            raise ValueError("请提供文字或参考图片。")
        summary, records, vectors = self.snapshot()
        if not records:
            raise IndexUnavailable('当前向量索引没有有效图片；仍可使用文字路线。')
        from .cards import CardError, keyword_scores
        from .voyage import VoyageEncoder
        use_vector = route != 'metadata'
        encoder = self.encoder
        if use_vector and encoder is None:
            encoder = VoyageEncoder(self.local / 'voyage', allow_online=allow_online,
                                    budget_usd=self.settings.get('budget_usd', 0.1))
        vector = None
        warnings = []
        picture = None
        excluded = set(exclude_ids or [])
        excluded.update(r['asset_id'] for r in records if r['item_id'] in excluded or
                        current_asset_id(self.settings['library'], r['item_id'], r['sha256']) in excluded)
        if reference_id:
            position = next((i for i, r in enumerate(records) if reference_id in
                             (r['asset_id'], r['item_id'], current_asset_id(self.settings['library'], r['item_id'], r['sha256']))), None)
            if position is None:
                raise SourceError("参考图编号不属于当前索引。")
            checked = verify_record(self.settings["library"], records[position])
            if query.strip():
                picture = checked
            else:
                checked.close()
            vector = vectors[position]
            excluded.add(reference_id)
            excluded.add(records[position]['asset_id'])
        elif reference_path:
            # Only the explicit image file is read; never enumerate its directory.
            supplied = Path(reference_path)
            if not supplied.is_absolute() or not supplied.is_file():
                raise SourceError('参考图必须是当次明确指定的绝对文件路径。')
            supplied = supplied.resolve(strict=True)
            if supplied.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
                raise SourceError('参考文件必须为支持的图片格式。')
            with Image.open(supplied) as raw:
                picture = ImageOps.exif_transpose(raw).convert("RGB")
        try:
            if use_vector:
                if picture is not None:
                    vector = encoder.encode(images=[picture], text=[query.strip()] if query.strip() else None)[0]
                elif query.strip():
                    vector = encoder.encode(text=[query.strip()])[0]
        except CardError as error:
            if route != 'hybrid':
                raise
            warnings.append('向量路线不可用，仅返回文字命中：' + str(error))
            vector = None
        finally:
            if picture is not None:
                picture.close()
        scores = None
        if vector is not None:
            if vector.shape != (vectors.shape[1],) or not np.isfinite(vector).all():
                raise SourceError('查询向量与图片索引不兼容。')
            scores = vectors @ vector
        contexts, stale_ids, lexical = {}, set(), {}
        if route != 'vector' or tags:
            for j, record in enumerate(records):
                try:
                    contexts[j] = item_context(self.settings['library'], record['item_id'])
                except (OSError, ValueError):
                    stale_ids.add(j)
            def text_of(c):
                return '\n'.join([*(c.get('tags') or []), c.get('annotation') or '',
                                  *[str(v.get('annotation', '')) for v in (c.get('comments') or []) if isinstance(v, dict)]])
            lexical = keyword_scores(query, {j: text_of(c) for j, c in contexts.items()}) if query.strip() else ({j: 1 for j in contexts} if route == 'metadata' else {})
        allowed = [j for j in range(len(records)) if j not in stale_ids and
                   (not tags or set(tags).issubset(set(contexts[j].get('tags') or [])))]
        vector_order = sorted(allowed, key=lambda j: (-float(scores[j]), records[j]['asset_id'])) if scores is not None else []
        lexical_order = sorted((j for j in allowed if j in lexical), key=lambda j: (-lexical[j], records[j]['asset_id']))
        route_ranks = {'vector': {j: rank for rank, j in enumerate(vector_order, 1)},
                       'metadata': {j: rank for rank, j in enumerate(lexical_order, 1)}}
        selected_by = {}
        if route == 'hybrid':
            # Alternate independent lists; a note hit need not rank high visually.
            from itertools import zip_longest
            for pair in zip_longest(vector_order, lexical_order):
                for source, j in zip(('vector', 'metadata'), pair):
                    if j is not None:
                        selected_by.setdefault(j, source)
        else:
            for j in lexical_order if route == 'metadata' else vector_order:
                selected_by[j] = route
        order = list(selected_by)
        results, stale = [], len(stale_ids)
        seen_content = {r["sha256"] for r in records if r["asset_id"] in excluded}
        for position in order:
            record = records[int(position)]
            if record["asset_id"] in excluded or record["sha256"] in seen_content:
                continue
            try:
                result = self.result(record, len(results) + 1, float(scores[position]) if scores is not None else None)
                if tags and not set(tags).issubset(set(result['context']['tags'] or [])):
                    stale += 1
                    continue
            except (OSError, ValueError, Image.DecompressionBombError):
                stale += 1
                continue
            results.append(result)
            result['matched_by'] = (['vector'] if scores is not None else []) + (['metadata'] if position in lexical else [])
            result['selected_by'] = selected_by[position]
            result['route_ranks'] = {name: ranks[position] for name, ranks in route_ranks.items() if position in ranks}
            if position in lexical:
                result['metadata_score'] = round(lexical[position], 4)
            seen_content.add(record["sha256"])
            if len(results) == limit:
                break
        return {"query": query, "reference_id": reference_id, "route": route, "results": results,
                "indexed": summary["indexed"], "eligible": summary["eligible"], "skipped_stale": stale,
                "embedding": getattr(encoder, 'last', None), "warnings": warnings,
                "notice": "候选不证明视觉条件成立；读全部人工信息并实际看图。未查到不代表全库不存在。"}
