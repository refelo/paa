"""Explicit, transactional source registration; no background directory scanning."""
import copy
import hashlib
import os
from pathlib import Path
import uuid

from .cards import CardError, read

INVENTORY = 'qa/source-inventory.json'
SCOPE = 'qa/source-scope.json'


def migrate(task):
    with task.store.lock():
        target = task.store.root / INVENTORY
        if target.exists():
            return {'migrated': False, 'sources': len(read(target)['sources'])}
        if task.base is None:
            raise CardError('首次迁移须以 --base 明确指定旧清单目录。')
        inventory = read(task.base / 'inventory.json')
        scope = read(task.base / 'scope-current.json')
        state = task.state()
        if not set(inventory['sources']).issubset(state['progress']):
            raise CardError('旧清单与当前进度不一致；未迁移。')
        task.store.commit_files({INVENTORY: inventory, SCOPE: scope})
        return {'migrated': True, 'sources': len(inventory['sources']), 'progress_unchanged': True}


def register(task, paths, *, collection=None, owner=None, apply=False):
    from .distillation import STATE, transcript_text
    expanded = []
    for supplied in paths:
        path = Path(supplied).resolve(strict=True)
        if path.is_dir():
            for candidate in path.rglob('*'):
                if candidate.is_file() and candidate.suffix.lower() in ('.txt', '.srt'):
                    if not candidate.resolve().is_relative_to(path):
                        raise CardError('资料链接超出本次指定目录。')
                    expanded.append(candidate)
        elif path.suffix.lower() in ('.txt', '.srt'):
            expanded.append(path)
        else:
            raise CardError('只登记明确指定的TXT/SRT资料。')
    with task.store.lock():
        inventory = copy.deepcopy(task.inventory())
        scope = task.scope()
        state = task.state()
        aliases = read(task.store.root / 'video-aliases.json')
        by_hash = {s['sha256']: sid for sid, s in inventory['sources'].items()}
        by_path = {str(Path(o['path']).resolve()): sid for sid, s in inventory['sources'].items() for o in s['occurrences']}
        additions, duplicates = [], 0
        roots = set(inventory.get('roots', [inventory['root']]))
        for path in sorted(set(expanded)):
            resolved = path.resolve(strict=True)
            if path.is_symlink():
                raise CardError('不沿资料符号链接扩大扫描范围。')
            data = resolved.read_bytes()
            text = transcript_text(data, path.suffix.lower())
            if not text.strip():
                continue
            digest = hashlib.sha256(data).hexdigest()
            try:
                rel = resolved.relative_to(Path(inventory['root']).resolve())
                inferred = rel.parts[0] if len(rel.parts) > 1 else resolved.parent.name
            except ValueError:
                inferred = resolved.parent.name
            group = collection or inferred
            if group in scope.get('excluded_collections', []):
                raise CardError('该合集仍在明确排除范围内；未登记。')
            source_owner = owner if owner is not None else group.removesuffix('的视频列表') if group.endswith('的视频列表') else None
            occurrence = {'path': str(resolved), 'collection': group, 'owner': source_owner}
            sid = by_hash.get(digest)
            if sid:
                if occurrence not in inventory['sources'][sid]['occurrences']:
                    inventory['sources'][sid]['occurrences'].append(occurrence)
                inventory['sources'][sid]['explicitly_registered'] = True
                duplicates += 1
                roots.add(str(resolved.parent))
                continue
            sid = 'D' + digest[:24]
            if sid in inventory['sources']:
                raise CardError('来源ID冲突。')
            prior = by_path.get(str(resolved))
            source = {'source_id': sid, 'sha256': digest, 'suffix': path.suffix.lower(),
                      'record_count': 1, 'text_chars': len(text), 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                      'line_count': len(text.splitlines()), 'state': 'unread', 'occurrences': [occurrence],
                      'explicitly_registered': True}
            if prior:
                source['revision_of'] = prior
                # Old and revised transcripts of the same file remain one video.
                aliases['source_to_video'][sid] = aliases['source_to_video'].get(prior, 'v_' + inventory['sources'][prior]['sha256'][:16])
                if state['progress'].get(prior) == 'unread':
                    old_archive = task.store.root.parent / 'sources' / prior / ('source' + inventory['sources'][prior]['suffix'])
                    if not old_archive.exists():
                        state['progress'][prior] = 'superseded_unread'
            inventory['sources'][sid] = source
            state['progress'][sid] = 'unread'
            by_hash[digest] = sid
            roots.add(str(resolved.parent))
            additions.append(sid)
            if apply:
                archive = task.store.root.parent / 'sources' / sid / ('source' + path.suffix.lower())
                archive.parent.mkdir(parents=True, exist_ok=True)
                if archive.exists() and archive.read_bytes() != data:
                    raise CardError('已有原文归档不同；不覆盖。')
                if not archive.exists():
                    temporary = archive.with_name(archive.name + '.' + uuid.uuid4().hex + '.tmp')
                    try:
                        temporary.write_bytes(data)
                        os.replace(temporary, archive)
                    finally:
                        temporary.unlink(missing_ok=True)
        inventory['roots'] = sorted(roots)
        if apply:
            task.store.commit_files({INVENTORY: inventory, SCOPE: scope, STATE: state, 'video-aliases.json': aliases})
        return {'applied': apply, 'new_sources': additions, 'duplicates': duplicates,
                'notice': '登记不等于已读；使用show完整阅读后stage，合适时归并。'}


def register_visual(task, empty_srt, media_sha256, *, collection, owner=None):
    """Register an actual empty transcript plus a distinct visual-media identity."""
    from .distillation import STATE, transcript_text
    path = Path(empty_srt).resolve(strict=True)
    data = path.read_bytes()
    if transcript_text(data, '.srt').strip() or len(media_sha256) != 64:
        raise CardError('视觉来源须有真实空字幕及完整原媒体SHA256。')
    sid = 'V' + media_sha256[:24]
    digest = hashlib.sha256(data).hexdigest()
    with task.store.lock():
        if collection in task.scope().get('excluded_collections', []):
            raise CardError('资料仍在排除范围内。')
        inventory, state = task.inventory(), task.state()
        if sid in inventory['sources']:
            if inventory['sources'][sid].get('media_sha256') != media_sha256:
                raise CardError('视觉来源身份冲突。')
            return sid
        inventory['sources'][sid] = {'source_id': sid, 'sha256': digest, 'suffix': '.srt',
            'record_count': 1, 'text_chars': 0, 'text_sha256': hashlib.sha256(b'').hexdigest(), 'line_count': 0,
            'state': 'unread', 'source_kind': 'visual_only', 'media_sha256': media_sha256,
            'explicitly_registered': True, 'occurrences': [{'path': str(path), 'collection': collection, 'owner': owner}]}
        archive = task.store.root.parent / 'sources' / sid / 'source.srt'
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists() and archive.read_bytes() != data:
            raise CardError('视觉来源归档冲突。')
        if not archive.exists():
            archive.write_bytes(data)
        state['progress'][sid] = 'unread'
        aliases = read(task.store.root / 'video-aliases.json')
        aliases['source_to_video'][sid] = 'v_' + media_sha256[:16]
        task.store.commit_files({INVENTORY: inventory, STATE: state, 'video-aliases.json': aliases})
    return sid
