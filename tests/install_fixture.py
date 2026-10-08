"""Synthetic seed packages; no third-party candidate material is shipped in tests."""
from pathlib import Path
import shutil

from paa.cards import save
from paa.installation import digest, json_bytes
from test_cards import fixture


def source_fixture(root, source):
    root = Path(root)
    for folder in ('docs', 'examples', 'integrations/codex/paa'):
        shutil.copytree(source / folder, root / folder)
    rows = []
    for prefix, count, kind in (('PT', 4, 'technique'), ('PA', 2, 'preference')):
        package = 'core' if kind == 'technique' else 'optional-aesthetics'
        for i in range(1, count + 1):
            identity = f'{prefix}{i:03}'
            card = fixture(identity, '窗光 柔和' if identity == 'PT004' else 'Synthetic sample ' + identity)
            card.update(kind=kind, author='Synthetic test author', problem='fixture',
                        conditions=['fixture scope'], method=['fixture method'], support_scope='synthetic only')
            path = f'knowledge/{package}/cards/{identity}.json'
            payload = json_bytes(card)
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            rows.append({'id': identity, 'kind': kind, 'path': path, 'sha256': digest(payload)})
        (root / f'knowledge/{package}/compilation.md').write_text('# Synthetic fixture\n', encoding='utf-8')
    save(root / 'knowledge/manifest.json', {'version': 'synthetic-1', 'cards': rows,
                                          'core_count': 4, 'optional_aesthetics_count': 2})
    return root
