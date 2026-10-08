"""Narrow SSH-only cloud operations. Never exposed as MCP tools."""
from __future__ import annotations

import argparse
import hashlib
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from . import __version__
from .cards import read, save
from .library import LOCAL, read_settings
from .workspace import Workspace


def status():
    active = subprocess.run(['systemctl', 'is-active', 'paa.service'], capture_output=True, text=True)
    workspace = Workspace()
    result = {'evidence': 'live_ssh', 'runtime_version': __version__, 'service': active.stdout.strip(),
              'workspace': str(LOCAL.parent), 'status': workspace.status(),
              'cards_snapshot': workspace.cards().snapshot()[2]}
    for name in ('last-sync.json', 'cloud-refresh.json', 'cloud-deployed.json', 'maintenance-sync.json'):
        path = LOCAL / name
        if path.exists():
            value = read(path)
            if name == 'cloud-refresh.json':
                value = {k: v for k, v in value.items() if k != 'status'}
            result[name] = value
    import paa
    code_root = Path(paa.__file__).resolve().parents[1]
    manifest = code_root / 'deployment-manifest.json'
    result['code_integrity'] = {'state': 'unrecorded'}
    if manifest.exists():
        manifest = read(manifest)
        mismatches = []
        for name, expected in manifest['deployed_files_sha256'].items():
            path = (code_root / name).resolve()
            if not path.is_relative_to(code_root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                mismatches.append(name)
        result['code_integrity'] = {'state': 'mismatch' if mismatches else 'verified',
                                    'mismatches': mismatches, 'release': manifest['release']}
    return result


async def tool_check():
    with tempfile.TemporaryDirectory(prefix='paa-tool-probe-') as cache:
        previous = os.environ.get('FASTMCP_HOME')
        os.environ['FASTMCP_HOME'] = cache
        try:
            return await _tool_check()
        finally:
            if previous is None:
                os.environ.pop('FASTMCP_HOME', None)
            else:
                os.environ['FASTMCP_HOME'] = previous


async def _tool_check():
    from fastmcp import Client
    from .cloud_server import create_cloud_server
    config = read(LOCAL / 'cloud.json')
    # In-process production-code/data probe; real OAuth/UI is tested separately.
    server = create_cloud_server(LOCAL / 'workspace.json', config,
                                 client_secret='in-process-probe-only', signing_key='in-process-probe-only')
    async with Client(server) as client:
        tools = {tool.name for tool in await client.list_tools()}
        expected = {'library_status', 'search_images', 'get_image', 'search_cards', 'get_card', 'get_user_reference', 'show_images'}
        if tools != expected:
            raise ValueError('云端工具集合不符合预期。')
        workspace = Workspace(read_settings())
        from .library import catalog
        ids, _ = catalog(workspace.settings['library'])
        image = workspace.images.get(ids[0])
        async def call(name, args):
            result = await client.call_tool(name, args)
            if result.is_error:
                raise ValueError(f'云端工具失败：{name}')
            return result
        await call('library_status', {})
        await call('get_image', {'asset_id': image['asset_id']})
        await call('search_images', {'query': '光', 'route': 'metadata', 'limit': 2, 'allow_online': False})
        await call('get_user_reference', {})
        cards, _, _ = workspace.cards().snapshot()
        card = next(c for c in cards.values() if c['status'] == 'active')
        await call('get_card', {'card_id': card['id']})
        await call('search_cards', {'query': card['title'], 'limit': 2, 'allow_online': False})
        await call('show_images', {'pictures': [{'image_id': image['asset_id'], 'caption': 'PAA deployment verification'}]})
        return {'tools': sorted(tools), 'actual_image': image['eagle_id'],
                'card_id': card['id'], 'transport': 'in_process_production_code_and_data',
                'browser_oauth_verified': False}


def verify():
    value = status()
    if value['service'] != 'active':
        raise ValueError('云端服务未启动。')
    if value['code_integrity']['state'] == 'mismatch':
        raise ValueError('运行代码与部署摘要不一致。')
    config = read(LOCAL / 'cloud.json')
    deadline = time.monotonic() + 20
    while True:
        try:
            with urllib.request.urlopen(config['base_url'] + '/health', timeout=5) as response:
                health = json.load(response)
            break
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(.5)
    if health.get('status') != 'ok':
        raise ValueError('云端HTTPS健康检查失败。')
    req = urllib.request.Request(config['base_url'] + '/mcp', data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                                 headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            auth_status = response.status
    except urllib.error.HTTPError as error:
        auth_status = error.code
    if auth_status not in (401, 403):
        raise ValueError('未认证MCP请求未被拒绝。')
    value['verification'] = {'https': health, 'unauthenticated_status': auth_status,
                             'tools': asyncio.run(tool_check())}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'verify', 'receipt', 'sync-state', 'hashes'])
    parser.add_argument('--sync-id')
    parser.add_argument('--fingerprint')
    args = parser.parse_args()
    if args.action == 'hashes':
        from .library import catalog, item_source
        root = read_settings()['library']
        hashes = {}
        for item in catalog(root)[0]:
            path, _ = item_source(root, item)
            with path.open('rb') as stream:
                hashes['library.library/' + path.relative_to(root).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
        result = {'hashes': hashes, 'evidence': 'actual_remote_content_sha256'}
    elif args.action == 'sync-state':
        result = {}
        for label, name in [('incoming', 'import/snapshot.json'), ('imported', 'last-sync.json'), ('refreshed', 'cloud-refresh.json')]:
            path = LOCAL / name
            value = read(path) if path.exists() else {}
            result[label] = {k: value.get(k) for k in ('sync_id', 'data_fingerprint')}
    elif args.action == 'receipt':
        if not args.sync_id or not args.fingerprint:
            parser.error('receipt requires sync-id and fingerprint')
        save(LOCAL / 'maintenance-sync.json', {'sync_id': args.sync_id, 'data_fingerprint': args.fingerprint})
        result = {'recorded': True}
    else:
        result = verify() if args.action == 'verify' else status()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
