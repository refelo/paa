from contextlib import ExitStack
from pathlib import Path
import re
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from paa.cards import CardError, CardIndexUnavailable, CardStore, read, save
from paa.installation import LocalInstallation, RECEIPT, SKILL, STORE, mcp_config
from paa.library import read_settings
from paa.workspace import Workspace
from install_fixture import source_fixture

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / 'isolated target'
        self.source = source_fixture(Path(self.tmp.name) / 'source', ROOT)
        self.installer = LocalInstallation(self.source, self.target)
        self.runtime = {'python': str(self.target / '.local/app/test/python.exe'), 'source_kind': 'test-double'}
        health = patch('paa.installation.verify_runtime', return_value={'ok': True})
        health.start()
        self.addCleanup(health.stop)

    def install(self, choice=None):
        return self.installer.install(self.runtime, aesthetics=choice)

    def store(self):
        return CardStore(self.target / STORE, initialize=False)

    def settings(self):
        with patch('paa.library.LOCAL', self.target / '.local'):
            return read_settings(self.target / '.local/workspace.json')

    def test_unanswered_imports_core_only_without_reading_optional_files(self):
        original = Path.read_bytes
        def checked(path):
            if 'optional-aesthetics' in path.parts:
                raise AssertionError('unanswered choice read optional content')
            return original(path)
        with patch.object(Path, 'read_bytes', checked):
            result = self.install()
        self.assertEqual(result['aesthetics'], 'unanswered')
        self.assertEqual(len(self.store().snapshot()[0]), 4)
        self.assertFalse((self.target / SKILL / 'references/knowledge/optional-aesthetics/compilation.md').exists())
        workspace = Workspace(self.settings())
        self.assertEqual(workspace.status()['active_cards'], 4)
        self.assertIsNone(workspace.status()['library'])
        self.assertEqual(workspace.search_cards('窗光 柔和')['candidates'][0]['id'], 'PT004')
        result = workspace.search_cards('窗光 柔和', route='hybrid', allow_online=False)
        self.assertEqual(result['effective_route'], 'keyword')
        self.assertTrue(result['warnings'])
        with self.assertRaises(CardError):
            workspace.get_card('PA001')
        with patch('paa.library.LOCAL', self.target / '.local'):
            self.assertFalse(workspace.get_user_reference()['available'])

    def test_disable_removes_seed_from_all_routes_but_preserves_user_cards_and_edits(self):
        self.install('enable')
        store = self.store()
        self.assertEqual(len(store.snapshot()[0]), 6)
        card = store.get('PA001')
        store.update('PA001', {'problem': 'my edited optional seed'}, card['etag'])
        user = {**store.snapshot()[0]['PA002'], 'id': 'MY_VIEW', 'problem': 'my own independent view'}
        save(store.cards_dir / 'MY_VIEW.json', user)
        store.vector_build('voyage', 'fixture', lambda texts, role: [[1, 2] for _ in texts])
        result = self.install('disable')
        self.assertTrue(result['warnings'])
        store = self.store()
        self.assertEqual(len(store.snapshot()[0]), 5)
        self.assertIn('MY_VIEW', store.snapshot()[0])
        self.assertNotIn('PA001', store.snapshot()[0])
        with self.assertRaises(CardError):
            store.get('PA001', include_inactive=True)
        self.assertFalse(any(row['id'].startswith('PA') for row in store.keyword('个人审美 蓝调', 20)))
        with self.assertRaises(CardIndexUnavailable):
            store.prepare_vector('voyage', 'fixture')
        encoded = []
        store.vector_build('voyage', 'fixture', lambda texts, role: (encoded.extend(texts) or [[1, 2] for _ in texts]))
        self.assertNotIn('PA001', read(store.vector_path('voyage'))['ids'])
        self.assertEqual(len(store.vector([1, 2], 'voyage', 'fixture', 20)), 5)
        self.assertEqual(read(self.target / '.local/install/seed-archive/PA001.json')['card']['problem'], 'my edited optional seed')
        self.assertFalse((self.target / SKILL / 'references/knowledge/optional-aesthetics/compilation.md').exists())
        self.install()
        self.assertEqual(read(self.target / RECEIPT)['aesthetics'], 'disable')
        self.install('enable')
        self.assertEqual(self.store().get('PA001')['card']['problem'], 'my edited optional seed')

    def test_repeat_upgrade_preserves_choice_user_edits_and_other_settings(self):
        self.install('disable')
        store = self.store()
        old = store.get('PT004')
        store.update('PT004', {'problem': 'user-edited technique'}, old['etag'])
        user = {**store.snapshot()[0]['PT001'], 'id': 'USER001'}
        save(store.cards_dir / 'USER001.json', user)
        settings_path = self.target / '.local/workspace.json'
        settings = read(settings_path)
        settings['budget_usd'] = 0.25
        settings['query_online_default'] = True
        save(settings_path, settings)
        config_path = self.target / '.codex/config.toml'
        config_path.write_text('model="keep"\n[features]\napps=true\n' + config_path.read_text(encoding='utf-8'), encoding='utf-8')
        self.install()
        self.assertEqual(self.store().get('PT004')['card']['problem'], 'user-edited technique')
        self.assertEqual(len(self.store().snapshot()[0]), 5)
        self.assertEqual(read(settings_path)['budget_usd'], 0.25)
        self.assertTrue(read(settings_path)['query_online_default'])
        config = tomllib.loads(config_path.read_text(encoding='utf-8'))
        self.assertEqual(config['model'], 'keep')
        self.assertEqual(config['features'], {'apps': True})
        before = {p.relative_to(self.target): p.read_bytes() for p in self.target.rglob('*') if p.is_file()}
        self.install()
        after = {p.relative_to(self.target): p.read_bytes() for p in self.target.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_existing_paa_conflicts_do_not_write_config_or_data(self):
        config = self.target / '.codex/config.toml'
        config.parent.mkdir(parents=True)
        original = 'model="keep"\n[mcp_servers.paa]\ncommand="private"\n'
        config.write_text(original, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.install('disable')
        self.assertEqual(config.read_text(encoding='utf-8'), original)
        self.assertFalse((self.target / STORE).exists())

    def test_existing_skill_and_user_seed_id_conflicts(self):
        skill = self.target / '.agents/skills/paa/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('private')
        with self.assertRaisesRegex(ValueError, 'Skill conflict'):
            self.install()
        self.assertEqual(skill.read_text(), 'private')

    def test_enable_refuses_unmanaged_id_collision(self):
        self.install('disable')
        card = {**self.store().snapshot()[0]['PT001'], 'id': 'PA001'}
        save(self.store().cards_dir / 'PA001.json', card)
        before = (self.target / RECEIPT).read_bytes()
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.install('enable')
        self.assertEqual((self.target / RECEIPT).read_bytes(), before)
        self.assertEqual(self.store().get('PA001')['card']['kind'], 'technique')

    def test_interruption_blocks_runtime_and_recovery_preserves_choice(self):
        self.install('enable')
        import paa.installation as module
        original = module.atomic_bytes
        writes = 0
        def interrupted(path, payload):
            nonlocal writes
            writes += 1
            if writes == 4:
                raise OSError('simulated interruption')
            return original(path, payload)
        with patch.object(module, 'atomic_bytes', interrupted):
            with self.assertRaisesRegex(OSError, 'interruption'):
                self.install('disable')
        self.assertTrue(self.installer.pending.exists())
        with self.assertRaisesRegex(ValueError, 'recover'):
            self.settings()
        with self.assertRaisesRegex(ValueError, 'recover'):
            self.install()
        self.assertTrue(self.installer.recover()['recovered'])
        self.assertEqual(len(self.store().snapshot()[0]), 4)

        self.assertEqual(self.settings()['aesthetics'], 'disable')
        self.assertFalse(self.installer.recover()['recovered'])

    def test_uninstall_is_repeatable_and_reinstall_keeps_user_data(self):
        self.install('enable')
        store = self.store()
        before = store.snapshot()[2]
        result = self.installer.uninstall()
        self.assertFalse(result['installed'])
        self.assertNotIn('paa_public', tomllib.loads((self.target / '.codex/config.toml').read_text()).get('mcp_servers', {}))
        self.assertFalse((self.target / SKILL / 'SKILL.md').exists())
        self.assertEqual(self.store().snapshot()[2], before)
        self.assertFalse(self.installer.uninstall()['installed'])
        self.install()
        self.assertEqual(read(self.target / RECEIPT)['aesthetics'], 'enable')
        self.assertEqual(self.store().snapshot()[2], before)

    def test_uninstall_rejects_modified_mcp_and_preserves_edited_skill(self):
        self.install()
        config = self.target / '.codex/config.toml'
        original = config.read_bytes()
        config.write_text('[mcp_servers.paa_public]\ncommand="other"\n')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.installer.uninstall()
        config.write_bytes(original)
        skill = self.target / SKILL / 'SKILL.md'
        skill.write_text('user-edited')
        result = self.installer.uninstall()
        self.assertTrue(result['retained_files'])
        self.assertTrue(any(p.read_text() == 'user-edited' for p in (self.target / '.local/install/disabled-skill').rglob('SKILL.md')))

    def test_rollback_preserves_current_choice_and_cards(self):
        self.install('enable')
        previous = self.runtime
        self.runtime = {**previous, 'python': str(self.target / '.local/app/new/python.exe')}
        self.install('disable')
        before = self.store().snapshot()[2]
        self.installer.rollback()
        self.assertEqual(read(self.target / RECEIPT)['runtime'], previous)
        self.assertEqual(self.settings()['aesthetics'], 'disable')
        self.assertEqual(self.store().snapshot()[2], before)

    def test_unhealthy_upgrade_never_changes_active_installation(self):
        self.install()
        before = (self.target / RECEIPT).read_bytes()
        with patch('paa.installation.verify_runtime', side_effect=ValueError('unhealthy')):
            with self.assertRaisesRegex(ValueError, 'unhealthy'):
                self.installer.install({**self.runtime, 'python': 'new'})
        self.assertEqual((self.target / RECEIPT).read_bytes(), before)

    def test_recovery_refuses_to_overwrite_a_later_edit(self):
        self.install('disable')
        with patch('paa.installation.atomic_bytes', side_effect=OSError('stop')):
            with self.assertRaises(OSError):
                self.install('enable')
        pending = read(self.installer.pending)
        entry = next(e for e in pending['entries'] if e['after'] is not None)
        path = self.target / entry['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'later user edit')
        with self.assertRaisesRegex(ValueError, 'later edit'):
            self.installer.recover()
        self.assertEqual(path.read_bytes(), b'later user edit')

    def test_edited_skill_is_not_silently_overwritten(self):
        self.install('disable')
        skill = self.target / SKILL / 'SKILL.md'
        skill.write_text('user-edited skill', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'was edited'):
            self.install()
        self.assertEqual(skill.read_text(encoding='utf-8'), 'user-edited skill')

    def test_new_package_resource_cannot_overwrite_an_unmanaged_file(self):
        self.install('disable')
        name = SKILL + '/references/user-file.md'
        path = self.target / name
        path.write_bytes(b'user resource')
        resources = {**self.installer.resources(False), name: b'new package resource'}
        with patch.object(self.installer, 'resources', return_value=resources):
            with self.assertRaisesRegex(ValueError, 'Unmanaged resource'):
                self.install()
        self.assertEqual(path.read_bytes(), b'user resource')

    def test_target_and_journal_cannot_escape_through_junction(self):
        self.install()
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaisesRegex(ValueError, 'link'):
                self.installer.preflight()
        with self.assertRaisesRegex(ValueError, 'escaped'):
            from paa.installation import safe_path
            safe_path(self.target, '../elsewhere')

    def test_local_maintenance_default_and_explicit_cloud_preflight(self):
        from paa.automation import main
        from PIL import Image
        library = self.target / 'fixture.library'
        item = library / 'images/A.info'
        item.mkdir(parents=True)
        Image.new('RGB', (12, 12), 'green').save(item / 'sample.png')
        save(item / 'metadata.json', {'id': 'A', 'name': 'sample', 'ext': 'png',
                                      'annotation': '【ai生成】synthetic fixture'})
        self.installer.install(self.runtime, aesthetics='disable', library=library)
        with ExitStack() as stack:
            for module in ('paa.library', 'paa.automation', 'paa.courses', 'paa.maintenance', 'paa.note_batch'):
                stack.enter_context(patch(module + '.LOCAL', self.target / '.local'))
            for module in ('paa.library', 'paa.automation'):
                stack.enter_context(patch(module + '.PROJECT', self.target))
            workspace = Workspace(self.settings())
            stack.enter_context(patch('paa.voyage.load_key', side_effect=AssertionError('credentials')))
            result = main(['run', '--no-courses'], workspace)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['receipts']['cloud']['status'], 'not_requested')
        self.assertEqual(result['receipts']['images']['status'], 'not_requested')
        self.assertFalse((self.target / '.local/runtime/index.sqlite3').exists())

    def test_scoped_config_preserves_other_fields(self):
        original = 'model="keep"\n[mcp_servers.other]\ncommand="keep"\n[mcp_servers.paa_public]\ncommand="old"\n[features]\napps=true\n'
        value = {'command': 'python', 'args': ['-m', 'paa'], 'enabled': True, 'env': {'PAA_WORKSPACE': 'isolated'}}
        changed = mcp_config(original, value)
        parsed = tomllib.loads(changed)
        self.assertEqual(parsed['model'], 'keep')
        self.assertEqual(parsed['mcp_servers']['other'], {'command': 'keep'})
        self.assertEqual(parsed['features'], {'apps': True})
        self.assertEqual(parsed['mcp_servers']['paa_public'], value)
        self.assertNotIn('paa_public', tomllib.loads(mcp_config(changed, None))['mcp_servers'])

    def test_runtime_resources_have_resolvable_links_and_vocabulary(self):
        self.install('disable')
        resources = self.target / SKILL / 'references'
        self.assertTrue((resources / 'docs/note-vocabulary.json').is_file())
        for page in resources.rglob('*.md'):
            for href in re.findall(r'\]\(([^)]+)\)', page.read_text(encoding='utf-8')):
                if '://' not in href and not href.startswith('#'):
                    self.assertTrue((page.parent / href.split('#')[0]).is_file(), (page, href))

    def test_upgrade_updates_unmodified_seeds_and_preserves_deleted_ones(self):
        self.install('disable')
        version, cards, packages = self.installer.seeds(False)
        cards['PT001'] = {**cards['PT001'], 'problem': 'updated package content'}
        (self.store().cards_dir / 'PT002.json').unlink()
        with patch.object(self.installer, 'seeds', return_value=(version + '-update', cards, packages)):
            self.install()
        self.assertEqual(self.store().get('PT001')['card']['problem'], 'updated package content')
        self.assertNotIn('PT002', self.store().snapshot()[0])

    def test_reenable_uses_new_seed_version_unless_archived_seed_was_edited(self):
        self.install('enable')
        self.install('disable')
        version, cards, packages = self.installer.seeds(True)
        cards['PA001'] = {**cards['PA001'], 'problem': 'upgraded optional seed'}
        with patch.object(self.installer, 'seeds', return_value=(version + '-update', cards, packages)):
            self.install('enable')
        self.assertEqual(self.store().get('PA001')['card']['problem'], 'upgraded optional seed')


if __name__ == '__main__':
    unittest.main()
