"""Manual cloud maintenance over the project's existing SSH connection."""
from __future__ import annotations

import argparse
import hashlib
import io
import inspect
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import time
import uuid

from .cards import read, save
from .cloud_transfer import json_bytes, snapshot, ssh_args, upload
from .library import PROJECT
from .locking import file_lock

REMOTE_PYTHON = '/opt/paa/.venv/bin/python'
REMOTE_CODE = '/opt/paa/current'
REMOTE_WORKSPACE = '/srv/paa/workspace'


def restore_code(root, current, previous, retained):
    """Move only program directories; also works if current disappeared mid-swap."""
    from pathlib import Path
    root = Path(root).resolve()
    paths = [Path(value).resolve() for value in (current, previous, retained)]
    if any(p == root or not p.is_relative_to(root) for p in paths) or len(set(paths)) != 3:
        raise ValueError('Program recovery paths must be distinct descendants of the code root.')
    current, previous, retained = paths
    if not previous.is_dir() or retained.exists():
        raise ValueError('Recovery source unavailable or retained target already exists.')
    if current.exists():
        current.rename(retained)
    previous.rename(current)



class CloudManager:
    def __init__(self, workspace=PROJECT):
        self.workspace = Path(workspace).resolve()
        self.directory = self.workspace / '.local/cloud'
        self.config = read(self.directory / 'connection.json')
        if Path(self.config['source_workspace']).resolve() != self.workspace:
            raise ValueError('云端连接指向另一个电脑资料工作区。')

    def remote(self, command, *, input_data=None):
        result = subprocess.run([*ssh_args(self.config), '-T', command], input=input_data,
                                capture_output=True)
        if result.returncode:
            raise RuntimeError('云端命令失败：' + result.stderr.decode('utf-8', 'replace')[-3000:])
        return result.stdout.decode('utf-8', 'replace')

    def operation(self, action, *args):
        privilege = 'sudo' if action == 'receipt' else 'sudo -u paa'
        command = f'cd {REMOTE_WORKSPACE} && {privilege} env PAA_WORKSPACE={REMOTE_WORKSPACE} PYTHONPATH={REMOTE_CODE} {REMOTE_PYTHON} -m paa.cloud_remote '
        return json.loads(self.remote(command + ' '.join(shlex.quote(str(a)) for a in (action, *args))))

    def verify_running(self):
        # The prior release may predate cloud_remote. Probe it without installing
        # new maintenance code into the release being verified.
        code = Path(__file__).with_name('cloud_remote.py').read_text(encoding='utf-8')
        code = code.replace('from . import ', 'from paa import ').replace('from .', 'from paa.')
        command = f'cd {REMOTE_WORKSPACE} && sudo -u paa env PAA_WORKSPACE={REMOTE_WORKSPACE} PYTHONPATH={REMOTE_CODE} {REMOTE_PYTHON} - verify'
        return json.loads(self.remote(command, input_data=code.encode()))

    def _restore(self, previous, retained):
        code = inspect.getsource(restore_code) + '\nrestore_code(' + ','.join(repr(v) for v in ('/opt/paa', REMOTE_CODE, previous, retained)) + ')\n'
        self.remote(f'sudo {REMOTE_PYTHON} -', input_data=code.encode())

    def _recovery_copy(self):
        result = subprocess.run([*ssh_args(self.config), '-T', f'sudo tar --exclude=__pycache__ -cf - -C {REMOTE_CODE} paa scripts'], capture_output=True)
        if result.returncode:
            raise RuntimeError('恢复程序副本下载失败。')
        root = self.directory.resolve()
        current = root / 'runtime'
        retained = root / ('runtime-before-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        if not current.resolve().is_relative_to(root) or not retained.resolve().is_relative_to(root):
            raise ValueError('恢复副本路径越界。')
        if current.exists():
            current.rename(retained)
        current.mkdir()
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(current, filter='data')

    def status(self, *, live=False):
        result = {'evidence': 'local_record_only'}
        result['unfinished_syncs'] = []
        for path in sorted((self.directory / 'runs').glob('*/run.json'), reverse=True):
            state = read(path)
            if state['status'] != 'completed':
                result['unfinished_syncs'].append({**{k: state.get(k) for k in ('sync_id', 'stage', 'status', 'last_error')},
                                                   'record': str(path)})
        for name in ('deployed.json', 'last-maintenance-sync.json'):
            path = self.directory / name
            if path.exists():
                value = read(path)
                if name == 'deployed.json':
                    value = {k: value.get(k) for k in ('recorded_date', 'deployed_code_source_commit', 'release', 'rollback')}
                result[name] = value
        if live:
            try:
                result['live'] = self.operation('status')
                result['evidence'] = 'live_ssh_and_local_records'
            except (OSError, ValueError, RuntimeError) as error:
                result['live'] = {'error': str(error)}
                result['evidence'] = 'live_check_failed_and_local_records'
        return result

    def create_sync(self, image_scope=None):
        if image_scope is None:
            from .automation import scan_images
            from .workspace import Workspace
            image_scope = scan_images(Workspace())
        run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        directory = self.directory / 'runs' / run_id
        directory.mkdir(parents=True)
        files, payloads, summary = snapshot(self.config, directory, image_scope)
        summary['sync_id'] = run_id
        payloads['workspace/.local/import/snapshot.json'] = json_bytes(summary)
        for name, data in payloads.items():
            path = directory / 'payloads' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        stamps = {name: [path.stat().st_size, path.stat().st_mtime_ns] for name, path in files.items()}
        save(directory / 'run.json', {'sync_id': run_id, 'stage': 'upload', 'status': 'ready',
             'summary': summary, 'files': {n: str(p) for n, p in files.items()}, 'stamps': stamps,
             'payloads': list(payloads), 'receipts': {}, 'image_scope': image_scope})
        return run_id

    def _path(self, run_id):
        if not re.fullmatch('[A-Za-z0-9_-]+', run_id):
            raise ValueError('无效同步 ID。')
        return self.directory / 'runs' / run_id / 'run.json'

    def resume(self, run_id):
        path = self._path(run_id)
        with file_lock(self.directory / '.sync.lock'):
            state = read(path)
            if state['status'] == 'completed':
                return self._summary(state)
            state.update(status='running', last_error=None)
            save(path, state)
            try:
                if state['stage'] == 'cleanup':
                    self._finish_sync(path, state)
                    return self._summary(state)
                if state['stage'] == 'upload':
                    last_path = self.directory / 'last-maintenance-sync.json'
                    last = read(last_path) if last_path.exists() else {}
                    if last.get('data_fingerprint') == state['summary']['data_fingerprint']:
                        remote = self.operation('status')
                        if (remote.get('maintenance-sync.json', {}).get('data_fingerprint') == last['data_fingerprint']
                                and remote['service'] == 'active'):
                            state.update(no_changes=True)
                            state['receipts']['verify'] = {'service': 'active', 'receipt_matches': True}
                            self._finish_sync(path, state)
                            return self._summary(state)
                    files = {name: Path(p) for name, p in state['files'].items()}
                    for name, file in files.items():
                        stat = file.stat()
                        if [stat.st_size, stat.st_mtime_ns] != state['stamps'][name]:
                            raise ValueError('快照之后上传源文件改变；需重新取得同步快照。')
                    payloads = {name: (path.parent / 'payloads' / name).read_bytes() for name in state['payloads']}
                    upload_state = self.directory / 'sync/uploaded.json'
                    upload_state.parent.mkdir(parents=True, exist_ok=True)
                    def progress(value):
                        state['receipts']['upload_progress'] = dict(value)
                        save(path, state)
                    state['receipts']['upload'] = upload(self.config, path.parent, files, payloads, state['summary'], upload_state,
                        lambda: self.operation('hashes')['hashes'], progress=progress)
                    state['stage'] = 'import'
                    save(path, state)
                if state['stage'] == 'import':
                    self._expect_generation(self.operation('sync-state')['incoming'], state)
                    result = self.remote(f'sudo env PAA_WORKSPACE={REMOTE_WORKSPACE} PYTHONPATH={REMOTE_CODE} {REMOTE_PYTHON} -m paa.cloud_importer --sync-id {run_id} --fingerprint {state["summary"]["data_fingerprint"]}')
                    state['receipts']['import'] = json.loads(result)
                    self._expect_generation(state['receipts']['import'], state)
                    state['stage'] = 'refresh'
                    save(path, state)
                if state['stage'] == 'refresh':
                    self._expect_generation(self.operation('sync-state')['imported'], state)
                    # Credentials remain under systemd; their contents never enter Python logs or SSH arguments.
                    unit = 'paa-refresh-' + run_id
                    command = (f'sudo systemd-run --unit={unit} --collect --wait --pipe '
                        f'-p WorkingDirectory={REMOTE_WORKSPACE} '
                        '-p LoadCredentialEncrypted=voyage:/etc/credstore.encrypted/paa-voyage.cred '
                        f'-E PAA_WORKSPACE={REMOTE_WORKSPACE} -E PYTHONPATH={REMOTE_CODE} '
                        f'{REMOTE_PYTHON} {REMOTE_CODE}/scripts/cloud_refresh.py '
                        f'--sync-id {run_id} --fingerprint {state["summary"]["data_fingerprint"]}')
                    result = self.remote(command)
                    state['receipts']['refresh'] = json.loads(result)
                    self._expect_generation(state['receipts']['refresh'], state)
                    state['stage'] = 'start'
                    save(path, state)
                if state['stage'] == 'start':
                    remote_state = self.operation('sync-state')
                    for label in ('imported', 'refreshed'):
                        self._expect_generation(remote_state[label], state)
                    self.remote('set -e\n'
                        'sudo chown -R ubuntu:paa /srv/paa/library.library /srv/paa/workspace/.local/data\n'
                        'sudo find /srv/paa/library.library /srv/paa/workspace/.local/data -type d -exec chmod 750 {} +\n'
                        'sudo find /srv/paa/library.library /srv/paa/workspace/.local/data -type f -exec chmod 640 {} +\n'
                        'sudo chown -R paa:paa /srv/paa/workspace/.local/runtime\n'
                        'if [ -d /srv/paa/workspace/.local/previews ]; then sudo chown -R paa:paa /srv/paa/workspace/.local/previews; fi\n'
                        'sudo systemctl start paa.service\n')
                    state['stage'] = 'verify'
                    save(path, state)
                if state['stage'] == 'verify':
                    verified = self.operation('verify')
                    self._expect_generation(verified.get('last-sync.json', {}), state)
                    self._expect_generation(verified.get('cloud-refresh.json', {}), state)
                    current = verified['status']
                    if current.get('eligible') != state['summary']['images'] or current.get('indexed') != current.get('eligible'):
                        raise ValueError('云端图片范围或向量覆盖与同步快照不一致。')
                    if current.get('active_cards') != state['summary']['active_cards']:
                        raise ValueError('云端活动卡片数与快照不一致。')
                    if verified.get('cards_snapshot') != state['summary']['card_snapshot']:
                        raise ValueError('云端实际卡片正文与快照不一致。')
                    state['receipts']['verify'] = verified['verification']
                    self.operation('receipt', '--sync-id', run_id, '--fingerprint', state['summary']['data_fingerprint'])
                    self._finish_sync(path, state)
            except Exception as error:
                state.update(status='failed', last_error=str(error))
                save(path, state)
                raise
            return self._summary(state)

    def _finish_sync(self, path, state):
        state.update(status='running', stage='cleanup', last_error=None)
        save(path, state)
        remote = self.operation('status')
        receipt = remote.get('maintenance-sync.json', {})
        if remote.get('service') != 'active':
            raise ValueError('云端服务尚未运行，不能结束本轮同步。')
        if state.get('no_changes'):
            if receipt.get('data_fingerprint') != state['summary']['data_fingerprint']:
                raise ValueError('云端维护回执已改变，不能结束旧批次。')
        else:
            self._expect_generation(receipt, state)
        self._clear_staging(path.parent, remote_parts=not state.get('no_changes'))
        state.update(status='completed', stage='done', completed_at=time.time())
        save(path, state)
        save(self.directory / 'last-maintenance-sync.json', self._summary(state))

    @staticmethod
    def _expect_generation(value, state):
        if (value.get('sync_id'), value.get('data_fingerprint')) != (state['sync_id'], state['summary']['data_fingerprint']):
            raise ValueError('远端已不是本次同步快照，不能续接或报告旧批次完成。')

    def _clear_staging(self, directory, remote_parts=False):
        directory = directory.resolve()
        allowed = (self.directory / 'runs').resolve()
        if directory == allowed or not directory.is_relative_to(allowed):
            raise ValueError('传输暂存目录越界。')
        for name in ('payloads', 'image-index.sqlite3'):
            target = directory / name
            if target.is_symlink() or not target.resolve().is_relative_to(directory):
                raise ValueError('传输暂存目标越界。')
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        if remote_parts:
            script = ("from pathlib import Path; import shutil; "
                      "root=Path('/srv/paa/.sync-parts').resolve(); "
                      f"target=(root/{directory.name!r}).resolve(); "
                      "assert target != root and target.is_relative_to(root); "
                      "shutil.rmtree(target) if target.exists() else None")
            self.remote('python3 -c ' + shlex.quote(script))

    @staticmethod
    def _summary(state):
        return {'sync_id': state['sync_id'], 'status': state['status'], 'stage': state['stage'],
                'images': state['summary']['images'], 'cards': state['summary']['active_cards'],
                'data_fingerprint': state['summary']['data_fingerprint'],
                'no_changes': state.get('no_changes', False), 'receipts': state['receipts'],
                'last_error': state.get('last_error'),
                'next_action': f'cloud resume --run {state["sync_id"]}' if state['status'] == 'failed' else None}

    def deploy(self, source=None):
        source = Path(source or Path(__file__).resolve().parents[1]).resolve()
        dirty = subprocess.check_output(['git', 'status', '--porcelain', '--', 'paa', 'scripts', 'pyproject.toml', 'requirements.lock'], cwd=source, text=True)
        if dirty.strip():
            raise ValueError('先提交本次程序输入，再部署可定位的版本。')
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        from . import __version__
        release = __version__ + '-' + commit[:12]
        local_old = self.directory / 'deployed.json'
        old_record = read(local_old) if local_old.exists() else {}
        if old_record.get('deployed_code_source_commit') == commit:
            verified = self.verify_running()
            if (verified.get('cloud-deployed.json', {}).get('deployed_code_source_commit') == commit
                    and verified.get('code_integrity', {}).get('state') == 'verified'):
                return {'release': release, 'commit': commit, 'no_changes': True,
                        'verification': verified['verification']}
        candidate = '/opt/paa/releases/' + release
        previous = '/opt/paa/releases/before-' + time.strftime('%Y%m%d-%H%M%S')
        entries = {}
        for pattern in ('paa/*.py', 'paa/*.html', 'scripts/*.py', 'pyproject.toml', 'requirements.lock'):
            for path in source.glob(pattern):
                if path.is_symlink() or not path.resolve().is_relative_to(source):
                    raise ValueError('部署文件超出提交源目录。')
                entries[path.relative_to(source).as_posix()] = path.read_text(encoding='utf-8').replace('\r\n', '\n').encode()
        manifest = {'release': release, 'deployed_code_source_commit': commit,
                    'deployed_files_sha256': {n: hashlib.sha256(b).hexdigest() for n, b in entries.items()},
                    'code_hash_normalization': 'UTF-8 LF', 'previous_code': previous}
        entries['deployment-manifest.json'] = json_bytes(manifest)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
        self.remote(f'sudo mkdir -p {shlex.quote(candidate)} && sudo tar -xf - -C {shlex.quote(candidate)}', input_data=archive.getvalue())
        check = "import json,hashlib,pathlib; p=pathlib.Path('.'); m=json.loads((p/'deployment-manifest.json').read_text()); assert all(hashlib.sha256((p/n).read_bytes()).hexdigest()==h for n,h in m['deployed_files_sha256'].items()); import paa.cloud_server; print('candidate_ok')"
        self.remote(f'cd {shlex.quote(candidate)} && {REMOTE_PYTHON} -c {shlex.quote(check)}')
        # Persist recovery pointers before stopping a working deployment.
        if local_old.exists():
            save(self.directory / 'deployed-before.json', read(local_old))
        save(self.directory / 'deployment-pending.json', manifest)
        command = ('set -e\nsudo systemctl stop paa.service\n'
                   f'sudo mv {REMOTE_CODE} {shlex.quote(previous)}\n'
                   f'sudo mv {shlex.quote(candidate)} {REMOTE_CODE}\n'
                   'sudo systemctl start paa.service\n')
        try:
            self.remote(command)
            verification = self.verify_running()
            self.remote(f'sudo cp {REMOTE_CODE}/deployment-manifest.json {REMOTE_WORKSPACE}/.local/cloud-deployed.json')
        except Exception:
            # Candidate faults may roll code back, but never roll data/auth back.
            self.remote('sudo systemctl stop paa.service')
            self._restore(previous, candidate + '-failed')
            self.remote('sudo systemctl start paa.service')
            self.verify_running()
            raise
        manifest = {**old_record, **manifest}
        manifest.update(recorded_date=time.strftime('%Y-%m-%d'), main_workspace=str(self.workspace),
                        rollback={'program_directory': previous}, validation=verification['verification'])
        save(local_old, manifest)
        # Recovery copy comes from exactly the files that were sent, not another worktree.
        runtime = self.directory / 'runtime'
        retained = self.directory / ('runtime-before-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        if any(not p.resolve().is_relative_to(self.directory.resolve()) for p in (runtime, retained)):
            raise ValueError('运行恢复副本路径越界。')
        if runtime.exists():
            runtime.rename(retained)
        for name, data in entries.items():
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return {'release': release, 'commit': commit, 'verification': verification['verification'], 'rollback': previous}

    def rollback(self):
        current = read(self.directory / 'deployed.json')
        previous = current.get('rollback', {}).get('program_directory')
        if not previous or not re.fullmatch(r'/opt/paa/releases/[A-Za-z0-9_.-]+', previous):
            raise ValueError('没有本工具可验证的上一程序位置。')
        retained = '/opt/paa/releases/rollback-from-' + time.strftime('%Y%m%d-%H%M%S')
        before = read(self.directory / 'deployed-before.json')
        self.remote('sudo systemctl stop paa.service')
        self._restore(previous, retained)
        try:
            self.remote('sudo systemctl start paa.service')
            verified = self.verify_running()
        except Exception:
            self.remote('sudo systemctl stop paa.service')
            self._restore(retained, previous)
            self.remote('sudo systemctl start paa.service')
            self.verify_running()
            raise
        before['rollback'] = {'program_directory': retained}
        code_record = {k: before.get(k) for k in ('release', 'deployed_code_source_commit', 'deployed_files_sha256', 'code_hash_normalization')}
        self.remote(f'sudo tee {REMOTE_WORKSPACE}/.local/cloud-deployed.json >/dev/null', input_data=json_bytes(code_record))
        self._recovery_copy()
        save(self.directory / 'deployed.json', before)
        save(self.directory / 'deployed-before.json', current)
        return {'rolled_back': True, 'code': previous, 'data_and_auth_preserved': True,
                'verification': verified['verification']}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['sync', 'resume', 'status', 'verify', 'deploy', 'rollback'])
    parser.add_argument('--run')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--source', type=Path)
    args = parser.parse_args(argv)
    manager = CloudManager()
    if args.action == 'status':
        return manager.status(live=args.live)
    if args.action == 'verify':
        return manager.operation('verify')
    if args.action == 'deploy':
        return manager.deploy(args.source)
    if args.action == 'rollback':
        return manager.rollback()
    if args.action == 'sync':
        try:
            run_id = manager.create_sync()
        except (OSError, ValueError, RuntimeError) as error:
            return {'status': 'failed', 'stage': 'preflight', 'sync_id': None,
                    'last_error': str(error), 'next_action': 'cloud status --live'}
        return _resume_result(manager, run_id)
    if not args.run:
        parser.error('resume 需要 --run。')
    return _resume_result(manager, args.run)


def _resume_result(manager, run_id):
    try:
        return manager.resume(run_id)
    except (OSError, ValueError, RuntimeError):
        return manager._summary(read(manager._path(run_id)))
