"""Explicit local installation targets, managed seeds and recoverable updates."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import uuid

from .cards import CardStore, fingerprint, read, save, validate
from .locking import file_lock

SERVER = 'paa_public'
SKILL = '.agents/skills/paa-public'
RECEIPT = '.local/install/receipt.json'
PENDING = '.local/install/pending.json'
STORE = '.local/data/cards'


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def safe_path(root, relative):
    """Do not traverse symlinks or Windows junctions, even within the target."""
    root = Path(root).absolute()
    path = root / relative
    if not path.is_relative_to(root) or '..' in path.parts:
        raise ValueError('Installation path escaped its explicit target')
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError(f'Installation path contains a link: {part}')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Installation path escaped its explicit target')
    return path


def atomic_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def mcp_config(original, value):
    before = tomllib.loads(original)
    text = re.sub(r'(?m)^\[mcp_servers\.paa_public(?:\.[^\]\n]+)?\][^\n]*\n(?:(?!^\[).*(?:\n|$))*', '', original).rstrip() + '\n'
    if value is not None:
        text += '\n[mcp_servers.paa_public]\n'
        for key, item in value.items():
            if key != 'env':
                text += key + ' = ' + json.dumps(item, ensure_ascii=False) + '\n'
        text += '\n[mcp_servers.paa_public.env]\n'
        for key, item in value.get('env', {}).items():
            text += key + ' = ' + json.dumps(item, ensure_ascii=False) + '\n'
    after = tomllib.loads(text)
    for obj in (before, after):
        obj.get('mcp_servers', {}).pop(SERVER, None)
        if obj.get('mcp_servers') == {}:
            obj.pop('mcp_servers', None)
    if before != after:
        raise ValueError('Refusing to change unrelated Codex configuration')
    if value is not None and tomllib.loads(text)['mcp_servers'][SERVER] != value:
        raise ValueError('Unsupported Codex table layout; configuration was not changed')
    return text


def verify_runtime(runtime, target):
    python = safe_path(target, Path(runtime['python']).relative_to(target))
    if not python.is_file():
        raise ValueError('Installed runtime is missing')
    env = dict(os.environ, PAA_WORKSPACE=str(target))
    env.pop('PYTHONPATH', None)
    code = ('import json, paa, mcp, numpy, PIL; from paa.server import create_server; '
            'print(json.dumps({"module": paa.__file__, "version": paa.__version__}))')
    result = subprocess.run([str(python), '-I', '-X', 'utf8', '-c', code], cwd=target,
                            env=env, capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
    health = json.loads(result.stdout)
    if not Path(health['module']).resolve().is_relative_to(python.parent.parent.resolve()):
        raise ValueError('Runtime imported code outside its virtual environment')
    return health


def server_config(runtime, target):
    return {'command': runtime['python'], 'args': ['-I', '-X', 'utf8', '-u', '-m', 'paa', 'serve'],
            'cwd': str(target), 'enabled': True, 'startup_timeout_sec': 30, 'tool_timeout_sec': 180,
            'env': {'PAA_WORKSPACE': str(target), 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1',
                    'PAA_RELEASE': runtime.get('source_sha256', 'development')[:20]}}


def selected_library(library, target):
    if library is None:
        return None
    selected = Path(library).absolute()
    safe_path(selected, '.')
    if selected.suffix.lower() != '.library' or not (selected / 'images').is_dir():
        raise ValueError('Explicit Eagle library is not readable')
    if target.is_relative_to(selected):
        raise ValueError('Installation cannot write inside its source library')
    return selected


class LocalInstallation:
    def __init__(self, source, target):
        self.source = Path(source).resolve(strict=True)
        self.target = Path(target).absolute()
        safe_path(self.target, '.')
        if any(path.suffix.lower() == '.library' for path in (self.target, *self.target.parents)):
            raise ValueError('A project cannot be installed inside an Eagle library')
        if (self.target == Path.home().absolute() or self.target == Path(self.target.anchor)
                or any(self.target.is_relative_to(Path.home() / name) for name in ('.codex', '.agents'))):
            raise ValueError('Choose a project directory, not a global configuration directory')
        if self.target != self.source and self.source.is_relative_to(self.target):
            raise ValueError('Use a separate installation target')
        self.pending = safe_path(self.target, PENDING)
        self.receipt_path = safe_path(self.target, RECEIPT)

    def receipt(self):
        if not self.receipt_path.exists():
            return None
        receipt = read(self.receipt_path)
        if receipt.get('schema_version') != 1 or receipt.get('target') != str(self.target):
            raise ValueError('Installation receipt belongs to another target')
        return receipt

    def preflight(self, *, library=None):
        selected_library(library, self.target)
        if self.pending.exists():
            raise ValueError('Interrupted installation; run recover before continuing')
        old = self.receipt()
        config = safe_path(self.target, '.codex/config.toml')
        original = config.read_bytes().decode('utf-8') if config.exists() else ''
        servers = tomllib.loads(original).get('mcp_servers', {})
        if 'paa' in servers or (SERVER in servers and (not old or servers[SERVER] != old['mcp'])):
            raise ValueError('Existing PAA MCP conflict in the explicit target; left unchanged')
        for skill in ('.agents/skills/paa', '.codex/skills/paa', '.codex/skills/paa-public'):
            if safe_path(self.target, skill).exists():
                raise ValueError('Existing PAA Skill conflict in the explicit target')
        if not old:
            for name in (SKILL, '.local/workspace.json', STORE):
                if safe_path(self.target, name).exists():
                    raise ValueError(f'Unmanaged installation data already exists: {name}')
        return old, original

    def seeds(self, enabled):
        manifest = read(safe_path(self.source, 'knowledge/manifest.json'))
        cards, packages = {}, {}
        for row in manifest['cards']:
            package = 'core' if row['kind'] == 'technique' else 'optional-aesthetics'
            if package == 'optional-aesthetics' and not enabled:
                continue
            expected = f"knowledge/{package}/cards/{row['id']}.json"
            if row['path'] != expected or row['id'] in cards:
                raise ValueError('Invalid or duplicate seed path')
            payload = safe_path(self.source, expected).read_bytes()
            if digest(payload) != row['sha256']:
                raise ValueError(f"Seed checksum mismatch: {row['id']}")
            card = json.loads(payload)
            validate(card)
            if card['id'] != row['id'] or card['kind'] != row['kind']:
                raise ValueError('Seed identity mismatch')
            cards[card['id']] = card
            packages[card['id']] = package
        return manifest['version'], cards, packages

    def resources(self, enabled):
        # Copy the method source once, preserving relative links inside the bundle.
        result = {}
        for folder in ('docs', 'examples'):
            for path in sorted((self.source / folder).rglob('*')):
                if path.suffix not in ('.md', '.json'):
                    continue
                relative = path.relative_to(self.source).as_posix()
                result[f'{SKILL}/references/{relative}'] = safe_path(self.source, relative).read_bytes()
        result[f'{SKILL}/references/PROJECT_STATUS.md'] = (
            '# 安装资源快照\n\n本目录只提供当前选择启用的方法资源；开发交接状态由源仓库 PROJECT_STATUS.md 管理。'
            '安装记录在本实例 .local/install/receipt.json，实际 MCP 范围以 library_status 为准。\n'
        ).encode('utf-8')
        for package in ('core', 'optional-aesthetics') if enabled else ('core',):
            name = f'knowledge/{package}/compilation.md'
            text = safe_path(self.source, name).read_text(encoding='utf-8')
            text = re.sub(r'\[([^\]]+)\]\(cards/([A-Za-z0-9_-]+)\.json\)', r'\1（卡片 ID：`\2`）', text)
            result[f'{SKILL}/references/{name}'] = text.encode('utf-8')
        content = safe_path(self.source, 'integrations/codex/paa/SKILL.md').read_text(encoding='utf-8')
        content = content.replace('name: paa\n', 'name: paa-public\n', 1)
        content += ('\n## 当前安装\n\n'
                    f'本实例工作区：`{self.target}`。仅使用 `paa_public` MCP；先以 library_status 核对工作区。\n'
                    '方法与资源从 [资源入口](references/INDEX.md) 按需读取。\n')
        result[f'{SKILL}/SKILL.md'] = content.encode('utf-8')
        index = ('# 本实例资料\n\n'
                 '汇编中的卡片 ID 用 get_card 读取当前正文；不保留另一份静态卡片副本。\n\n'
                 '- [备注方法](docs/note-writing.md)\n'
                 '- [卡片方法](docs/card-maintenance.md)\n'
                 '- [基础技巧](knowledge/core/compilation.md)\n'
                 '- [使用示例](examples/requests.md)\n')
        if enabled:
            index += '- [作者审美观点，仅作主体明确的参考](knowledge/optional-aesthetics/compilation.md)\n'
        result[f'{SKILL}/references/INDEX.md'] = index.encode('utf-8')
        return result

    def install(self, runtime, *, aesthetics=None, library=None, allow_preview=False):
        if aesthetics not in (None, 'enable', 'disable'):
            raise ValueError('Aesthetics must be an explicit enable or disable choice')
        self.preflight(library=library)
        with file_lock(safe_path(self.target, '.local/.maintenance.lock')):
            old, original = self.preflight(library=library)
            verify_runtime(runtime, self.target)
            choice = aesthetics if aesthetics is not None else (old or {}).get('aesthetics', 'unanswered')
            enabled = choice == 'enable'
            version, incoming, packages = self.seeds(enabled)
            previous = (old or {}).get('seeds', {})
            changes, managed, warnings = {}, {}, []
            if enabled and 'optional-aesthetics' not in packages.values():
                warnings.append('本版本没有可分发的作者观点卡；已保留启用选择，未增加观点内容。')
            store_root = safe_path(self.target, STORE)
            if old:
                store = CardStore(store_root, initialize=False)
                # Maintenance writers use the same workspace lock; direct card writers
                # also hold the store lock during this complete installation transaction.
                with store.lock():
                    return self._install_locked(runtime, old, original, choice, version, incoming,
                                                packages, previous, changes, managed, warnings,
                                                library, allow_preview)
            return self._install_locked(runtime, old, original, choice, version, incoming,
                                        packages, previous, changes, managed, warnings,
                                        library, allow_preview)

    def _install_locked(self, runtime, old, original, choice, version, incoming, packages,
                        previous, changes, managed, warnings, library, allow_preview):
        store_root = safe_path(self.target, STORE)
        existing, paths = {}, {}
        if old:
            existing, paths, _ = CardStore(store_root, initialize=False).snapshot()
        else:
            changes[f'{STORE}/store.json'] = json_bytes({'schema_version': 1, 'store_id': uuid.uuid4().hex})
        for card_id, info in previous.items():
            if card_id not in incoming and card_id in existing:
                changes[f'.local/install/seed-archive/{card_id}.json'] = json_bytes({'card': existing[card_id], 'seed': info})
                changes[paths[card_id].relative_to(self.target).as_posix()] = None
                if fingerprint(existing[card_id]) != info['fingerprint']:
                    warnings.append(f'{card_id}: edited seed preserved outside runtime in seed-archive')
        for card_id, card in incoming.items():
            current = existing.get(card_id)
            info = previous.get(card_id)
            if current is not None and info is None:
                raise ValueError(f'User card ID conflicts with package seed: {card_id}')
            target = paths[card_id].relative_to(self.target).as_posix() if current else f'{STORE}/cards/{card_id}.json'
            archived = safe_path(self.target, f'.local/install/seed-archive/{card_id}.json')
            if current is not None and fingerprint(current) != info['fingerprint']:
                managed[card_id] = info
                warnings.append(f'{card_id}: user edit preserved')
                continue
            if current is None and info is not None:
                # A user-deleted managed card is not silently resurrected by an upgrade.
                managed[card_id] = info
                warnings.append(f'{card_id}: user deletion preserved')
                continue
            if current is None and archived.exists():
                archive = read(archived)
                restored = archive['card']
                validate(restored)
                if restored['id'] != card_id:
                    raise ValueError('Archived seed identity mismatch')
                edited = fingerprint(restored) != archive['seed']['fingerprint']
                changes[target] = json_bytes(restored if edited else card)
                managed[card_id] = archive['seed'] if edited else {'fingerprint': fingerprint(card), 'package': packages[card_id]}
            else:
                changes[target] = json_bytes(card)
                managed[card_id] = {'fingerprint': fingerprint(card), 'package': packages[card_id]}
        resources = self.resources(choice == 'enable')
        for name in resources:
            if name not in (old or {}).get('resources', {}) and safe_path(self.target, name).exists():
                raise ValueError(f'Unmanaged resource conflicts with package update: {name}')
        for name, checksum in (old or {}).get('resources', {}).items():
            path = safe_path(self.target, name)
            if path.exists() and digest(path.read_bytes()) != checksum:
                if '/optional-aesthetics/' not in name or choice == 'enable':
                    raise ValueError(f'Managed Skill resource was edited; preserve and resolve it first: {name}')
                warnings.append(f'{name}: edited optional resource retained in transaction backup')
            if name not in resources:
                changes[name] = None
        changes.update(resources)
        settings_path = safe_path(self.target, '.local/workspace.json')
        settings = read(settings_path) if old else {
            'library': None, 'card_store': str(store_root),
            'index_dir': str(self.target / '.local/runtime'),
            'embedding_model': 'voyage-multimodal-3.5', 'query_online_default': False,
            'allow_paid_api': False, 'budget_usd': 0, 'allow_preview_to_agent': False,
            'maintenance_vectors': False,
        }
        if Path(settings['card_store']) != store_root:
            raise ValueError('Managed card store path changed; refusing to manage another store')
        if library is not None:
            selected = selected_library(library, self.target)
            if old and settings.get('library') and Path(settings['library']) != selected:
                raise ValueError('Library changes need a separate target; old index and notes were preserved')
            settings['library'] = str(selected)
        if allow_preview:
            settings['allow_preview_to_agent'] = True
        settings['aesthetics'] = choice
        mcp = server_config(runtime, self.target)
        changes['.codex/config.toml'] = mcp_config(original, mcp).encode('utf-8')
        changes['.local/workspace.json'] = json_bytes(settings)
        receipt = {'schema_version': 1, 'target': str(self.target), 'aesthetics': choice,
                   'state': 'installed',
                   'seed_version': version, 'seeds': managed, 'runtime': runtime,
                   'resources': {name: digest(payload) for name, payload in resources.items()},
                   'mcp': mcp, 'warnings': warnings}
        if old and old['runtime'] != runtime:
            receipt['previous_runtime'] = old['runtime']
        elif old and 'previous_runtime' in old:
            receipt['previous_runtime'] = old['previous_runtime']
        changes[RECEIPT] = json_bytes(receipt)
        self._commit(changes, expected_config=original)
        return {'installed': True, 'target': str(self.target), 'aesthetics': choice,
                'managed_cards': len(managed), 'warnings': warnings, 'runtime': runtime}

    def _commit(self, changes, *, expected_config=None):
        config = safe_path(self.target, '.codex/config.toml')
        if expected_config is not None:
            current = config.read_bytes().decode('utf-8') if config.exists() else ''
            if current != expected_config:
                raise ValueError('Codex configuration changed during installation; retry')
        entries = []
        def encode(value):
            return base64.b64encode(value).decode('ascii') if value is not None else None
        for name, after in changes.items():
            path = safe_path(self.target, name)
            before = path.read_bytes() if path.exists() else None
            if before != after:
                entries.append({'path': name, 'before': encode(before), 'after': encode(after)})
        if not entries:
            return
        transaction = {'schema_version': 1, 'target': str(self.target), 'id': uuid.uuid4().hex, 'entries': entries}
        save(self.pending, transaction)
        self._finish(transaction)

    def _finish(self, transaction):
        if transaction.get('target') != str(self.target) or transaction.get('schema_version') != 1:
            raise ValueError('Foreign installation recovery journal')
        if not re.fullmatch(r'[a-f0-9]{32}', transaction['id']):
            raise ValueError('Invalid installation transaction ID')
        def decode(value):
            return base64.b64decode(value, validate=True) if value is not None else None
        for entry in transaction['entries']:
            path = safe_path(self.target, entry['path'])
            if not entry['path'].startswith(('.local/', '.codex/', SKILL + '/')) or entry['path'] == PENDING:
                raise ValueError('Invalid installation transaction destination')
            current = path.read_bytes() if path.exists() else None
            if current not in (decode(entry['before']), decode(entry['after'])):
                raise ValueError(f"Recovery would overwrite a later edit: {entry['path']}")
        # Retain every previous byte before removing or replacing a managed file.
        save(safe_path(self.target, f".local/install/history/{transaction['id']}.json"), transaction)
        for entry in transaction['entries']:
            path = safe_path(self.target, entry['path'])
            after = decode(entry['after'])
            if after is None:
                path.unlink(missing_ok=True)
            else:
                atomic_bytes(path, after)
        self.pending.unlink()

    def recover(self):
        with file_lock(safe_path(self.target, '.local/.maintenance.lock')):
            if not self.pending.exists():
                return {'recovered': False}
            self._finish(read(self.pending))
            return {'recovered': True, 'target': str(self.target)}

    def uninstall(self):
        with file_lock(safe_path(self.target, '.local/.maintenance.lock')):
            old, original = self.preflight()
            if not old or old.get('state') == 'uninstalled':
                return {'installed': False, 'data_preserved': True, 'retained_files': []}
            changes, retained = {}, []
            backup = '.local/install/disabled-skill/' + uuid.uuid4().hex
            for name in old['resources']:
                path = safe_path(self.target, name)
                if not name.startswith(SKILL + '/'):
                    raise ValueError('Invalid owned Skill resource')
                if path.exists():
                    saved = backup + '/' + name.removeprefix(SKILL + '/')
                    changes[saved] = path.read_bytes()
                    changes[name] = None
                    retained.append(saved)
            changes['.codex/config.toml'] = mcp_config(original, None).encode('utf-8')
            changes[RECEIPT] = json_bytes({**old, 'state': 'uninstalled', 'resources': {},
                                          'retained_files': retained})
            self._commit(changes, expected_config=original)
            return {'installed': False, 'data_preserved': True, 'retained_files': retained}

    def rollback(self):
        with file_lock(safe_path(self.target, '.local/.maintenance.lock')):
            old, original = self.preflight()
            if not old or old.get('state') == 'uninstalled' or not old.get('previous_runtime'):
                raise ValueError('No installed previous runtime to restore')
            previous = old['previous_runtime']
            verify_runtime(previous, self.target)
            mcp = server_config(previous, self.target)
            updated = {**old, 'runtime': previous, 'previous_runtime': old['runtime'], 'mcp': mcp}
            self._commit({RECEIPT: json_bytes(updated),
                          '.codex/config.toml': mcp_config(original, mcp).encode('utf-8')},
                         expected_config=original)
            return {'rolled_back': True, 'runtime': previous, 'data_and_choice_preserved': True}


def runtime_inputs(source):
    source = Path(source)
    names = ['pyproject.toml', 'requirements.lock', 'LICENSE']
    names += [p.relative_to(source).as_posix() for p in sorted((source / 'paa').glob('*.py'))]
    names += [p.relative_to(source).as_posix() for p in sorted((source / 'paa').glob('*.html'))]
    return {name: safe_path(source, name).read_bytes() for name in names}


def build_runtime(source, target, *, run, wheelhouse=None):
    if sys.version_info[:2] != (3, 14) or sys.platform != 'win32':
        raise ValueError('The verified dependency lock requires Windows and Python 3.14')
    inputs = runtime_inputs(source)
    identity = fingerprint({name: digest(value) for name, value in inputs.items()})
    directory = safe_path(target, '.local/app/releases/' + identity[:20])
    manifest_path = directory / 'runtime.json'
    if manifest_path.exists():
        manifest = read(manifest_path)
        expected_python = safe_path(directory, 'venv/Scripts/python.exe')
        wheels = list(safe_path(directory, 'wheels').glob('paa_search-*.whl'))
        if (manifest.get('source_sha256') != identity or manifest.get('python') != str(expected_python)
                or not expected_python.is_file() or len(wheels) != 1
                or digest(safe_path(directory, wheels[0].relative_to(directory)).read_bytes()) != manifest.get('wheel_sha256')):
            raise ValueError('Incomplete or conflicting installed runtime')
        return manifest
    snapshot = directory / 'source'
    for name, payload in inputs.items():
        atomic_bytes(safe_path(snapshot, name), payload)
    python = directory / 'venv/Scripts/python.exe'
    if not python.exists():
        run([sys.executable, '-m', 'venv', str(directory / 'venv')], cwd=target)
    options = ['--no-index', '--find-links', str(Path(wheelhouse).resolve(strict=True))] if wheelhouse else []
    run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', *options,
         '-r', str(snapshot / 'requirements.lock')], cwd=directory)
    run([str(python), '-m', 'pip', 'wheel', '--no-index', '--no-deps', '--no-build-isolation',
         '--wheel-dir', str(directory / 'wheels'), str(snapshot)], cwd=directory)
    wheels = list((directory / 'wheels').glob('paa_search-*.whl'))
    if len(wheels) != 1:
        raise ValueError('Expected one built runtime wheel')
    run([str(python), '-m', 'pip', 'install', '--no-index', '--no-deps', '--force-reinstall', str(wheels[0])], cwd=directory)
    run([str(python), '-m', 'pip', 'check'], cwd=directory)
    run([str(python), '-c', 'import paa; print(paa.__version__, paa.__file__)'], cwd=directory)
    manifest = {'python': str(python), 'source_sha256': identity,
                'wheel_sha256': digest(wheels[0].read_bytes()), 'source_kind': 'content_snapshot',
                'directory': str(directory)}
    save(manifest_path, manifest)
    return manifest


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Manage only an explicitly installed PAA project')
    parser.add_argument('action', choices=['status', 'recover', 'uninstall', 'rollback'])
    parser.add_argument('--target', type=Path, required=True)
    args = parser.parse_args()
    installation = LocalInstallation(args.target, args.target)
    result = (installation.receipt() if args.action == 'status'
              else getattr(installation, args.action)())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
