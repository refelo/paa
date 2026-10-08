"""Reusable, explicitly registered course preparation and evidence accounting."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import time

from .automation import digest_file
from .cards import CardError, fingerprint, read, save
from .distillation import Distillation, transcript_text
from .library import LOCAL
from .locking import file_lock

MEDIA = {'.mp4', '.mov', '.mkv', '.m4v', '.webm', '.avi', '.mpg', '.mp3', '.wav'}
COMPLETE = {'covered_by_synthesis', 'distilled', 'no_useful_knowledge', 'duplicate_reviewed'}


class CourseStore:
    def __init__(self, workspace, local=None):
        self.workspace = workspace
        self.local = Path(local or LOCAL)
        self.path = self.local / 'data/course-maintenance.json'
        self.directory = self.local / 'maintenance/courses'

    def state(self):
        return read(self.path) if self.path.exists() else {'schema_version': 1, 'roots': {}, 'media': {}}

    def roots(self, path=None, *, collection=None, owner=None, disable=False):
        if path is None:
            return list(self.state()['roots'].values())
        root = Path(path).resolve()
        if root == self.local or root.is_relative_to(self.local) or self.local.is_relative_to(root):
            raise ValueError('课程原始目录不能覆盖项目输出或归档目录。')
        if not disable and not root.is_dir():
            raise ValueError('新增课程目录须当前可读。')
        key = fingerprint(str(root))[:16]
        with file_lock(self.path.with_suffix('.lock')):
            state = self.state()
            if disable and key not in state['roots']:
                raise ValueError('该目录尚未登记。')
            old = state['roots'].get(key, {})
            state['roots'][key] = {**old, 'id': key, 'path': str(root),
                                  'collection': collection or old.get('collection') or root.name,
                                  'owner': owner if owner is not None else old.get('owner'),
                                  'enabled': not disable}
            save(self.path, state)
        return state['roots'][key]

    def scan(self):
        state = self.state()
        roots, pending, seen, paths_seen = [], [], set(), set()
        excluded_path = Path(self.workspace.settings['card_store']) / 'qa/source-scope.json'
        excluded = read(excluded_path).get('excluded_collections', []) if excluded_path.exists() else []
        for item in sorted(state['roots'].values(), key=lambda r: len(Path(r['path']).parts), reverse=True):
            root = Path(item['path'])
            if not item['enabled'] or item['collection'] in excluded:
                roots.append({**item, 'state': 'excluded'})
                continue
            if not root.is_dir():
                roots.append({**item, 'state': 'unavailable'})
                continue
            count = 0
            for path in sorted(root.rglob('*')):
                if not path.is_file() or path.suffix.lower() not in MEDIA | {'.srt'}:
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise ValueError('资料链接超出登记目录。')
                if path.resolve() in paths_seen:
                    continue
                paths_seen.add(path.resolve())
                stamp = path.stat()
                digest = digest_file(path)
                after = path.stat()
                if (stamp.st_size, stamp.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('课程扫描期间文件改变。')
                key = 'M' + digest[:24]
                count += 1
                known = state['media'].get(key)
                if known and known['sha256'] != digest:
                    raise ValueError('课程身份冲突。')
                # One content identity, irrespective of duplicate file names.
                if key in seen:
                    continue
                seen.add(key)
                if not known or known.get('status') != 'completed':
                    pending.append({'id': key, 'path': str(path), 'sha256': digest,
                                    'collection': item['collection'], 'owner': item.get('owner'), 'root_id': item['id']})
            roots.append({**item, 'state': 'available', 'files': count})
        return {'roots': roots, 'pending': pending, 'scanned_at': time.time()}

    @staticmethod
    def summary(scan):
        return {'pending': len(scan['pending']), 'roots': scan['roots'],
                'notice': '不可用目录不表示空库或资料已删除；历史排除项不恢复。'}

    def adopt(self, manifest_path):
        """Explicit migration of user-confirmed completed runs; no old code imported."""
        path = Path(manifest_path).resolve(strict=True)
        manifest = read(path)
        rows = manifest['videos']
        groups = manifest.get('roots') or [manifest.get('root')]
        groups = [Path(p).resolve() for p in groups if p]
        imported, missing_hash = 0, []
        recorded_hashes = {}
        for hashes_file in sorted((path.parent / 'baseline').glob('source-hashes*.json')):
            recorded_hashes.update(read(hashes_file))
        with file_lock(self.path.with_suffix('.lock')):
            state = self.state()
            for root in groups:
                key = fingerprint(str(root))[:16]
                state['roots'].setdefault(key, {'id': key, 'path': str(root), 'collection': root.name, 'enabled': True})
            for row in rows:
                status_path = path.parent / 'status' / (row['id'] + '.json')
                old = read(status_path) if status_path.exists() else {}
                digest = old.get('source_sha256') or row.get('source_sha256') or row.get('sha256') or recorded_hashes.get(row['id'], {}).get('sha256')
                if not digest:
                    missing_hash.append(row['id'])
                key = 'M' + digest[:24] if digest else 'H' + fingerprint([str(path), row['id']])[:24]
                # The caller must explicitly authorize completed-baseline adoption.
                state['media'].setdefault(key, {'id': key, 'sha256': digest, 'path': row['path'],
                    'collection': row.get('group', ''), 'status': 'completed',
                    'evidence': 'user_confirmed_historical_completion', 'manifest': str(path),
                    'source_id': old.get('old_source_id'),
                    'historical_id': row['id'], 'srt_path': old.get('srt_path'),
                    'srt_sha256': old.get('srt_sha256')})
                imported += 1
            save(self.path, state)
        return {'adopted': imported, 'missing_hash': missing_hash,
                'notice': '无媒体哈希的历史完成项保留为H身份，不冒充可自动匹配的新文件哈希。'}

    def prepare(self, item, *, model='turbo', device='cuda', frame_step=90):
        if isinstance(item, str):
            candidates = self.scan()['pending']
            item = next((v for v in candidates if v['id'] == item), None)
            if item is None:
                raise ValueError('未找到该课程待办；请先扫描登记目录。')
        source = Path(item['path']).resolve(strict=True)
        if digest_file(source) != item['sha256']:
            raise ValueError('冻结后课程内容改变。')
        directory = self.directory / item['id']
        directory.mkdir(parents=True, exist_ok=True)
        with file_lock(directory / '.prepare.lock'):
            from .media import prepare_media
            result = prepare_media(source, directory, item['sha256'], model=model,
                                   device=device, frame_step=frame_step)
            srt = Path(result['srt_path'])
            source_id = None
            if transcript_text(srt.read_bytes(), '.srt').strip():
                from .source_registry import register
                task = Distillation(Path(self.workspace.settings['card_store']))
                registered = register(task, [srt], collection=item['collection'], owner=item.get('owner'), apply=True)
                source_id = 'D' + digest_file(srt)[:24]
                if source_id not in task.inventory()['sources']:
                    raise CardError('字幕登记没有产生可定位的来源。')
                # Cache paths change with content; original-media identity must not.
                previous = next((v for v in self.state()['media'].values()
                    if Path(v['path']).resolve() == source and v.get('source_id') and v['id'] != item['id']), None)
                if previous:
                    with task.store.lock():
                        aliases = read(task.store.root / 'video-aliases.json')
                        prior_id = previous['source_id']
                        inventory = task.inventory()
                        video = aliases['source_to_video'].get(prior_id, 'v_' + inventory['sources'][prior_id]['sha256'][:16])
                        aliases['source_to_video'][prior_id] = video
                        aliases['source_to_video'][source_id] = video
                        inventory['sources'][source_id]['revision_of'] = prior_id
                        task.store.commit_files({'video-aliases.json': aliases, 'qa/source-inventory.json': inventory})
                result['registration'] = registered
            elif result.get('frames') and read(result['frames']).get('sheets'):
                from .source_registry import register_visual
                task = Distillation(Path(self.workspace.settings['card_store']))
                source_id = register_visual(task, srt, item['sha256'], collection=item['collection'], owner=item.get('owner'))
            with file_lock(self.path.with_suffix('.lock')):
                state = self.state()
                prior = state['media'].get(item['id'], {})
                state['media'][item['id']] = {**prior, **item, **result, 'source_id': source_id,
                                             'status': prior.get('status', 'prepared')}
                save(self.path, state)
        return {'id': item['id'], 'source_id': source_id, 'srt_path': str(srt),
                'frames': result.get('frames'), 'status': 'needs_agent',
                'next_action': f'courses read {item["id"]}，实际看图后用现有 distill stage/merge；最后 courses review/audit。'}

    def get(self, item_id):
        row = self.state()['media'].get(item_id)
        if not row:
            raise ValueError('未知课程 ID。')
        return row

    def text(self, item_id, start=0, size=8000):
        row = self.get(item_id)
        path = Path(row['srt_path'])
        if digest_file(path) != row['srt_sha256']:
            raise ValueError('字幕已改变，需要重新登记修订。')
        content = transcript_text(path.read_bytes(), '.srt')
        if start < 0 or size < 1 or start > len(content):
            raise ValueError('无效阅读范围。')
        if row.get('source_id'):
            task = Distillation(Path(self.workspace.settings['card_store']))
            task.show(row['source_id'], start=start, size=size)
        return {'id': item_id, 'source_id': row.get('source_id'), 'start': start,
                'end': min(len(content), start + size), 'total_chars': len(content),
                'text': content[start:start + size], 'frames': row.get('frames'),
                'notice': '返回材料不等于已读；截断时补读，实际看图后再登记 review。'}

    def review(self, item_id, path):
        review = read(path)
        row = self.get(item_id)
        text = self.text(item_id)
        end = 0
        for start, stop in sorted(review.get('text_ranges', [])):
            if start < 0 or stop < start or stop > text['total_chars'] or start > end:
                raise ValueError('阅读范围有缺口或超出正文。')
            end = max(end, stop)
        if end != text['total_chars']:
            raise ValueError('未声明完整正文阅读；不能标记完成。')
        frames = read(row['frames']) if row.get('frames') else {'sheets': []}
        allowed = set(frames.get('sheets', [])) | {x['path'] for x in frames.get('details', [])}
        viewed = set(review.get('viewed_frames', []))
        if allowed and (not viewed or not viewed.issubset(allowed)):
            raise ValueError('须声明实际查看的有效画面文件；声明不能替代看图。')
        if not review.get('disposition'):
            raise ValueError('须给出语义处置或无知识的原因。')
        with file_lock(self.path.with_suffix('.lock')):
            state = self.state()
            state['media'][item_id]['review'] = {**review, 'recorded_at': time.time()}
            save(self.path, state)
        return {'id': item_id, 'review_recorded': True, 'semantic_completion': 'audit_required'}

    def compilation(self, candidate, baseline):
        path = Path(candidate).resolve(strict=True)
        if not path.is_relative_to(self.local.resolve()):
            raise ValueError('汇编候选必须在项目私有目录内。')
        target = self.local / 'data/image-reading-compilation.md'
        with file_lock(self.local / 'data/.compilation.lock'):
            actual = digest_file(target) if target.exists() else None
            if actual != baseline:
                raise ValueError('汇编基线已改变，不能覆盖其他写入。')
            data = path.read_bytes()
            if not data.strip():
                raise ValueError('不能发布空汇编。')
            store = self.workspace.cards()
            with store.lock():
                token = store.snapshot()[2]
                if target.exists():
                    history = self.local / 'data/compilation-history' / (actual + '.md')
                    history.parent.mkdir(parents=True, exist_ok=True)
                    if not history.exists():
                        shutil.copyfile(target, history)
                temporary = target.with_suffix('.pending.md')
                temporary.write_bytes(data)
                temporary.replace(target)
                receipt = {'sha256': digest_file(target), 'cards_token': token, 'updated_at': time.time()}
                save(self.local / 'data/compilation-receipt.json', receipt)
        return receipt

    def audit_scope(self, pending):
        if not pending:
            return {'complete': True, 'courses': 0, 'notice': '没有新课程；历史已完成资料不重跑。'}
        state = self.state()
        task = Distillation(Path(self.workspace.settings['card_store']))
        progress = task.state()
        problems = []
        for item in pending:
            row = state['media'].get(item['id'])
            if not row or row.get('sha256') != item['sha256']:
                problems.append({'id': item['id'], 'reason': 'needs_prepare'})
                continue
            if row.get('status') == 'completed':
                continue
            if not row.get('review'):
                problems.append({'id': item['id'], 'reason': 'needs_full_read_and_visual_review'})
                continue
            source_id = row.get('source_id')
            if not source_id and row['review'].get('disposition') != 'no_useful_knowledge':
                problems.append({'id': item['id'], 'reason': 'visual_knowledge_requires_registered_merge_evidence'})
                continue
            if not source_id and not row['review'].get('reason'):
                problems.append({'id': item['id'], 'reason': 'empty_source_requires_disposition_reason'})
                continue
            if source_id and progress['progress'].get(source_id) not in COMPLETE:
                problems.append({'id': item['id'], 'reason': 'needs_semantic_merge'})
                continue
            if source_id:
                aliases = read(task.store.root / 'video-aliases.json')['source_to_video']
                video = aliases.get(source_id)
                unresolved = [d for d in progress['drafts'].values()
                              if d.get('state') == 'candidate' and video in d.get('video_ids', [])]
                if unresolved:
                    problems.append({'id': item['id'], 'reason': 'unmerged_candidates'})
                    continue
            if digest_file(row['srt_path']) != row['srt_sha256']:
                problems.append({'id': item['id'], 'reason': 'srt_changed'})
        receipt_path = self.local / 'data/compilation-receipt.json'
        receipt = read(receipt_path) if receipt_path.exists() else {}
        compilation = self.local / 'data/image-reading-compilation.md'
        if (receipt.get('cards_token') != self.workspace.cards().snapshot()[2]
                or not compilation.is_file() or receipt.get('sha256') != digest_file(compilation)):
            problems.append({'reason': 'needs_compilation_update_or_reviewed_unchanged_candidate'})
        if problems:
            return {'complete': False, 'problems': problems,
                    'next_action': 'courses prepare/read/review；用 distill stage/merge 完成归并，再 courses compilation --candidate ... --baseline ...'}
        with file_lock(self.path.with_suffix('.lock')):
            state = self.state()
            for item in pending:
                state['media'][item['id']]['status'] = 'completed'
            save(self.path, state)
        return {'complete': True, 'courses': len(pending), 'compilation': receipt}


def main(argv, workspace):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['roots', 'scan', 'prepare', 'frames', 'read', 'review', 'status', 'audit', 'adopt', 'compilation'])
    parser.add_argument('target', nargs='?')
    parser.add_argument('--collection')
    parser.add_argument('--owner', help='仅当用户明确讲师/主体时登记；不从收藏推断')
    parser.add_argument('--disable', action='store_true')
    parser.add_argument('--completed-baseline', action='store_true')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--size', type=int, default=8000)
    parser.add_argument('--model', default='turbo')
    parser.add_argument('--device', choices=['cuda', 'cpu'], default='cuda')
    parser.add_argument('--frame-step', type=float, default=90)
    parser.add_argument('--seconds', help='详情帧的逗号分隔时间，单位秒')
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--baseline')
    args = parser.parse_args(argv)
    store = CourseStore(workspace)
    if args.action == 'roots':
        return store.roots(args.target, collection=args.collection, owner=args.owner, disable=args.disable)
    if args.action == 'scan':
        scan = store.scan()
        return {**store.summary(scan), 'items': scan['pending']}
    if args.action == 'status':
        state = store.state()
        return {'roots': store.roots(), 'media': len(state['media']),
                'completed': sum(r['status'] == 'completed' for r in state['media'].values()),
                'pending': [k for k, r in state['media'].items() if r['status'] != 'completed']}
    if args.action == 'compilation':
        if not args.candidate:
            parser.error('--candidate 必填；--baseline 为当前汇编 SHA256。')
        return store.compilation(args.candidate, args.baseline)
    if args.action == 'audit':
        scope = _scope(read(args.file)) if args.file else store.scan()['pending']
        return store.audit_scope(scope)
    if not args.target:
        parser.error('该操作需要课程 ID 或明确路径。')
    if args.action == 'adopt':
        if not args.completed_baseline:
            parser.error('只接纳用户已确认完成的历史范围，须给出 --completed-baseline。')
        return store.adopt(args.target)
    if args.action == 'prepare':
        target = args.target
        if args.file:
            target = next((i for i in _scope(read(args.file)) if i['id'] == args.target), None)
            if target is None:
                parser.error('课程不在指定冻结范围内。')
        return store.prepare(target, model=args.model, device=args.device, frame_step=args.frame_step)
    if args.action == 'read':
        return store.text(args.target, args.start, args.size)
    if args.action == 'frames':
        if not args.seconds:
            parser.error('frames 需要 --seconds，例如 65,75。')
        row = store.get(args.target)
        from .media import sample_frames
        directory = store.directory / args.target
        cache = directory / ('input' + Path(row['path']).suffix.lower())
        if not cache.is_file() or digest_file(cache) != row['sha256']:
            raise ValueError('缺少已核对的媒体缓存；先 prepare。')
        frames = sample_frames(cache, directory, seconds=[float(x) for x in args.seconds.split(',')])
        path = Path(row['frames'])
        previous = read(path)
        merged = {r['path']: r for r in previous.get('details', []) + frames['details']}
        previous['details'] = list(merged.values())
        save(path, previous)
        return frames
    if args.action == 'review':
        if not args.file:
            parser.error('review 需要 --file。')
        return store.review(args.target, args.file)
    raise AssertionError('unreachable')


def _scope(value):
    if isinstance(value, list):
        return value
    return value.get('course_scope', value)['pending']
