"""Workspace-only diagnostics and resumable note work; no generated-text archive."""
import hashlib
from pathlib import Path
import time

from .cards import read, save
from .library import LOCAL, PROJECT, catalog, item_source, source_key
from .locking import file_lock


def doctor(workspace):
    checks = {}
    try:
        checks['library'] = workspace.status()
    except (OSError, ValueError) as error:
        checks['library'] = {'error': str(error)}
    store = Path(workspace.settings['card_store'])
    registry = store / 'qa/source-inventory.json'
    checks['sources_registered'] = len(read(registry)['sources']) if registry.exists() else None
    progress = store / 'qa/distillation-v2.json'
    checks['unread_sources'] = sum(v == 'unread' for v in read(progress)['progress'].values()) if progress.exists() else None
    checks['pending_card_transaction'] = (store / '.pending.json').exists()
    checks['legacy_card_lock'] = (store / '.writer.lock').exists()
    checks['workspace'] = str(PROJECT)
    active = LOCAL / 'app/active.json'
    checks['installed'] = read(active) if active.exists() else None
    checks['notice'] = '只读诊断，不调用API、不自动修复；索引覆盖不等于文件内容全部重新校验。'
    return checks


def notes_pending(workspace, *, ids=None, bootstrap=False):
    root = workspace.settings['library']
    path = workspace.images.local / 'notes-state.json'
    state = read(path) if path.exists() else {'source_key': source_key(root), 'completed': {}}
    if state['source_key'] != source_key(root):
        raise ValueError('备注状态属于其他图库。')
    available, skipped = catalog(root)
    requested = set(ids or [])
    if requested - set(available):
        raise ValueError('指定的Eagle ID不属于当前有效图片。')
    pending = []
    for item in available:
        original, context = item_source(root, item)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        if bootstrap and '【ai生成】' in (context.get('annotation') or '') and item not in state['completed']:
            state['completed'][item] = {'sha256': digest, 'status': 'completed', 'origin': 'existing-ai-section'}
        reason = 'explicit' if item in requested else 'content_changed' if item in state['completed'] and state['completed'][item]['sha256'] != digest else 'missing_ai' if '【ai生成】' not in (context.get('annotation') or '') else None
        if reason:
            pending.append({'eagle_id': item, 'sha256': digest, 'reason': reason})
    if bootstrap:
        save(path, state)
    return {'pending': pending, 'count': len(pending), 'completed': len(state['completed']), 'skipped': skipped}


def note_completed(workspace, image):
    path = workspace.images.local / 'notes-state.json'
    key = source_key(workspace.settings['library'])
    with file_lock(path.parent / '.notes-state.lock', timeout=60):
        state = read(path) if path.exists() else {'source_key': key, 'completed': {}}
        if state['source_key'] != key:
            raise ValueError('备注状态属于其他图库。')
        state['completed'][image['eagle_id']] = {'sha256': image['content_sha256'], 'status': 'completed', 'updated_at': time.time()}
        save(path, state)
