"""Incremental image publication, preserving the existing index wire format."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import time
import uuid

import numpy as np

from .cards import fingerprint, normalize, read, save
from .library import LOCAL, MODEL_ID, SourceError, catalog, item_source, load_image, source_key
from .locking import file_lock

RECIPE = 'exif-rgb-contain1568-jpeg90-v1'


def build_index(settings, limit=None, local=None, encoder=None, allow_online=False, rebuild=False,
                item_ids=None, expected_hashes=None):
    if allow_online and settings.get('allow_paid_api') is False:
        raise SourceError('当前任务不允许新增付费Provider调用。')
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError('limit 必须为正数。')
    if rebuild and limit is not None:
        raise ValueError('完整重建不能使用limit；局部更新不会替换完整覆盖。')
    if item_ids is not None and (limit is not None or rebuild):
        raise ValueError('显式范围不能与limit或rebuild混用。')
    from .search import Search
    from .voyage import VoyageEncoder
    root = settings['library']
    local = Path(local or settings.get('index_dir', LOCAL))
    model = settings.get('embedding_model', MODEL_ID)
    if model != MODEL_ID:
        raise SourceError('仅支持当前Voyage模型。')
    batch_size = settings.get('image_batch_size', 8)
    if isinstance(batch_size, bool) or batch_size not in (8, 32):
        raise ValueError('图片批量大小只支持8或32。')
    local.mkdir(parents=True, exist_ok=True)
    with file_lock(local / '.index.lock'):
        ids, skipped = catalog(root)
        old = {}
        compatible_recipe = True
        dimension = 1024
        if (local / 'index.sqlite3').exists() and not rebuild:
            old_summary, records, vectors = Search(settings, local).snapshot()
            compatible_recipe = old_summary.get('recipe', 'exif-rgb-contain1568-jpeg90-v1') == RECIPE
            if not compatible_recipe and limit is not None:
                raise SourceError('预处理版本改变须完整更新，不能混合局部版本。')
            dimension = vectors.shape[1]
            old = {r['item_id']: (r, v) for r, v in zip(records, vectors, strict=True)}
        if not ids and not old:
            raise SourceError('授权目录中没有可索引的静态图片。')
        chosen = ids if limit is None else ids[:limit]
        if item_ids is not None:
            chosen = list(dict.fromkeys(item_ids))
            if set(chosen) - set(ids):
                raise SourceError('冻结范围中的图片已不可用；保留旧索引。')
        selected = set(chosen)
        output = {i: rv for i, rv in old.items() if i in ids and i not in selected}
        encoder = encoder or VoyageEncoder(local / 'voyage', allow_online=allow_online,
                                             budget_usd=settings.get('budget_usd', 0.1), role='document')
        pending, pictures, errors = [], [], []
        started = time.monotonic()
        encoded = reused = 0

        def cache_path(record):
            return local / 'image-vectors' / (fingerprint([model, RECIPE, record['sha256']]) + '.json')

        def flush():
            nonlocal encoded, dimension
            if not pending:
                return
            try:
                values = normalize(encoder.encode(images=pictures), len(pending))
                if output and dimension != values.shape[1]:
                    raise SourceError('批次间向量维度改变；保留旧索引。')
                dimension = values.shape[1]
                for record, vector in zip(pending, values, strict=True):
                    payload = {'model': model, 'recipe': RECIPE, 'sha256': record['sha256'], 'vector': vector.tolist()}
                    save(cache_path(record), {**payload, 'checksum': fingerprint(payload)})
                    output[record['item_id']] = (record, vector)
                    encoded += 1
            finally:
                for image in pictures:
                    image.close()
                pending.clear()
                pictures.clear()

        try:
            for item in chosen:
                try:
                    previous = old.get(item)
                    original, _ = item_source(root, item)
                    digest = hashlib.sha256(original.read_bytes()).hexdigest()
                    if expected_hashes is not None and expected_hashes.get(item) != digest:
                        raise SourceError('冻结后图片内容改变。')
                    if compatible_recipe and previous and previous[0]['sha256'] == digest:
                        record = {**previous[0], 'relative_path': original.relative_to(root).as_posix()}
                        image = None
                    else:
                        record, image = load_image(root, item)
                except (OSError, ValueError) as error:
                    errors.append({'item_id': item, 'reason': str(error)})
                    continue
                vector = None
                if compatible_recipe and previous and previous[0]['sha256'] == record['sha256']:
                    vector = previous[1]
                elif cache_path(record).exists():
                    cached = read(cache_path(record))
                    checksum = cached.pop('checksum', None)
                    if checksum != fingerprint(cached) or (cached['model'], cached['recipe'], cached['sha256']) != (model, RECIPE, record['sha256']):
                        if image is not None:
                            image.close()
                        raise SourceError('逐图缓存损坏；保留当前索引。')
                    vector = normalize([cached['vector']], 1)[0]
                if vector is not None:
                    if output and dimension != len(vector):
                        if image is not None:
                            image.close()
                        raise SourceError('逐图缓存维度不符。')
                    dimension = len(vector)
                    output[item] = (record, vector)
                    if not cache_path(record).exists():
                        payload = {'model': model, 'recipe': RECIPE, 'sha256': record['sha256'], 'vector': vector.tolist()}
                        save(cache_path(record), {**payload, 'checksum': fingerprint(payload)})
                    reused += 1
                    if image is not None:
                        image.close()
                else:
                    pending.append(record)
                    pictures.append(image)
                    if len(pending) == batch_size:
                        flush()
            flush()
        finally:
            for image in pictures:
                image.close()
        if errors:
            raise SourceError('图片读取失败，保留当前索引及成功缓存：' + json.dumps(errors, ensure_ascii=False))
        summary = {'schema': 2, 'source_key': source_key(root), 'model': model, 'dimension': dimension,
                   'indexed': len(output), 'eligible': len(ids), 'requested_limit': limit,
                   'skipped': skipped, 'errors': errors, 'encoded': encoded, 'reused': reused,
                   'recipe': RECIPE, 'updated_at': time.time(), 'partial': limit is not None,
                   'seconds': round(time.monotonic() - started, 2)}
        temporary = local / ('index.build-' + uuid.uuid4().hex + '.sqlite3')
        try:
            with sqlite3.connect(temporary) as db:
                db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                db.execute('CREATE TABLE images (asset_id TEXT PRIMARY KEY, record TEXT NOT NULL, vector BLOB NOT NULL)')
                db.executemany('INSERT INTO meta VALUES (?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in summary.items()])
                db.executemany('INSERT INTO images VALUES (?,?,?)', [(r['asset_id'], json.dumps(r, ensure_ascii=False), np.asarray(v, dtype=np.float32).tobytes()) for r, v in output.values()])
            db.close()
            os.replace(temporary, local / 'index.sqlite3')
        finally:
            temporary.unlink(missing_ok=True)
        return summary
