"""Request-driven maintenance. Records phases, never generates image/card prose."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import time
import uuid

from .cards import fingerprint, read, save
from .library import EXTENSIONS, LOCAL, PROJECT, SourceError, item_source, source_key
from .locking import file_lock


def digest_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def scan_images(workspace):
    """Full identity scan; no writes, network calls, or note-body archive."""
    if workspace.settings.get('library') is None:
        raise SourceError('尚未配置图库；知识卡可独立检索，图片维护需先明确选择图库。')
    root = Path(workspace.settings['library']).resolve(strict=True)
    if not (root / 'images').is_dir():
        raise SourceError('图库不可读；不能把源盘掉线当作删除。')
    runtime = workspace.images.local
    old = {}
    if (runtime / 'index.sqlite3').exists():
        _, records, _ = workspace.images.snapshot()
        old = {r['item_id']: r for r in records}
    previous_path = runtime / 'maintenance-images.json'
    previous = read(previous_path) if previous_path.exists() else {}
    notes_path = runtime / 'notes-state.json'
    notes = read(notes_path) if notes_path.exists() else {}
    for value in (previous, notes):
        if value and value.get('source_key') != source_key(root):
            raise SourceError('维护记录属于另一图库。')
    records, changes = {}, {k: [] for k in ('new', 'changed', 'renamed', 'metadata', 'removed', 'notes', 'vectors')}
    skipped = {'unsupported': 0, 'deleted': 0}
    for folder in sorted((root / 'images').glob('*.info')):
        # Invalid current metadata is an error, never an implicit cloud deletion.
        meta = read(folder / 'metadata.json')
        if meta.get('isDeleted'):
            skipped['deleted'] += 1
            continue
        if str(meta.get('ext', '')).lower() not in EXTENSIONS:
            skipped['unsupported'] += 1
            continue
        item = folder.name.removesuffix('.info')
        original, meta = item_source(root, item)
        before = original.stat()
        digest = digest_file(original)
        after = original.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SourceError(f'扫描期间原图改变：{item}；未冻结范围。')
        record = {'sha256': digest, 'relative_path': original.relative_to(root).as_posix(),
                  'metadata_sha256': fingerprint(meta)}
        records[item] = record
        prior = previous.get('images', {}).get(item) or old.get(item)
        indexed = old.get(item)
        if prior is None:
            changes['new'].append(item)
        elif prior['sha256'] != digest:
            changes['changed'].append(item)
        elif prior['relative_path'] != record['relative_path']:
            changes['renamed'].append(item)
        if prior and prior.get('metadata_sha256') and prior['metadata_sha256'] != record['metadata_sha256']:
            changes['metadata'].append(item)
        completed = notes.get('completed', {}).get(item)
        note_digest = completed.get('sha256') if completed else (prior or {}).get('sha256')
        if '【ai生成】' not in (meta.get('annotation') or '') or (note_digest and note_digest != digest):
            changes['notes'].append(item)
        if indexed is None or indexed['sha256'] != digest:
            changes['vectors'].append(item)
    if not records:
        raise SourceError('有效图库为空；拒绝维护或同步删除。')
    changes['removed'] = sorted((set(old) | set(previous.get('images', {}))) - set(records))
    return {'source_key': source_key(root), 'library': str(root), 'images': records,
            'changes': changes, 'skipped': skipped, 'scanned_at': time.time(),
            'metadata_comparison': 'available' if previous else 'baseline_missing'}


def image_summary(scan):
    return {'eligible': len(scan['images']), **{k: len(v) for k, v in scan['changes'].items()},
            'skipped': scan['skipped'], 'content_verified': True,
            'metadata_comparison': scan.get('metadata_comparison', 'baseline_missing')}


def check_frozen(workspace, scope):
    root = Path(workspace.settings['library']).resolve(strict=True)
    if source_key(root) != scope['source_key']:
        raise SourceError('当前图库与冻结范围不同。')
    for item, record in scope['images'].items():
        original, _ = item_source(root, item)
        if digest_file(original) != record['sha256']:
            raise SourceError(f'冻结后原图内容改变：{item}；本轮未自动扩展范围。')


class MaintenanceRun:
    def __init__(self, workspace, run_id):
        if not run_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in run_id):
            raise ValueError('无效运行 ID。')
        self.workspace = workspace
        self.directory = LOCAL / 'maintenance/runs' / run_id
        self.path = self.directory / 'run.json'
        self.run_id = run_id

    @classmethod
    def create(cls, workspace, *, cloud=False, include_courses=True):
        if cloud:
            from .cloud import CloudManager
            CloudManager(PROJECT)
        scope = scan_images(workspace)
        courses = {'pending': [], 'roots': [], 'notice': '本次未启用课程处理。'}
        if include_courses:
            from .courses import CourseStore
            courses = CourseStore(workspace).scan()
        obj = cls(workspace, time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        state = {'schema_version': 1, 'run_id': obj.run_id, 'created_at': time.time(),
                 'workspace': str(PROJECT), 'stage': 'notes', 'status': 'running',
                 'cloud': cloud, 'image_scope': scope, 'course_scope': courses,
                 'receipts': {}, 'last_error': None}
        save(obj.path, state)
        from .note_batch import NoteBatch
        batch = NoteBatch(workspace, obj.directory / 'notes')
        batch.initialize(scope['library'], ids=scope['changes']['notes'],
                         expected_hashes={i: scope['images'][i]['sha256'] for i in scope['changes']['notes']})
        return obj

    def state(self):
        state = read(self.path)
        if Path(state['workspace']).resolve() != PROJECT or state['image_scope']['source_key'] != source_key(self.workspace.settings['library']):
            raise SourceError('运行记录属于其他工作区或图库。')
        return state

    def status(self):
        state = self.state()
        from .note_batch import NoteBatch
        return {'run_id': self.run_id, 'stage': state['stage'], 'status': state['status'],
                'images': image_summary(state['image_scope']),
                'notes': NoteBatch(self.workspace, self.directory / 'notes').status(),
                'courses_pending': len(state['course_scope']['pending']),
                'last_error': state.get('last_error'), 'next_action': state.get('next_action'),
                'record': str(self.path), 'receipts': state['receipts']}

    def resume(self, until=None):
        with file_lock(self.directory / '.run.lock'):
            state = self.state()
            if state['status'] == 'completed':
                return self.status()
            state.update(status='running', last_error=None, next_action=None)
            save(self.path, state)
            try:
                self._advance(state, until)
            except Exception as error:
                state.update(status='failed', last_error=str(error), next_action=f'maintain resume --run {self.run_id}')
                save(self.path, state)
                raise
            return self.status()

    def _advance(self, state, until=None):
        from .note_batch import NoteBatch
        if state['stage'] == 'notes':
            batch = NoteBatch(self.workspace, self.directory / 'notes')
            status = batch.status()
            if status['remaining_count'] or status['issues'] or status['inflight']:
                state.update(status='needs_agent', next_action=f'python -m paa.note_batch prepare --run "{batch.directory}" --worker writer-1')
                save(self.path, state)
                return
            state['receipts']['notes'] = {'record': str(batch.path), 'completed': status['completed_count']}
            state['stage'] = 'courses'
            save(self.path, state)
        if state['stage'] == 'courses':
            from .courses import CourseStore
            courses = CourseStore(self.workspace)
            result = courses.audit_scope(state['course_scope']['pending'])
            if not result['complete']:
                state.update(status='needs_agent', next_action=result['next_action'])
                save(self.path, state)
                return
            state['receipts']['courses'] = result
            state['stage'] = 'index'
            save(self.path, state)
        if state['stage'] == 'index':
            check_frozen(self.workspace, state['image_scope'])
            from .incremental import build_index
            online = self.workspace.settings.get('allow_paid_api', False) is True
            if self.workspace.settings.get('maintenance_vectors', True):
                state['receipts']['images'] = build_index(self.workspace.settings, allow_online=online,
                    item_ids=list(state['image_scope']['images']),
                    expected_hashes={i: r['sha256'] for i, r in state['image_scope']['images'].items()})
                state['receipts']['cards'] = self.workspace.index_cards(allow_online=online)
            else:
                state['receipts']['images'] = {'status': 'not_requested', 'reason': 'maintenance_vectors=false'}
                state['receipts']['cards'] = {'status': 'not_requested', 'reason': 'maintenance_vectors=false'}
            state['stage'] = 'cloud'
            save(self.path, state)
        if state['stage'] == 'cloud':
            if until == 'index':
                state.update(status='ready', next_action=f'maintain resume --run {self.run_id}')
                save(self.path, state)
                return
            if state['cloud']:
                from .cloud import CloudManager
                manager = CloudManager(PROJECT)
                cloud_id = state.get('cloud_run')
                if not cloud_id:
                    cloud_id = manager.create_sync(image_scope=state['image_scope'])
                    state['cloud_run'] = cloud_id
                    save(self.path, state)
                state['receipts']['cloud'] = manager.resume(cloud_id)
            else:
                state['receipts']['cloud'] = {'status': 'not_requested'}
            state['stage'] = 'finish'
            save(self.path, state)
        if state['stage'] == 'finish':
            # Do not enroll post-freeze additions or save complete annotation bodies.
            images = {}
            for item, record in state['image_scope']['images'].items():
                _, metadata = item_source(self.workspace.settings['library'], item)
                images[item] = {**record, 'metadata_sha256': fingerprint(metadata)}
            save(self.workspace.images.local / 'maintenance-images.json',
                 {'source_key': state['image_scope']['source_key'], 'images': images, 'run_id': self.run_id})
            state.update(status='completed', stage='done', completed_at=time.time(), next_action=None)
            save(self.path, state)


def revision(directory):
    top = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if top.returncode or Path(top.stdout.strip()).resolve() != Path(directory).resolve():
        return None
    result = subprocess.run(['git', '-C', str(directory), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def diagnostics(workspace, *, cloud=False):
    from .courses import CourseStore
    code = Path(__file__).resolve().parents[1]
    code_commit = revision(code)
    for ancestor in (code, *code.parents):
        if ancestor.parent == LOCAL / 'app/releases' and (ancestor / 'release.json').is_file():
            code_commit = read(ancestor / 'release.json')['commit']
            break
    try:
        images = image_summary(scan_images(workspace))
    except (OSError, ValueError) as error:
        images = {'state': 'unavailable', 'error': str(error), 'content_verified': False}
    course_store = CourseStore(workspace)
    course_state = course_store.state()
    course = {'roots': [{**r, 'available': Path(r['path']).is_dir()} for r in course_state['roots'].values()],
              'completed_records': sum(r.get('status') == 'completed' for r in course_state['media'].values()),
              'unfinished_preparations': sum(r.get('status') != 'completed' for r in course_state['media'].values()),
              'evidence': 'registered_records_and_live_directory_availability',
              'notice': '诊断不重哈希全部课程；发现新文件用 maintain plan 或 courses scan。'}
    unfinished = []
    for path in sorted((LOCAL / 'maintenance/runs').glob('*/run.json')):
        row = read(path)
        if row['status'] != 'completed':
            unfinished.append({'run_id': row['run_id'], 'stage': row['stage'], 'status': row['status']})
    deployed = LOCAL / 'cloud/deployed.json'
    recorded = read(deployed) if deployed.exists() else None
    result = {'checked_at': time.time(), 'code_root': str(code), 'code_commit': code_commit,
              'data_workspace': str(PROJECT), 'workspace_commit': revision(PROJECT),
              'images': images, 'courses': course, 'unfinished': unfinished,
              'cloud': {'evidence': 'local_record_only', 'deployed_commit': (recorded or {}).get('deployed_code_source_commit')}}
    if cloud:
        from .cloud import CloudManager
        try:
            result['cloud'] = CloudManager(PROJECT).status(live=True)
        except (OSError, ValueError, RuntimeError) as error:
            result['cloud'] = {**result['cloud'], 'evidence': 'live_check_failed', 'error': str(error)}
    active = LOCAL / 'app/active.json'
    try:
        result['installed'] = read(active) if active.exists() else None
    except (OSError, ValueError) as error:
        result['installed'] = {'error': str(error)}
    return result


def main(argv, workspace):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'run', 'resume', 'status'])
    parser.add_argument('--run', dest='run_id')
    route = parser.add_mutually_exclusive_group()
    route.add_argument('--local-only', action='store_true', help='默认行为：仅本机')
    route.add_argument('--cloud', action='store_true', help='明确请求云端；需要本工作区的连接配置')
    parser.add_argument('--no-courses', action='store_true')
    parser.add_argument('--until', choices=['index'], help='发布期间先完成本机阶段；不将整轮标为完成')
    args = parser.parse_args(argv)
    if args.action == 'plan':
        from .courses import CourseStore
        return {'images': image_summary(scan_images(workspace)),
                'courses': CourseStore.summary(CourseStore(workspace).scan()), 'applied': False}
    if args.action == 'run':
        try:
            run = MaintenanceRun.create(workspace, cloud=args.cloud, include_courses=not args.no_courses)
        except (OSError, ValueError, RuntimeError) as error:
            return {'status': 'failed', 'stage': 'preflight', 'run_id': None,
                    'error': str(error), 'next_action': 'doctor --summary'}
        return _resume_result(run, args.until)
    if args.action == 'status' and not args.run_id:
        rows = []
        for path in sorted((LOCAL / 'maintenance/runs').glob('*/run.json'), reverse=True):
            state = read(path)
            rows.append({k: state.get(k) for k in ('run_id', 'stage', 'status', 'created_at', 'last_error', 'next_action')})
        return {'runs': rows, 'notice': '恢复时使用明确run_id；不靠旧聊天猜测。'}
    if not args.run_id:
        parser.error('--run 必须指定明确的运行 ID。')
    run = MaintenanceRun(workspace, args.run_id)
    return _resume_result(run, args.until) if args.action == 'resume' else run.status()


def _resume_result(run, until):
    try:
        return run.resume(until)
    except (OSError, ValueError, RuntimeError):
        return run.status()
