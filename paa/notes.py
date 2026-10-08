"""Explicit note drafts and guarded annotation-only writes via Eagle's API."""
from __future__ import annotations

import json
from pathlib import Path
import time
import urllib.request

from .cards import read
from .voyage import NoRedirect
from .library import LOCAL, SourceError, item_context

MARKER = '【ai生成】'


def compose(original, body):
    if not isinstance(original, str) or not isinstance(body, str) or not body.strip():
        raise SourceError('备注草稿为空或原文格式不支持。')
    if MARKER in body:
        raise SourceError('草稿正文不能含区块分隔符。')
    prefix = original.split(MARKER, 1)[0]
    separator = '' if not prefix or prefix.endswith('\n\n') else '\n' if prefix.endswith('\n') else '\n\n'
    return prefix + separator + MARKER + '\n' + body.strip()


class EagleAPI:
    def __call__(self, endpoint, data=None):
        if endpoint not in ('library/info', 'item/update'):
            raise SourceError('不允许此Eagle操作。')
        if data is not None and set(data) != {'id', 'annotation'}:
            raise SourceError('只允许写annotation。')
        request = urllib.request.Request('http://127.0.0.1:41595/api/' + endpoint,
                                        data=json.dumps(data).encode() if data is not None else None,
                                        headers={'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=15) as response:
            value = json.load(response)
        if value.get('status') != 'success':
            raise SourceError('Eagle API操作未成功。')
        return value.get('data')


def apply_draft(workspace, draft_path, *, apply=False, api=None):
    path = Path(draft_path).resolve(strict=True)
    if not path.is_relative_to(LOCAL.resolve()):
        raise SourceError('草稿必须位于本项目.local。')
    draft = read(path)
    if not isinstance(draft, dict) or set(draft) != {'asset_id', 'body'}:
        raise SourceError('草稿须仅包含asset_id、body。')
    image = workspace.images.get(draft['asset_id'])
    context = image['context']
    original = context.get('annotation')
    if original is None and 'annotation' in context['missing_fields']:
        original = ''
    annotation = compose(original, draft['body'])
    result = {'asset_id': image['asset_id'], 'eagle_id': image['eagle_id'],
              'annotation': annotation, 'applied': False}
    if not apply:
        return result
    api = api or EagleAPI()
    root = workspace.settings['library']
    def guard():
        info = api('library/info')
        if Path(info['library']['path']).resolve() != root.resolve():
            raise SourceError('Eagle当前库不是本次授权库；未写入，不自动切库。')
    guard()
    api('item/update', {'id': image['eagle_id'], 'annotation': annotation})
    guard()
    latest = None
    for _ in range(20):
        latest = item_context(root, image['eagle_id'])
        if latest['annotation'] == annotation:
            break
        time.sleep(0.1)
    if latest['annotation'] != annotation:
        raise SourceError('备注回读不一致；本次不计成功，不自动重复写入。')
    from .maintenance import note_completed
    note_completed(workspace, image)
    return {**result, 'applied': True}
