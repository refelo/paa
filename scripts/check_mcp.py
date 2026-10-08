"""Read-only checks of an explicitly installed, isolated PAA stdio server."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def unpack(result):
    if result.isError:
        raise RuntimeError('MCP check returned an error: ' + str(result.content))
    return result.structuredContent or json.loads(result.content[0].text)


def installed_config(target):
    target = target.resolve(strict=True)
    if not (target / '.local/install/receipt.json').is_file():
        raise ValueError('Checks require an explicit installed, isolated target')
    receipt = json.loads((target / '.local/install/receipt.json').read_text(encoding='utf-8'))
    return target, receipt


async def check(target, image_id=None):
    target, receipt = installed_config(target)
    config = receipt['mcp']
    started = time.perf_counter()
    params = StdioServerParameters(command=config['command'], args=config['args'],
                                  cwd=config['cwd'], env=config['env'])
    output = target / '.local/outputs'
    output.mkdir(parents=True, exist_ok=True)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            listing = await client.list_tools()
            names = {tool.name for tool in listing.tools}
            assert names == {'library_status', 'search_images', 'get_image', 'search_cards', 'get_card', 'get_user_reference'}
            status = unpack(await client.call_tool('library_status', {}))
            assert Path(status['workspace']) == target
            expected = len(receipt['seeds'])
            assert status['active_cards'] == expected
            assert status['aesthetics'] == receipt['aesthetics']
            card_id = next(key for key, value in receipt['seeds'].items() if value['package'] == 'core')
            card = unpack(await client.call_tool('get_card', {'card_id': card_id}))
            cards = unpack(await client.call_tool('search_cards', {'query': card['card']['title'], 'route': 'hybrid', 'allow_online': False}))
            assert cards['candidates'][0]['id'] == card_id
            assert cards['effective_route'] == 'keyword' and cards['warnings']
            assert card['card']['kind'] == 'technique'
            optional_ids = [key for key, value in receipt['seeds'].items() if value['package'] == 'optional-aesthetics']
            optional = await client.call_tool('get_card', {'card_id': optional_ids[0] if optional_ids else 'PA001'})
            assert bool(optional.isError) == (not optional_ids)
            user = unpack(await client.call_tool('get_user_reference', {}))
            assert not user['available']
            summary = {'ok': True, 'tools': sorted(names), 'target': str(target),
                       'aesthetics': receipt['aesthetics'], 'active_cards': expected,
                       'runtime_release': status['runtime_release'],
                       'no_user_reference': True, 'card_effective_route': cards['effective_route'],
                       'card_warnings': cards['warnings'], 'optional_id_control': True,
                       'client': 'isolated Python MCP SDK, not desktop chat',
                       'new_provider_requests': 0}
            if image_id:
                first = await client.call_tool('get_image', {'asset_id': image_id})
                row = unpack(first)
                blocks = [part for part in first.content if part.type == 'image']
                assert blocks
                payload = base64.b64decode(blocks[0].data)
                assert payload == Path(row['preview_path']).read_bytes()
                evidence = output / 'mcp-returned-image.jpg'
                evidence.write_bytes(payload)
                second = unpack(await client.call_tool('get_image', {'asset_id': row['asset_id']}))
                assert row['asset_id'] == second['asset_id'] and row['context'] == second['context']
                found = unpack(await client.call_tool('search_images', {'query': 'synthetic shapes', 'allow_online': False}))
                assert found['effective_route'] == 'metadata' and found['warnings']
                assert found['results'][0]['asset_id'] == row['asset_id']
                summary.update(actual_image_bytes=True, same_id_and_context=True,
                               image_effective_route=found['effective_route'],
                               image_evidence=str(evidence), asset_id=row['asset_id'])
            summary['seconds'] = round(time.perf_counter() - started, 2)
            (output / 'mcp-check.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(summary, ensure_ascii=False))
            return summary


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--image-id', help='Only an explicitly created synthetic fixture ID')
    args = parser.parse_args()
    os.environ.pop('VOYAGE_API_KEY', None)
    await check(args.target, args.image_id)


if __name__ == '__main__':
    asyncio.run(main())
