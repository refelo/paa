"""Finish an explicitly requested transfer while the PAA service is stopped."""
import hashlib
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3

from paa.library import asset_id, catalog, item_source, source_key
from paa.cards import CardStore, fingerprint, read, retrieval_text, save


def merge_card_vectors(incoming, current):
    """Retain embeddings made on either machine; normal refresh checks content."""
    store = CardStore(incoming, initialize=False)
    cards, _, _ = store.snapshot()
    hashes = {i: fingerprint(retrieval_text(c)) for i, c in cards.items() if c['status'] == 'active'}
    cached = {}
    for root in (current, incoming):
        path = root / '.index/voyage.json'
        if not path.exists():
            continue
        index = read(path)
        checksum = index.pop('checksum', None)
        if checksum != fingerprint(index) or index.get('store_id') != store.store_id:
            raise ValueError('Card vector cache is corrupt or belongs to another store.')
        if index.get('model') != 'voyage-multimodal-3.5':
            continue
        for item, vector in zip(index['ids'], index['vectors'], strict=True):
            if item in hashes and index.get('text_hashes', {}).get(item) == hashes[item]:
                cached[item] = vector
    if cached:
        ids = sorted(cached)
        payload = dict(schema_version=2, store_id=store.store_id, model='voyage-multimodal-3.5',
                       snapshot='', ids=ids, text_hashes={i: hashes[i] for i in ids},
                       vectors=[cached[i] for i in ids])
        save(incoming / '.index/voyage.json', {**payload, 'checksum': fingerprint(payload)})


def image_vectors(paths):
    seen = set()
    for path in paths:
        if not path.exists():
            continue
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            for encoded, vector in db.execute('SELECT record,vector FROM images'):
                record = json.loads(encoded)
                key = (record['item_id'], record['sha256'])
                if key not in seen:
                    seen.add(key)
                    yield record, vector


def remove_absent_images(library, expected_ids):
    if not expected_ids:
        raise ValueError('Refusing an empty source snapshot.')
    expected_ids = set(expected_ids)
    removed = []
    for folder in (library / 'images').glob('*.info'):
        if folder.is_dir() and not folder.is_symlink() and folder.stem not in expected_ids:
            shutil.rmtree(folder)
            removed.append(folder.stem)
    return removed


def import_snapshot(root=Path('/srv/paa'), expected_sync=None, expected_fingerprint=None):
    root = Path(root).resolve()
    local = root / 'workspace/.local'
    incoming = local / 'import'
    library = root / 'library.library'
    summary = json.loads((incoming / 'snapshot.json').read_text())
    if expected_sync is not None and (summary.get('sync_id'), summary.get('data_fingerprint')) != (expected_sync, expected_fingerprint):
        raise ValueError('Incoming snapshot belongs to another synchronization.')
    transaction = incoming / 'import-state.json'
    key = summary.get('sync_id') or fingerprint(summary)
    prior_state = read(transaction) if transaction.exists() else {}
    if prior_state.get('sync_id') == key and prior_state.get('completed'):
        return read(local / 'last-sync.json')
    removed = remove_absent_images(library, summary['image_ids']) if 'image_ids' in summary else []
    runtime = local / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    source = incoming / 'image-index.sqlite3'
    temporary = runtime / 'index.import.sqlite3'
    eligible, skipped = catalog(library)
    valid_ids = set(eligible)
    reused = stale = 0
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as old:
        meta = {k: json.loads(v) for k, v in old.execute('SELECT key,value FROM meta')}
        if meta.get('model') != 'voyage-multimodal-3.5' or meta.get('dimension') != 1024:
            raise ValueError('Imported index model is not the configured PAA model.')
        temporary.unlink(missing_ok=True)
        with closing(sqlite3.connect(temporary)) as new, new:
            new.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            new.execute('CREATE TABLE images (asset_id TEXT PRIMARY KEY, record TEXT NOT NULL, vector BLOB NOT NULL)')
            for record, vector in image_vectors([source, runtime / 'index.sqlite3']):
                if record['item_id'] not in valid_ids:
                    stale += 1
                    continue
                original, _ = item_source(library, record['item_id'])
                with original.open('rb') as handle:
                    digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                if digest != record['sha256']:
                    stale += 1
                    continue
                record['asset_id'] = asset_id(library, record['item_id'], digest)
                record['relative_path'] = original.relative_to(library).as_posix()
                new.execute('INSERT INTO images VALUES (?,?,?)',
                            (record['asset_id'], json.dumps(record, ensure_ascii=False), vector))
                reused += 1
            meta.update(source_key=source_key(library), indexed=reused, eligible=len(eligible),
                        skipped=skipped, cloud_reused=reused, cloud_stale=stale)
            new.executemany('INSERT INTO meta VALUES (?,?)',
                            [(key, json.dumps(value, ensure_ascii=False)) for key, value in meta.items()])
    os.replace(temporary, runtime / 'index.sqlite3')
    cards = local / 'data/cards'
    # Retain incoming until completion so a retry cannot replay a consumed rename.
    staged = local / 'data/cards.sync-staging'
    prior = local / 'data/cards.before-sync'
    phase = prior_state.get('phase') if prior_state.get('sync_id') == key else None
    if phase not in ('cards_ready', 'cards_published'):
        if 'card_ids' in summary:
            expected_cards = set(summary['card_ids'])
            card_root = (incoming / 'cards/cards').resolve()
            for path in card_root.rglob('*.json'):
                if path.is_symlink() or not path.resolve().is_relative_to(card_root):
                    raise ValueError('Incoming card escaped snapshot.')
                if path.stem not in expected_cards:
                    path.unlink()
        if CardStore(incoming / 'cards', initialize=False).snapshot()[2] != summary['card_snapshot']:
            raise ValueError('Incoming cards do not match the frozen snapshot.')
        merge_card_vectors(incoming / 'cards', cards)
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(incoming / 'cards', staged)
        if prior.exists():
            shutil.rmtree(prior)
        save(transaction, {'sync_id': key, 'phase': 'cards_ready'})
        phase = 'cards_ready'
    if phase == 'cards_ready':
        if staged.exists():
            if cards.exists() and not prior.exists():
                cards.rename(prior)
            staged.rename(cards)
        elif not cards.exists():
            raise ValueError('Interrupted card publication requires the staged snapshot.')
        save(transaction, {'sync_id': key, 'phase': 'cards_published'})
    settings = {'library': str(library), 'allow_preview_to_agent': True,
                'index_dir': str(runtime), 'card_store': str(cards),
                'embedding_model': 'voyage-multimodal-3.5', 'budget_usd': None,
                'allow_paid_api': True, 'image_batch_size': 8, 'query_online_default': True}
    (local / 'workspace.json').write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n')
    summary.pop('image_ids', None)
    summary.pop('source_files_sha256', None)
    summary['removed_images'] = removed
    summary.update(reused_vectors=reused, stale_vectors=stale, missing_vectors=len(eligible)-reused)
    (local / 'last-sync.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    save(transaction, {'sync_id': key, 'phase': 'done', 'completed': True})
    return summary


def main():
    if os.name != 'posix':
        raise SystemExit('此脚本只在云服务器上通过 SSH 执行。')
    from .locking import file_lock
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync-id')
    parser.add_argument('--fingerprint')
    args = parser.parse_args()
    with file_lock(Path('/srv/paa/workspace/.local/.cloud-import.lock')):
        print(json.dumps(import_snapshot(expected_sync=args.sync_id, expected_fingerprint=args.fingerprint), ensure_ascii=False))


if __name__ == '__main__':
    main()
