"""Workspace-only rolling batches for an explicitly authorized note rewrite."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time

from .cards import read, save
from .library import LOCAL, PROJECT, SourceError, catalog, source_key
from .maintenance import note_completed
from .notes import MARKER, apply_draft, compose


class NoteBatch:
    def __init__(self, workspace, directory):
        self.workspace = workspace
        self.directory = Path(directory).resolve()
        if not self.directory.is_relative_to(LOCAL.resolve()) or self.directory == LOCAL.resolve():
            raise SourceError('批次目录必须在本项目 .local 内。')
        self.path = self.directory / 'progress.json'

    @contextmanager
    def lock(self, *, worker=None, wait_seconds=30):
        self.directory.mkdir(parents=True, exist_ok=True)
        if worker is not None:
            self.worker_files(worker)
        path = self.directory / (f'.apply-{worker}.lock' if worker else '.coordinator.lock')
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                stream = path.open('x', encoding='utf-8')
                break
            except FileExistsError as error:
                if time.monotonic() >= deadline:
                    raise SourceError('已有批次协调者；若上次中断，先核对锁内 PID 已退出。') from error
                time.sleep(0.05)
        try:
            with stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            path.unlink()

    def state(self):
        value = read(self.path)
        if value['source_key'] != source_key(self.workspace.settings['library']):
            raise SourceError('批次属于其他图库，不能续写。')
        return value

    def worker_files(self, worker):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', worker):
            raise SourceError('执行者名称只能含字母、数字、下划线或连字符。')
        base = self.directory / worker
        return base / 'input.json', base / 'drafts.json', base / 'current-draft.json'

    def initialize(self, library, *, ids=None, expected_hashes=None):
        if Path(library).resolve() != self.workspace.settings['library'].resolve():
            raise SourceError('指定图库与本工作区配置不一致。')
        with self.lock():
            if self.path.exists():
                if ids is not None and set(ids) != set(self.state()['scope_ids']):
                    raise SourceError('不能用另一个范围覆盖已有批次。')
                return self.status()  # Never reset completed or in-flight work.
            available, skipped = catalog(self.workspace.settings['library'])
            if ids is None:
                ids = available
            else:
                ids = list(dict.fromkeys(ids))
                if set(ids) - set(available):
                    raise SourceError('冻结范围包含当前图库不可用的图片。')
            if expected_hashes is not None and set(expected_hashes) != set(ids):
                raise SourceError('冻结内容哈希与图片范围不一致。')
            save(self.path, {'schema_version': 1, 'library': str(Path(library).resolve()),
                             'source_key': source_key(Path(library)), 'scope_ids': ids,
                             'skipped_at_start': skipped, 'completed_ids': [],
                             'inflight': {}, 'issues': {},
                             'expected_hashes': expected_hashes or {}})
            return self.status()

    def status(self):
        state = self.state()
        done = set(state['completed_ids'])
        inflight = {key: [i for i in ids if i not in done]
                    for key, ids in state['inflight'].items()}
        return {'library': state['library'], 'scope_count': len(state['scope_ids']),
                'completed_count': len(done), 'remaining_count': len(state['scope_ids']) - len(done),
                'inflight': {key: ids for key, ids in inflight.items() if ids},
                'issues': state['issues'], 'skipped_at_start': state['skipped_at_start']}

    def prepare(self, worker, size=12):
        if type(size) is not int or size < 1:
            raise SourceError('批大小须为正整数，由任务按实际情况调整。')
        input_path, draft_path, _ = self.worker_files(worker)
        with self.lock():
            state = self.state()
            done = set(state['completed_ids'])
            pending = [i for i in state['inflight'].get(worker, []) if i not in done]
            if any(state['issues'].get(i, {}).get('state') == 'write_uncertain' for i in pending):
                raise SourceError('上次写回未确认，先 reconcile，不重新分配或自动重写。')
            if pending and not any(i in state['issues'] for i in pending):
                if not input_path.is_file():
                    raise SourceError('进行中批次缺输入文件，不能猜测草稿身份。')
                return {'input': str(input_path), 'drafts': str(draft_path), 'count': len(pending), 'resumed': True}
            held = {i for ids in state['inflight'].values() for i in ids}
            candidates = pending or [i for i in state['scope_ids']
                                     if i not in done and i not in held and i not in state['issues']]
            rows = []
            limit = len(pending) if pending else size
            for item_id in candidates:
                if len(rows) >= limit:
                    break
                try:
                    image = self.workspace.images.get(item_id)
                    expected = state.get('expected_hashes', {}).get(item_id)
                    if expected and image['content_sha256'] != expected:
                        raise SourceError('冻结后原图内容改变，不能沿用本次观察范围。')
                    row = {k: image[k] for k in ('eagle_id', 'asset_id', 'preview_path',
                                                 'frame_count', 'frame_previews', 'context')}
                    rows.append(row)
                    state['issues'].pop(item_id, None)
                except (OSError, ValueError) as error:
                    state['issues'][item_id] = {'state': 'read_failed', 'detail': str(error)}
            # A refreshed batch must receive new observations, never stale drafts.
            save(input_path, rows)
            save(draft_path, {'viewed_ids': [], 'drafts': []})
            state['inflight'][worker] = [r['eagle_id'] for r in rows]
            save(self.path, state)
            return {'input': str(input_path), 'drafts': str(draft_path), 'count': len(rows), 'resumed': False}

    def drafts(self, worker, state):
        input_path, draft_path, single_path = self.worker_files(worker)
        rows = read(input_path)
        assigned = state['inflight'].get(worker, [])
        if not assigned or [r['eagle_id'] for r in rows] != assigned:
            raise SourceError('输入与本批分配不一致。')
        value = read(draft_path)
        if not isinstance(value, dict) or set(value) != {'viewed_ids', 'drafts'}:
            raise SourceError('批草稿须包含 viewed_ids 与 drafts；格式见批量说明。')
        expected = {r['asset_id']: r for r in rows}
        if len(expected) != len(rows) or any(not i.startswith('eagle:') for i in expected):
            raise SourceError('输入须包含唯一完整 asset_id。')
        bodies = {}
        for draft in value['drafts']:
            if not isinstance(draft, dict) or set(draft) != {'asset_id', 'body'}:
                raise SourceError('每条草稿仅含 asset_id、body。')
            key = draft['asset_id']
            if key not in expected or key in bodies:
                raise SourceError('草稿身份未知、重复或不属于当前批次。')
            compose('', draft['body'])
            bodies[key] = draft['body']
        viewed = value['viewed_ids']
        if (set(bodies) != set(expected) or not isinstance(viewed, list)
                or len(viewed) != len(expected) or set(viewed) != set(expected)):
            raise SourceError('草稿或已看图声明未覆盖本批；声明本身不替代实际看图。')
        return rows, bodies, single_path

    def apply(self, worker, *, apply=False, api=None):
        # A worker owns its draft file for the whole call. Other workers may
        # write different items while the coordinator lock protects progress.
        with self.lock(worker=worker):
            with self.lock():
                state = self.state()
                rows, bodies, single_path = self.drafts(worker, state)
                ids = {row['eagle_id'] for row in rows}
                held_elsewhere = {item for name, assigned in state['inflight'].items()
                                  if name != worker for item in assigned}
                if ids & held_elsewhere:
                    raise SourceError('图片被重复分配给不同执行者，拒绝写回。')
                done = set(state['completed_ids'])
                if any(item in state['issues'] for item in ids - done):
                    raise SourceError('本批有未解决项；先 reconcile 或重新 prepare。')
            count = 0
            for row in rows:
                item_id = row['eagle_id']
                with self.lock():
                    state = self.state()
                    if item_id not in state['inflight'].get(worker, []):
                        raise SourceError('当前图片已不属于本执行者，拒绝写回。')
                    if item_id in state['completed_ids']:
                        continue
                    if item_id in state['issues']:
                        raise SourceError('本批有未解决项；先 reconcile 或重新 prepare。')
                    if apply:
                        state['issues'][item_id] = {'state': 'write_uncertain',
                                                    'detail': '写回进行中或结果尚未确认'}
                        save(self.path, state)
                save(single_path, {'asset_id': row['asset_id'], 'body': bodies[row['asset_id']]})
                try:
                    result = apply_draft(self.workspace, single_path, apply=apply, api=api)
                except (OSError, ValueError) as error:
                    if apply:
                        with self.lock():
                            state = self.state()
                            state['issues'][item_id]['detail'] = str(error)
                            save(self.path, state)
                    raise
                if result['applied']:
                    with self.lock():
                        state = self.state()
                        if item_id not in state['completed_ids']:
                            state['completed_ids'].append(item_id)
                        state['issues'].pop(item_id, None)
                        save(self.path, state)
                count += 1
            return {'applied': apply, 'count': count, **self.status()}

    def reconcile(self, worker):
        """Read back uncertain writes; never retry the API or archive annotations."""
        with self.lock():
            state = self.state()
            rows, bodies, _ = self.drafts(worker, state)
            for row in rows:
                item_id = row['eagle_id']
                if state['issues'].get(item_id, {}).get('state') != 'write_uncertain':
                    continue
                try:
                    image = self.workspace.images.get(row['asset_id'])
                    current = image['context'].get('annotation') or ''
                    if MARKER not in current or current.split(MARKER, 1)[1].strip() != bodies[row['asset_id']].strip():
                        raise SourceError('未读到本稿；重新 prepare 取图后再处理。')
                    note_completed(self.workspace, image)
                    if item_id not in state['completed_ids']:
                        state['completed_ids'].append(item_id)
                    state['issues'].pop(item_id, None)
                except (OSError, ValueError) as error:
                    state['issues'][item_id] = {'state': 'needs_rewrite', 'detail': str(error)}
                save(self.path, state)
            return self.status()

    def retry(self, item_id):
        with self.lock():
            state = self.state()
            if item_id not in state['issues']:
                raise SourceError('该 ID 不是当前问题项。')
            if state['issues'][item_id]['state'] != 'read_failed':
                raise SourceError('未确认写回请先 reconcile，需重看项请重新 prepare。')
            state['issues'].pop(item_id)
            save(self.path, state)
            return self.status()


def main():
    parser = argparse.ArgumentParser(description='已授权的本地备注批次；默认只预览，绝不调用生成 API')
    parser.add_argument('action', choices=['init', 'status', 'prepare', 'apply', 'reconcile', 'retry'])
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--library', help='init 必须明确指定已授权图库')
    parser.add_argument('--worker', default='writer-1')
    parser.add_argument('--size', type=int, default=12)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--id', dest='item_id')
    parser.add_argument('--ids-file', type=Path, help='init 的显式 Eagle ID JSON 数组')
    args = parser.parse_args()
    if not Path.cwd().resolve().is_relative_to(PROJECT):
        parser.error('批量维护仅在 PAA 工作区执行。')
    from .workspace import Workspace
    batch = NoteBatch(Workspace(), args.run)
    if args.action == 'init':
        if not args.library:
            parser.error('init 需要 --library；本命令只冻结范围，不开始看图或写回。')
        result = batch.initialize(args.library, ids=read(args.ids_file) if args.ids_file else None)
    elif args.action == 'prepare':
        result = batch.prepare(args.worker, args.size)
    elif args.action == 'apply':
        result = batch.apply(args.worker, apply=args.apply)
    elif args.action == 'reconcile':
        result = batch.reconcile(args.worker)
    elif args.action == 'retry':
        result = batch.retry(args.item_id)
    else:
        result = batch.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
