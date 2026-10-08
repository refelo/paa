"""Maintenance contract tests with synthetic media and no real provider/SSH."""
import json
import importlib.util
import os
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from paa.automation import MaintenanceRun, scan_images
from paa.cards import CardStore, read, save
from paa.cloud import CloudManager
from paa.courses import CourseStore
from paa.distillation import STATE
from paa.incremental import build_index
from paa.library import LOCAL, SourceError
from paa.note_batch import NoteBatch
from paa.workspace import Workspace
from test_cards import fixture


class Encoder:
    def __init__(self):
        self.count = 0

    def encode(self, *, images=None, text=None):
        values = images or text
        self.count += len(values)
        return np.ones((len(values), 1024), dtype=np.float32)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=LOCAL)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.local = self.root / '.local'
        self.library = self.root / 'sample.library'
        (self.library / 'images').mkdir(parents=True)
        self.inputs = self.root / 'courses-input'
        self.inputs.mkdir()
        self.card_path = self.local / 'data/cards'
        card = fixture()
        card.pop('source_refs')
        card.pop('author')
        card.update(schema_version=2, video_ids=['v_' + 'a' * 16], agent_notes=['领域：摄影'])
        save(self.card_path / 'cards/T1.json', card)
        store = CardStore(self.card_path)
        save(self.card_path / 'qa/source-inventory.json', {'root': str(self.inputs), 'roots': [str(self.inputs)], 'sources': {}})
        save(self.card_path / 'qa/source-scope.json', {'excluded_collections': ['excluded']})
        save(self.card_path / 'video-aliases.json', {'schema_version': 1, 'source_to_video': {}})
        save(self.card_path / STATE, {'schema_version': 2, 'store_id': store.store_id,
            'progress': {}, 'shown': {}, 'drafts': {}, 'batches': {}})
        self.settings = {'library': self.library, 'index_dir': self.local / 'runtime',
                         'card_store': self.card_path, 'allow_preview_to_agent': True,
                         'allow_paid_api': False}
        self.workspace = Workspace(self.settings)
        self.encoder = Encoder()
        for module in ('automation', 'note_batch', 'courses'):
            p = patch('paa.' + module + '.LOCAL', self.local)
            p.start()
            self.addCleanup(p.stop)
        p = patch('paa.automation.PROJECT', self.root)
        p.start()
        self.addCleanup(p.stop)
        self.add_image('A', annotation='【ai生成】\n已完成')
        self.index()

    def add_image(self, item, color='red', annotation=''):
        folder = self.library / 'images' / (item + '.info')
        folder.mkdir(exist_ok=True)
        Image.new('RGB', (20, 20), color).save(folder / 'image.png')
        save(folder / 'metadata.json', {'id': item, 'name': 'image', 'ext': 'png', 'annotation': annotation})

    def index(self, **kwargs):
        return build_index(self.settings, encoder=self.encoder, **kwargs)

    def test_plan_is_readonly_and_content_replacement_is_not_hidden_by_ai(self):
        before = set(self.local.rglob('*'))
        original = scan_images(self.workspace)
        self.assertEqual(original['changes']['notes'], [])
        self.assertEqual(before, set(self.local.rglob('*')))
        self.add_image('A', 'blue', annotation='【ai生成】\n旧图分析')
        changed = scan_images(self.workspace)
        self.assertEqual(changed['changes']['changed'], ['A'])
        self.assertEqual(changed['changes']['notes'], ['A'])

    def test_metadata_and_rename_do_not_reencode(self):
        baseline = scan_images(self.workspace)
        save(self.workspace.images.local / 'maintenance-images.json', baseline)
        path = self.library / 'images/A.info/metadata.json'
        metadata = read(path)
        metadata.update(tags=['喜欢'], star='5', name='renamed')
        (path.parent / 'image.png').rename(path.parent / 'renamed.png')
        save(path, metadata)
        scan = scan_images(self.workspace)
        self.assertEqual(scan['changes']['notes'], [])
        self.assertEqual(scan['changes']['metadata'], ['A'])
        self.assertEqual(scan['changes']['renamed'], ['A'])
        self.index()
        self.assertEqual(self.encoder.count, 1)

    def test_empty_or_broken_source_is_not_deletion(self):
        path = self.library / 'images/A.info/metadata.json'
        path.write_text('{broken')
        with self.assertRaises(ValueError):
            scan_images(self.workspace)
        path.write_text(json.dumps({'id': 'A', 'isDeleted': True}))
        with self.assertRaises(SourceError):
            scan_images(self.workspace)

    def test_frozen_note_batch_cannot_enroll_later_additions_or_replacements(self):
        self.add_image('B')
        scope = scan_images(self.workspace)
        batch = NoteBatch(self.workspace, self.local / 'batch')
        batch.initialize(self.library, ids=['B'], expected_hashes={'B': scope['images']['B']['sha256']})
        self.add_image('C')
        self.assertEqual(batch.state()['scope_ids'], ['B'])
        self.add_image('B', 'blue')
        result = batch.prepare('worker')
        self.assertEqual(result['count'], 0)
        self.assertIn('B', batch.status()['issues'])
        with self.assertRaises(SourceError):
            batch.initialize(self.library, ids=['C'])

    def test_scoped_index_omits_new_arrivals_and_rejects_changed_input(self):
        scope = scan_images(self.workspace)
        self.add_image('B')
        self.index(item_ids=['A'], expected_hashes={'A': scope['images']['A']['sha256']})
        self.assertEqual(self.workspace.images.snapshot()[0]['indexed'], 1)
        self.add_image('A', 'black')
        with self.assertRaises(SourceError):
            self.index(item_ids=['A'], expected_hashes={'A': scope['images']['A']['sha256']})
        self.assertEqual(self.workspace.images.snapshot()[0]['indexed'], 1)

    def test_completed_run_reentry_does_not_repeat_stages(self):
        run = MaintenanceRun.create(self.workspace, cloud=False, include_courses=False)
        def indexed(settings, **kwargs):
            kwargs.pop('allow_online', None)
            return build_index(settings, encoder=self.encoder, **kwargs)
        with patch('paa.incremental.build_index', side_effect=indexed) as indexing, patch.object(self.workspace, 'index_cards', return_value={'ready': True}) as cards:
            self.assertEqual(run.resume()['status'], 'completed')
            run.resume()
            self.assertEqual(indexing.call_count, 1)
            self.assertEqual(cards.call_count, 1)
        self.assertEqual(self.encoder.count, 1)

    def test_run_needs_agent_before_indexes(self):
        self.add_image('B')
        run = MaintenanceRun.create(self.workspace, cloud=False, include_courses=False)
        with patch.object(self.workspace, 'index_cards', side_effect=AssertionError('not before notes')):
            result = run.resume()
        self.assertEqual(result['status'], 'needs_agent')
        self.assertEqual(result['stage'], 'notes')

    def test_course_duplicates_and_unavailable_roots(self):
        text = '1\n00:00:00,000 --> 00:00:02,000\n窗边逆光保留轮廓\n'
        (self.inputs / 'one.srt').write_text(text, encoding='utf-8')
        (self.inputs / 'copy.srt').write_text(text, encoding='utf-8')
        store = CourseStore(self.workspace, self.local)
        store.roots(self.inputs, collection='test')
        self.assertEqual(len(store.scan()['pending']), 1)
        self.inputs.rename(self.inputs.with_name('unplugged'))
        self.assertEqual(store.scan()['roots'][0]['state'], 'unavailable')
        self.assertEqual(store.scan()['pending'], [])

    def test_course_prepare_does_not_claim_reading_and_partial_review_fails(self):
        source = self.inputs / 'one.srt'
        source.write_text('1\n00:00:00,000 --> 00:00:02,000\n窗边逆光保留轮廓\n', encoding='utf-8')
        store = CourseStore(self.workspace, self.local)
        store.roots(self.inputs, collection='test')
        item = store.scan()['pending'][0]
        prepared = store.prepare(item, device='cpu')
        self.assertEqual(prepared['status'], 'needs_agent')
        self.assertFalse(store.get(item['id'])['full_text_read'])
        review = self.local / 'review.json'
        save(review, {'text_ranges': [[0, 2]], 'viewed_frames': [], 'disposition': 'no_useful_knowledge'})
        with self.assertRaises(ValueError):
            store.review(item['id'], review)
        self.assertFalse(store.audit_scope([item])['complete'])
        self.assertEqual(store.prepare(item, device='cpu')['source_id'], prepared['source_id'])

    def cloud_manager(self):
        directory = self.local / 'cloud'
        directory.mkdir(exist_ok=True)
        save(directory / 'connection.json', {'source_workspace': str(self.root), 'library': str(self.library),
             'host': 'not-real', 'identity_file': 'not-used', 'known_hosts': 'not-used'})
        save(self.local / 'workspace.json', {k: str(v) if isinstance(v, Path) else v for k, v in self.settings.items()})
        (self.local / 'data/user-reference.md').write_text('fixture', encoding='utf-8')
        return CloudManager(self.root)

    def test_revised_course_at_same_original_path_keeps_video_identity(self):
        source = self.inputs / 'one.srt'
        source.write_text('1\n00:00:00,000 --> 00:00:02,000\n旧的说明\n', encoding='utf-8')
        store = CourseStore(self.workspace, self.local)
        store.roots(self.inputs, collection='test')
        first = store.prepare(store.scan()['pending'][0], device='cpu')
        aliases = self.card_path / 'video-aliases.json'
        save(aliases, {'schema_version': 1, 'source_to_video': {first['source_id']: 'v_existing_video'}})
        source.write_text('1\n00:00:00,000 --> 00:00:02,000\n修订后的说明\n', encoding='utf-8')
        second = store.prepare(store.scan()['pending'][0], device='cpu')
        self.assertNotEqual(first['source_id'], second['source_id'])
        self.assertEqual(read(aliases)['source_to_video'][second['source_id']], 'v_existing_video')

    def test_cloud_import_failure_resumes_without_second_upload(self):
        manager = self.cloud_manager()
        run_id = manager.create_sync(scan_images(self.workspace))
        summary = read(manager._path(run_id))['summary']
        generation = {'sync_id': run_id, 'data_fingerprint': summary['data_fingerprint']}
        remote_state = {k: generation for k in ('incoming', 'imported', 'refreshed')}
        with patch('paa.cloud.upload', return_value={'receipt': 'uploaded'}) as uploaded, patch.object(manager, 'operation', return_value=remote_state), patch.object(manager, 'remote', side_effect=RuntimeError('import failure')):
            with self.assertRaises(RuntimeError):
                manager.resume(run_id)
            self.assertEqual(uploaded.call_count, 1)
        self.assertEqual(read(manager._path(run_id))['stage'], 'import')
        verified = {'status': {'eligible': 1, 'indexed': 1, 'active_cards': 1}, 'verification': {'ok': True},
                    'last-sync.json': generation, 'cloud-refresh.json': generation,
                    'maintenance-sync.json': generation, 'service': 'active',
                    'cards_snapshot': summary['card_snapshot']}
        with patch('paa.cloud.upload', side_effect=AssertionError('must not upload again')), patch.object(manager, 'remote', return_value=json.dumps(generation)), patch.object(manager, 'operation', side_effect=lambda action,*args: remote_state if action == 'sync-state' else verified):
            self.assertEqual(manager.resume(run_id)['status'], 'completed')

    def test_identical_cloud_snapshot_is_noop_and_does_not_stop_service(self):
        manager = self.cloud_manager()
        first = manager.create_sync(scan_images(self.workspace))
        second = manager.create_sync(scan_images(self.workspace))
        one, two = read(manager._path(first)), read(manager._path(second))
        self.assertEqual(one['summary']['data_fingerprint'], two['summary']['data_fingerprint'])
        value = {'data_fingerprint': one['summary']['data_fingerprint']}
        save(manager.directory / 'last-maintenance-sync.json', value)
        remote = {'maintenance-sync.json': value, 'service': 'active'}
        with patch.object(manager, 'operation', return_value=remote), patch.object(manager, 'remote', side_effect=AssertionError('no mutating SSH')):
            self.assertTrue(manager.resume(second)['no_changes'])

    def test_cloud_resume_rejects_another_generation_with_equal_counts(self):
        manager = self.cloud_manager()
        run_id = manager.create_sync(scan_images(self.workspace))
        state = read(manager._path(run_id))
        state['stage'] = 'import'
        save(manager._path(run_id), state)
        foreign = {'sync_id': 'other', 'data_fingerprint': 'different', 'images': 1, 'active_cards': 1}
        verified = {'status': {'eligible': 1, 'active_cards': 1}, 'last-sync.json': foreign,
                    'incoming': foreign, 'verification': {'ok': True}}
        with patch.object(manager, 'remote', return_value=json.dumps(foreign)), patch.object(manager, 'operation', return_value=verified):
            with self.assertRaises(ValueError):
                manager.resume(run_id)

    def test_unstaged_course_revision_keeps_default_video_identity(self):
        source = self.inputs / 'one.srt'
        source.write_text('1\n00:00:00,000 --> 00:00:02,000\n旧说明\n', encoding='utf-8')
        store = CourseStore(self.workspace, self.local)
        store.roots(self.inputs, collection='test')
        first = store.prepare(store.scan()['pending'][0], device='cpu')
        inventory = read(self.card_path / 'qa/source-inventory.json')
        expected = 'v_' + inventory['sources'][first['source_id']]['sha256'][:16]
        source.write_text('1\n00:00:00,000 --> 00:00:02,000\n新说明\n', encoding='utf-8')
        second = store.prepare(store.scan()['pending'][0], device='cpu')
        self.assertEqual(read(self.card_path / 'video-aliases.json')['source_to_video'][second['source_id']], expected)

    def test_installed_probe_uses_explicit_data_workspace(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/check_mcp.py'
        with patch.dict(os.environ, {'PAA_WORKSPACE': str(self.root)}):
            spec = importlib.util.spec_from_file_location('probe_test', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.ROOT, path.parents[1])
            with self.assertRaisesRegex(ValueError, 'isolated target'):
                module.installed_config(path.parents[1])

    def test_upload_does_not_trust_unchanged_size_and_mtime(self):
        from paa.cloud_transfer import upload
        from paa.automation import digest_file
        manager = self.cloud_manager()
        folder = self.local / 'transfer'
        folder.mkdir()
        source = self.inputs / 'bytes.bin'
        source.write_bytes(b'AAAA')
        stamp = source.stat()
        old_hash = digest_file(source)
        name = 'library.library/images/A.info/bytes.bin'
        save(folder / 'uploaded.json', {'receipt': 'prior', 'files': {name: [4, stamp.st_mtime_ns]},
                                       'content_hashes': {name: old_hash}})
        source.write_bytes(b'BBBB')
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        class Sink(io.BytesIO):
            def close(self):
                self.value = self.getvalue()
                super().close()
        process = SimpleNamespace(stdin=Sink(), wait=lambda: 0)
        summary = {'images': 1, 'source_files_sha256': {name: digest_file(source)}}
        with patch('paa.cloud_transfer.subprocess.run', return_value=SimpleNamespace(stdout='prior')), patch('paa.cloud_transfer.subprocess.Popen', return_value=process):
            result = upload(manager.config, folder, {name: source}, {}, summary)
        self.assertEqual(result['files_to_upload'], 1)
        self.assertTrue(process.stdin.value.startswith(b'\x1f\x8b'))
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(process.stdin.value), mode='r:gz') as archive:
            self.assertEqual(archive.extractfile(name).read(), b'BBBB')

    def test_empty_transcript_cannot_claim_unrecorded_visual_knowledge(self):
        store = CourseStore(self.workspace, self.local)
        empty = self.inputs / 'silent.srt'
        empty.write_text('', encoding='utf-8')
        item = {'id': 'Mempty', 'path': str(empty), 'sha256': 'fixture'}
        from paa.automation import digest_file
        state = store.state()
        state['media'][item['id']] = {**item, 'srt_path': str(empty), 'srt_sha256': digest_file(empty),
             'source_id': None, 'status': 'prepared', 'review': {'disposition': 'distilled'}}
        save(store.path, state)
        save(self.local / 'data/compilation-receipt.json', {'cards_token': self.workspace.cards().snapshot()[2]})
        self.assertFalse(store.audit_scope([item])['complete'])

    def test_code_recovery_after_current_was_already_moved(self):
        from paa.cloud import restore_code
        root = self.root / 'code'
        previous = root / 'releases/old'
        previous.mkdir(parents=True)
        (previous / 'version').write_text('old')
        restore_code(root, root / 'current', previous, root / 'releases/failed')
        self.assertEqual((root / 'current/version').read_text(), 'old')
        self.assertFalse((root / 'releases/failed').exists())

    def test_failed_rollback_does_not_publish_success_record(self):
        manager = self.cloud_manager()
        old = {'release': 'old', 'rollback': {'program_directory': '/opt/paa/releases/new'}}
        current = {'release': 'new', 'rollback': {'program_directory': '/opt/paa/releases/old'}}
        save(manager.directory / 'deployed.json', current)
        save(manager.directory / 'deployed-before.json', old)
        with patch.object(manager, 'remote'), patch.object(manager, '_restore') as restore, patch.object(manager, 'verify_running', side_effect=[RuntimeError('old cannot start'), {'verification': {'recovered': True}}]):
            with self.assertRaises(RuntimeError):
                manager.rollback()
            self.assertEqual(restore.call_count, 2)
        self.assertEqual(read(manager.directory / 'deployed.json')['release'], 'new')

    def test_verify_cli_target_reaches_dispatch(self):
        from paa.__main__ import main
        with patch('sys.argv', ['paa', 'verify', '--target', 'all']), patch('paa.library.read_settings', return_value=self.settings), patch('paa.verification.main', return_value={'ok': True}) as dispatch, redirect_stdout(io.StringIO()):
            main()
        self.assertEqual(dispatch.call_args.args[0], ['--target', 'all'])

    def test_diagnostics_reports_missing_source_without_calling_it_empty(self):
        from paa.automation import diagnostics
        self.library.rename(self.library.with_name('unplugged.library'))
        result = diagnostics(self.workspace)
        self.assertEqual(result['images']['state'], 'unavailable')
        self.assertFalse(result['images']['content_verified'])

    def test_silent_video_uses_visual_source_and_blocks_unmerged_candidates(self):
        try:
            import av
        except ImportError:
            self.skipTest('optional media dependency is not installed')
        source = self.inputs / 'silent.mp4'
        with av.open(str(source), 'w') as output:
            stream = output.add_stream('mpeg4', rate=5)
            stream.width, stream.height, stream.pix_fmt = 32, 24, 'yuv420p'
            for i in range(10):
                frame = av.VideoFrame.from_image(Image.new('RGB', (32, 24), 'blue'))
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
        store = CourseStore(self.workspace, self.local)
        store.roots(self.inputs, collection='test', owner='test teacher')
        item = store.scan()['pending'][0]
        prepared = store.prepare(item, device='cpu', frame_step=1)
        sid = prepared['source_id']
        self.assertTrue(sid.startswith('V'))
        self.assertEqual(store.text(item['id'])['total_chars'], 0)
        frames = read(prepared['frames'])
        review = self.local / 'review.json'
        save(review, {'text_ranges': [], 'viewed_frames': frames['sheets'], 'disposition': 'distilled'})
        store.review(item['id'], review)
        state = read(self.card_path / STATE)
        state['progress'][sid] = 'distilled'
        video = read(self.card_path / 'video-aliases.json')['source_to_video'][sid]
        state['drafts']['unfinished'] = {'state': 'candidate', 'video_ids': [video]}
        save(self.card_path / STATE, state)
        result = store.audit_scope([item])
        self.assertFalse(result['complete'])
        self.assertTrue(any(p['reason'] == 'unmerged_candidates' for p in result['problems']))

    def test_import_recovers_after_card_directory_was_published(self):
        from paa.cloud_importer import import_snapshot
        manager = self.cloud_manager()
        run_id = manager.create_sync(scan_images(self.workspace))
        path = manager._path(run_id)
        state = read(path)
        server = self.root / 'server'
        for name, source in state['files'].items():
            target = server / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for name in state['payloads']:
            target = server / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path.parent / 'payloads' / name, target)
        stale = read(server / 'workspace/.local/import/cards/cards/T1.json')
        stale['id'] = 'STALE'
        save(server / 'workspace/.local/import/cards/cards/STALE.json', stale)
        def interrupted(target, value):
            if value.get('phase') == 'cards_published':
                raise RuntimeError('power loss after card rename')
            save(target, value)
        with patch('paa.cloud_importer.save', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                import_snapshot(server, run_id, state['summary']['data_fingerprint'])
        result = import_snapshot(server, run_id, state['summary']['data_fingerprint'])
        self.assertEqual(result['sync_id'], run_id)
        self.assertEqual(result['reused_vectors'], 1)
        self.assertFalse((server / 'workspace/.local/data/cards/cards/STALE.json').exists())
        self.assertEqual(import_snapshot(server), result)

    def test_deployment_preserves_existing_skill_identity_record(self):
        manager = self.cloud_manager()
        save(manager.directory / 'deployed.json', {'chatgpt_skill': {'id': 'keep-same-skill'}, 'custom_record': 'retain'})
        source = Path(__file__).resolve().parents[1]
        def git(args, **kwargs):
            return '' if 'status' in args else 'a' * 40 + '\n'
        with patch('paa.cloud.subprocess.check_output', side_effect=git), patch.object(manager, 'remote', return_value=''), patch.object(manager, 'verify_running', return_value={'verification': {'ok': True}}):
            manager.deploy(source)
        result = read(manager.directory / 'deployed.json')
        self.assertEqual(result['chatgpt_skill']['id'], 'keep-same-skill')
        self.assertEqual(result['custom_record'], 'retain')

    def test_offline_zero_budget_noop_does_not_load_credentials(self):
        self.workspace.settings['budget_usd'] = 0
        self.workspace.cards().vector_build('voyage', 'voyage-multimodal-3.5', lambda texts, role: [[1] * 1024 for _ in texts])
        run = MaintenanceRun.create(self.workspace, cloud=False, include_courses=False)
        with patch('paa.voyage.load_key', side_effect=AssertionError('offline reuse must not read credentials')):
            result = run.resume()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['receipts']['images']['encoded'], 0)
        self.assertEqual(result['receipts']['cards']['encoded'], 0)

    def test_local_default_does_not_construct_cloud_manager(self):
        self.workspace.cards().vector_build('voyage', 'voyage-multimodal-3.5', lambda texts, role: [[1] * 1024 for _ in texts])
        with patch('paa.cloud.CloudManager', side_effect=AssertionError('local run contacted cloud')):
            run = MaintenanceRun.create(self.workspace, include_courses=False)
            result = run.resume()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['receipts']['cloud']['status'], 'not_requested')

    def test_explicit_cloud_requires_configuration_before_creating_run(self):
        from paa.automation import main
        result = main(['run', '--cloud', '--no-courses'], self.workspace)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['stage'], 'preflight')
        self.assertIsNone(result['run_id'])
        self.assertIn('connection.json', result['error'])

    def test_cli_failure_returns_run_identity_and_resume_action(self):
        from paa.__main__ import main
        self.add_image('B', annotation='【ai生成】existing note')
        output = io.StringIO()
        with patch('sys.argv', ['paa', 'maintain', 'run', '--local-only', '--no-courses']), patch('paa.library.read_settings', return_value=self.settings), redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                main()
        self.assertEqual(stopped.exception.code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['run_id'])
        self.assertIn(result['run_id'], result['next_action'])

    def test_upload_checkpoints_successful_chunks_before_connection_reset(self):
        from paa.cloud_transfer import upload
        from paa.automation import digest_file
        manager = self.cloud_manager()
        folder = self.local / 'transfer-chunks'
        folder.mkdir()
        files = {}
        for name in ('A', 'B'):
            path = self.inputs / name
            path.write_bytes(name.encode() * 4)
            files['workspace/' + name] = path
        summary = {'sync_id': 'fixture', 'source_files_sha256': {n:digest_file(p) for n,p in files.items()}}
        remote = {'receipt': ''}
        def command(args, **kwargs):
            if kwargs.get('input'):
                remote['receipt'] = kwargs['input'].decode()
            return SimpleNamespace(stdout=remote['receipt'])
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', side_effect=[50, RuntimeError('connection reset')]):
            with self.assertRaises(RuntimeError):
                upload(manager.config, folder, files, {}, summary, chunk_bytes=4)
        checkpoint = read(folder / 'uploaded.json')
        self.assertIn('workspace/A', checkpoint['content_hashes'])
        self.assertNotIn('workspace/B', checkpoint['content_hashes'])
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', return_value=50) as sent:
            result = upload(manager.config, folder, files, {}, summary, chunk_bytes=4)
        self.assertEqual(result['files_to_upload'], 1)
        self.assertEqual(sent.call_args.args[1][0][0], 'workspace/B')

    def test_cloud_cli_failure_keeps_sync_identity(self):
        from paa.cloud import main
        manager = self.cloud_manager()
        run_id = manager.create_sync(scan_images(self.workspace))
        state = read(manager._path(run_id))
        state.update(status='failed', last_error='connection reset')
        save(manager._path(run_id), state)
        with patch('paa.cloud.CloudManager', return_value=manager), patch.object(manager, 'resume', side_effect=RuntimeError('connection reset')):
            result = main(['resume', '--run', run_id])
        self.assertEqual(result['sync_id'], run_id)
        self.assertIn(run_id, result['next_action'])
        self.assertEqual(manager.status()['unfinished_syncs'][0]['sync_id'], run_id)

    def test_large_card_payload_resumes_by_parts(self):
        from paa.cloud_transfer import upload
        folder = self.local / 'large-payload'
        folder.mkdir()
        config = self.cloud_manager().config
        remote = {'receipt': ''}
        def command(args, **kwargs):
            if kwargs.get('input'):
                remote['receipt'] = kwargs['input'].decode()
            return SimpleNamespace(stdout=remote['receipt'])
        name = 'workspace/cards/.index/voyage.json'
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', side_effect=[4, RuntimeError('reset')]):
            with self.assertRaises(RuntimeError):
                upload(config, folder, {}, {name: b'aaaabbbbcccc'}, {'sync_id': 'cards'}, chunk_bytes=4)
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', return_value=4) as sent, patch('paa.cloud_transfer._assemble_parts') as assemble:
            upload(config, folder, {}, {name: b'aaaabbbbcccc'}, {'sync_id': 'cards'}, chunk_bytes=4)
        self.assertEqual(sent.call_count, 2)
        self.assertTrue(sent.call_args_list[0].args[1][0][0].endswith('/000001'))
        self.assertEqual(assemble.call_args.args[1][0]['name'], name)

    def test_upload_recovers_remote_checkpoint_after_lost_ack(self):
        from paa.cloud_transfer import upload
        folder = self.local / 'lost-ack'
        folder.mkdir()
        remote = {'receipt': ''}
        calls = 0
        def command(args, **kwargs):
            nonlocal calls
            if kwargs.get('input'):
                remote['receipt'] = kwargs['input'].decode()
                calls += 1
                if calls == 2:
                    raise RuntimeError('checkpoint committed but ACK lost')
            return SimpleNamespace(stdout=remote['receipt'])
        payloads = {'workspace/A': b'AAAA', 'workspace/B': b'BBBB', 'workspace/C': b'CCCC'}
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', return_value=4):
            with self.assertRaises(RuntimeError):
                upload(self.cloud_manager().config, folder, {}, payloads, {}, chunk_bytes=4)
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', return_value=4) as sent:
            upload(self.cloud_manager().config, folder, {}, payloads, {}, chunk_bytes=4)
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(sent.call_args.args[1], [('workspace/C', b'CCCC')])

    def test_partial_overwrite_invalidates_old_hash_before_transfer(self):
        from paa.cloud_transfer import upload
        folder = self.local / 'overwrite'
        folder.mkdir()
        remote = {'receipt': ''}
        content = {}
        def command(args, **kwargs):
            if kwargs.get('input'):
                remote['receipt'] = kwargs['input'].decode()
            return SimpleNamespace(stdout=remote['receipt'])
        def send(config, entries):
            content.update(entries)
            return 4
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', side_effect=send):
            upload(self.cloud_manager().config, folder, {}, {'workspace/A': b'AAAA'}, {})
        def partial(config, entries):
            content['workspace/A'] = b'BB'
            raise RuntimeError('partial overwrite')
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', side_effect=partial):
            with self.assertRaises(RuntimeError):
                upload(self.cloud_manager().config, folder, {}, {'workspace/A': b'BBBB'}, {})
        with patch('paa.cloud_transfer.subprocess.run', side_effect=command), patch('paa.cloud_transfer._send_archive', side_effect=send):
            result = upload(self.cloud_manager().config, folder, {}, {'workspace/A': b'AAAA'}, {})
        self.assertEqual(result['payloads_to_upload'], 1)
        self.assertEqual(content['workspace/A'], b'AAAA')

    def test_cleanup_resume_cannot_overwrite_new_generation_receipt(self):
        manager = self.cloud_manager()
        run_id = manager.create_sync(scan_images(self.workspace))
        state = read(manager._path(run_id))
        state.update(stage='cleanup', status='failed')
        save(manager._path(run_id), state)
        new = {'sync_id': 'new', 'data_fingerprint': 'new'}
        save(manager.directory / 'last-maintenance-sync.json', new)
        with patch.object(manager, 'operation', return_value={'service': 'active', 'maintenance-sync.json': new}), patch.object(manager, '_clear_staging') as clear:
            with self.assertRaises(ValueError):
                manager.resume(run_id)
            clear.assert_not_called()
        self.assertEqual(read(manager.directory / 'last-maintenance-sync.json'), new)


if __name__ == '__main__':
    unittest.main()
