"""Only the cloud-specific boundary: private access and actual picture delivery."""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from paa.library import LOCAL

CLOUD_AVAILABLE = importlib.util.find_spec('fastmcp') is not None
if CLOUD_AVAILABLE:
    from fastmcp.server.auth.auth import AccessToken
    from paa.cloud_server import OwnerVerifier, create_cloud_server, image_result

@unittest.skipUnless(CLOUD_AVAILABLE, 'Cloud checks require the optional cloud dependencies.')
class CloudTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tool_preserves_images_and_gallery_metadata(self):
        with tempfile.TemporaryDirectory(dir=LOCAL) as directory:
            base = Path(directory)
            library = base / 'test.library'
            for item, color in [('A', 'blue'), ('B', 'red')]:
                folder = library / f'images/{item}.info'
                folder.mkdir(parents=True)
                Image.new('RGB', (1200, 1800), color).save(folder / 'a.jpg')
                (folder / 'metadata.json').write_text(json.dumps({'id': item, 'name': 'a', 'ext': 'jpg'}))
            settings = base / 'settings.json'
            settings.write_text(json.dumps({'library': str(library), 'allow_preview_to_agent': True,
                                            'index_dir': str(base / 'runtime')}))
            server = create_cloud_server(settings, {'base_url':'https://paa.example',
                                         'github_client_id':'test','github_owner_id':'42'},
                                         client_secret='test-only-secret', signing_key='test-only-signing-key-123456')
            result = await server.call_tool('get_image', {'asset_id': 'A'})
            self.assertIn('image', [block.type for block in result.content])
            self.assertEqual(len(result.structured_content['pictures']), 1)
            url = result.structured_content['pictures'][0]['image_url']
            self.assertNotIn('data:image', json.dumps(result.meta))
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertNotIn('ui', tools['get_image'].meta or {})
            self.assertNotIn('ui', tools['search_images'].meta or {})
            self.assertIn('ui', tools['show_images'].meta)
            second = await server.call_tool('get_image', {'asset_id': 'B'})
            selected_ids = [second.structured_content['pictures'][0]['image_id'],
                            result.structured_content['pictures'][0]['image_id']]
            shown = await server.call_tool('show_images', {'pictures': [
                {'image_id': selected_ids[0], 'caption': '手侧朝向镜头，保持与道具的关系。'},
                {'image_id': selected_ids[1], 'caption': '留出视线方向的空间。'}]})
            self.assertEqual([p['image_id'] for p in shown.structured_content['pictures']], selected_ids)
            self.assertEqual(shown.structured_content['pictures'][0]['caption'],
                             '手侧朝向镜头，保持与道具的关系。')
            self.assertEqual(shown.structured_content['pictures'][0]['number'], 1)
            self.assertNotIn('image', [block.type for block in shown.content])
            app = server.http_app(stateless_http=True, allowed_hosts=['paa.example'])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
                response = await client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['content-type'], 'image/jpeg')
                self.assertEqual(response.content, base64.b64decode(next(
                    block.data for block in result.content if block.type == 'image')))
                self.assertEqual((await client.get(url + 'tampered')).status_code, 403)
                large = await client.get(shown.structured_content['pictures'][0]['large_image_url'])
                self.assertEqual(large.status_code, 200)
                import io
                with Image.open(io.BytesIO(large.content)) as image:
                    self.assertEqual(max(image.size), 1600)
                with patch('paa.cloud_server.time.time', return_value=9999999999):
                    self.assertEqual((await client.get(url)).status_code, 403)

    async def test_another_github_user_is_denied(self):
        verifier = OwnerVerifier('42')
        token = AccessToken(token='test', client_id='test', scopes=['read:user'], claims={'sub': '43'})
        with patch('paa.cloud_server.GitHubTokenVerifier.verify_token', new=AsyncMock(return_value=token)):
            self.assertIsNone(await verifier.verify_token('test'))
            token.claims['sub'] = '42'
            self.assertIs(await verifier.verify_token('test'), token)

    async def test_http_requires_login_and_discovery_remains_available(self):
        LOCAL.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=LOCAL) as temp:
            base = Path(temp)
            library = base / 'test.library'
            (library / 'images').mkdir(parents=True)
            settings = base / 'settings.json'
            settings.write_text(json.dumps({'library': str(library), 'allow_preview_to_agent': True}))
            server = create_cloud_server(settings, {'base_url': 'https://paa.example',
                                         'github_client_id': 'test', 'github_owner_id': '42'},
                                         client_secret='test-only-client-secret', signing_key='test-only-signing-key-0123456789')
            app = server.http_app(stateless_http=True, allowed_hosts=['paa.example'])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://paa.example') as client:
                response = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                                             headers={'Accept': 'application/json, text/event-stream'})
                self.assertEqual(response.status_code, 401)
                self.assertIn('resource_metadata', response.headers['www-authenticate'])
                response = await client.get('/.well-known/oauth-authorization-server')
                self.assertEqual(response.status_code, 200)
                self.assertIn('S256', response.json()['code_challenge_methods_supported'])

    async def test_picture_bytes_reach_model_and_gallery_with_same_id(self):
        with tempfile.TemporaryDirectory(dir=LOCAL) as temp:
            path = Path(temp) / 'image.jpg'
            Image.new('RGB', (32, 48), 'blue').save(path)
            picture = {'number': 1, 'asset_id': 'same-image', 'preview_path': str(path),
                       'source_path': 'private/server/path', 'context': {'annotation': 'test'}}
            result = image_result({'results': [picture]}, [picture],
                                  preview_url=lambda path: 'https://paa.example/preview/signed-image')
            visual = next(block for block in result.content if block.type == 'image')
            self.assertEqual(base64.b64decode(visual.data), path.read_bytes())
            self.assertEqual(result.structured_content['pictures'][0]['image_id'], 'same-image')
            self.assertEqual(result.structured_content['pictures'][0]['context'], picture['context'])
            self.assertNotIn('private/server/path', result.content[0].text)


if __name__ == '__main__':
    unittest.main()
