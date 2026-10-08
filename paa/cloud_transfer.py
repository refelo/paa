"""Agent-invoked, one-way PAA transfer. No watcher, scheduler, or user interface."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import time

from paa.cards import CardStore, read
from paa.library import catalog, item_source


def ssh_args(config):
    return ['ssh.exe' if sys.platform == 'win32' else 'ssh', '-F',
            'NUL' if sys.platform == 'win32' else '/dev/null',
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=' +
            ('NUL' if sys.platform == 'win32' else '/dev/null'),
            '-o', 'UserKnownHostsFile=' + config['known_hosts'],
            '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30',
            '-i', config['identity_file'], config['host']]


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def snapshot(config, directory, image_scope=None):
    source = Path(config['source_workspace'])
    settings = read(source / '.local/workspace.json')
    library = Path(settings['library']).resolve(strict=True)
    if library != Path(config['library']).resolve(strict=True):
        raise RuntimeError('The project switched libraries; cloud sync remains scoped to the approved library.')
    ids, skipped = catalog(library)
    if image_scope is not None:
        from .automation import check_frozen
        from .workspace import Workspace
        check_frozen(Workspace(settings), image_scope)
        ids = list(image_scope['images'])
    if not ids:
        raise RuntimeError('No valid source images; refusing to synchronize an empty library.')
    store = CardStore(Path(settings['card_store']), initialize=False)
    for attempt in range(3):
        try:
            cards, _, token = store.snapshot()
            index = {p.name: p.read_bytes() for p in (store.root / '.index').glob('*.json')}
            store.ensure_current(token)
            break
        except ValueError:
            if attempt == 2:
                raise
            time.sleep(1)
    files, payloads = {}, {}
    total = 0
    for item in ids:
        original, _ = item_source(library, item)
        name = 'library.library/' + original.relative_to(library).as_posix()
        files[name] = original
        # Freeze small metadata bytes. Later local edits belong to the next sync.
        path = original.parent / 'metadata.json'
        payloads['library.library/' + path.relative_to(library).as_posix()] = path.read_bytes()
        total += original.stat().st_size
    base = 'workspace/.local/import/cards/'
    payloads[base + 'store.json'] = (store.root / 'store.json').read_bytes()
    for item, card in cards.items():
        payloads[base + f'cards/{item}.json'] = json_bytes(card)
    for name, data in index.items():
        payloads[base + '.index/' + name] = data
    user_reference = source / '.local/data/user-reference.md'
    payloads['workspace/.local/data/user-reference.md'] = user_reference.read_bytes()
    exported_index = directory / 'image-index.sqlite3'
    index_source = Path(settings['index_dir']) / 'index.sqlite3'
    with closing(sqlite3.connect(index_source.as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(exported_index)) as dst:
            src.backup(dst)
    files['workspace/.local/import/image-index.sqlite3'] = exported_index
    summary = {'images': len(ids), 'image_ids': ids, 'image_bytes': total, 'skipped': skipped,
               'cards': len(cards), 'card_ids': sorted(cards), 'active_cards': sum(c['status'] == 'active' for c in cards.values()),
               'card_snapshot': token, 'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    from .automation import digest_file
    summary['source_files_sha256'] = {}
    for name, path in files.items():
        item = path.parent.name.removesuffix('.info')
        summary['source_files_sha256'][name] = image_scope['images'][item]['sha256'] if image_scope and name.startswith('library.library/') else digest_file(path)
    with closing(sqlite3.connect(exported_index.as_uri() + '?mode=ro', uri=True)) as index:
        vector_hash = hashlib.sha256()
        for record, vector in index.execute('SELECT record,vector FROM images ORDER BY asset_id'):
            record = json.loads(record)
            vector_hash.update(json_bytes([record['item_id'], record['sha256']]))
            vector_hash.update(vector)
    summary['data_fingerprint'] = hashlib.sha256(json_bytes({
        'images': {i: r['sha256'] for i,r in image_scope['images'].items()} if image_scope else {i: hashlib.sha256(item_source(library,i)[0].read_bytes()).hexdigest() for i in ids},
        'payloads': {n: hashlib.sha256(b).hexdigest() for n,b in payloads.items()},
        'vectors': vector_hash.hexdigest()})).hexdigest()
    payloads['workspace/.local/import/snapshot.json'] = json_bytes(summary)
    return files, payloads, summary


def _send_archive(config, entries):
    """A bounded compressed stream. A failed stream never receives a checkpoint."""
    import tempfile
    command = ('set -e; sudo systemctl stop paa.service; '
               'mkdir -p /srv/paa/workspace/.local/import; tar -xzf - -C /srv/paa')
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([*ssh_args(config), command], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=errors)
        class Counter:
            count = 0
            def write(self, data):
                written = process.stdin.write(data)
                self.count += written
                return written
        counter = Counter()
        try:
            with tarfile.open(fileobj=counter, mode='w|gz') as archive:
                for name, value in entries:
                    data = value.read_bytes() if isinstance(value, Path) else value
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), 0o644
                    archive.addfile(info, io.BytesIO(data))
            process.stdin.close()
            code = process.wait()
            if code:
                raise RuntimeError('SSH stream failed')
        except BaseException as error:
            if not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            process.wait()
            errors.seek(0)
            detail = errors.read().decode('utf-8', 'replace')[-1500:]
            raise RuntimeError('传输未完成，已确认的分段保留；服务保持停止。' + (detail or str(error))) from error
        return counter.count


def _assemble_parts(config, plans):
    """Publish a large file only after the assembled content matches its SHA."""
    import shlex
    script = r"""import hashlib,json,os,sys
from pathlib import Path
root=Path('/srv/paa').resolve()
for plan in json.load(sys.stdin):
 target=(root/plan['name']).resolve()
 if not target.is_relative_to(root): raise ValueError('Target escaped PAA')
 target.parent.mkdir(parents=True,exist_ok=True)
 temporary=target.with_name(target.name+'.assembling')
 digest=hashlib.sha256()
 with temporary.open('wb') as output:
  for name in plan['parts']:
   part=(root/name).resolve()
   if not part.is_relative_to(root/'.sync-parts'): raise ValueError('Part escaped staging')
   with part.open('rb') as source:
    while data:=source.read(1024*1024):
     digest.update(data);output.write(data)
 if digest.hexdigest()!=plan['sha256']: raise ValueError('Assembled content mismatch')
 os.replace(temporary,target)
"""
    subprocess.run([*ssh_args(config), 'set -e; sudo systemctl stop paa.service; python3 -c ' + shlex.quote(script)],
                   input=json_bytes(plans), check=True, capture_output=True)


def upload(config, directory, files, payloads, summary, state_path=None, legacy_hashes=None,
           progress=None, chunk_bytes=8*1024*1024):
    from .automation import digest_file
    from .cards import save
    state_path = Path(state_path) if state_path else directory / 'uploaded.json'
    pending_path = state_path.with_name(state_path.name + '.pending')
    previous = read(state_path) if state_path.exists() else {}
    remote = subprocess.run([*ssh_args(config), 'cat /srv/paa/.sync-receipt 2>/dev/null || true'],
                            capture_output=True, text=True, check=True).stdout.strip()
    if pending_path.exists():
        pending = read(pending_path)
        if remote == pending.get('receipt'):
            # The remote commit succeeded but its SSH acknowledgement was lost.
            previous = pending
            save(state_path, previous)
    if remote != previous.get('receipt'):
        previous = {}
    prior_hashes = previous.get('content_hashes', {})
    if not prior_hashes and legacy_hashes:
        prior_hashes = legacy_hashes() if callable(legacy_hashes) else legacy_hashes
    hashes = {name: digest_file(path) for name, path in files.items()}
    if summary.get('source_files_sha256') and hashes != summary['source_files_sha256']:
        raise ValueError('上传前源内容已不属于冻结快照。')
    stamps = {name: [path.stat().st_size, path.stat().st_mtime_ns] for name, path in files.items()}
    wanted = [(n,p) for n,p in files.items() if hashes[n] != prior_hashes.get(n)]
    payload_hashes = {n: hashlib.sha256(data).hexdigest() for n,data in payloads.items()}
    wanted_payloads = [(n,b) for n,b in payloads.items() if payload_hashes[n] != prior_hashes.get(n)]
    report = {'files_to_upload': len(wanted), 'payloads_to_upload': len(wanted_payloads),
              'upload_bytes': sum(p.stat().st_size for _,p in wanted) + sum(len(b) for _,b in wanted_payloads),
              'compressed_tar_bytes': 0, 'completed_chunks': 0}
    if progress:
        progress(report)
    done = dict(prior_hashes)
    checkpoint_receipt = previous.get('receipt')
    def checkpoint():
        nonlocal checkpoint_receipt
        checkpoint_receipt = hashlib.sha256(json_bytes(done)).hexdigest()
        value = {'receipt': checkpoint_receipt, 'files': stamps, 'content_hashes': dict(done),
                 'summary': {k:v for k,v in summary.items() if k not in ('image_ids','source_files_sha256')}}
        save(pending_path, value)
        subprocess.run([*ssh_args(config), 'cat > /srv/paa/.sync-receipt.next && mv /srv/paa/.sync-receipt.next /srv/paa/.sync-receipt'], input=checkpoint_receipt.encode(), check=True)
        save(state_path, value)
        pending_path.unlink(missing_ok=True)
    def invalidate(names):
        changed = False
        for name in names:
            if name in done:
                del done[name]
                changed = True
        if changed:
            # An interrupted tar may already have truncated a destination. The
            # previous content must cease to be trusted before any overwrite.
            checkpoint()
    batch, size = [], 0
    def flush():
        nonlocal batch, size
        if not batch:
            return
        invalidate(name for name, _, _ in batch)
        report['compressed_tar_bytes'] += _send_archive(config, [(n,b) for n,b,_ in batch])
        for name, _, checksum in batch:
            done[name] = checksum
        checkpoint()
        report['completed_chunks'] += 1
        if progress:
            progress(report)
        batch, size = [], 0
    def enqueue(name, value, checksum):
        nonlocal size
        length = value.stat().st_size if isinstance(value, Path) else len(value)
        if batch and size + length > chunk_bytes:
            flush()
        batch.append((name,value,checksum))
        size += length
        if size >= chunk_bytes:
            flush()
    def send_large(name, source, expected_hash):
        key = hashlib.sha256(name.encode()).hexdigest()[:16]
        namespace = summary.get('sync_id') or hashlib.sha256(json_bytes(summary)).hexdigest()[:16]
        import re
        if not re.fullmatch('[A-Za-z0-9_-]+', namespace):
            raise ValueError('无效传输范围标识。')
        parts = []
        index = 0
        while data := source.read(chunk_bytes):
            part = f'.sync-parts/{namespace}/{key}/{index:06}'
            checksum = hashlib.sha256(data).hexdigest()
            parts.append(part)
            if done.get(part) != checksum:
                enqueue(part,data,checksum)
            index += 1
        flush()
        invalidate([name])
        _assemble_parts(config, [{'name': name, 'sha256': expected_hash, 'parts': parts}])
        done[name] = expected_hash
        for part in parts:
            done.pop(part, None)
        checkpoint()
    for name, path in wanted:
        if path.stat().st_size <= chunk_bytes:
            enqueue(name, path, hashes[name])
            flush()
        else:
            with path.open('rb') as source:
                send_large(name, source, hashes[name])
        if digest_file(path) != hashes[name] or [path.stat().st_size,path.stat().st_mtime_ns] != stamps[name]:
            raise ValueError('传输期间原文件改变；本轮不能导入。')
    for name, data in wanted_payloads:
        if len(data) > chunk_bytes:
            send_large(name, io.BytesIO(data), payload_hashes[name])
        else:
            enqueue(name, data, payload_hashes[name])
    flush()
    # A snapshot marker is part of the final payload set; import starts only now.
    return {**report, 'receipt': checkpoint_receipt, 'status': 'uploaded'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = read(args.config)
    directory = args.config.parent / 'sync'
    directory.mkdir(parents=True, exist_ok=True)
    files, payloads, summary = snapshot(config, directory)
    (directory / 'snapshot.json').write_bytes(json_bytes(summary))
    upload(config, directory, files, payloads, summary)


if __name__ == '__main__':
    main()
