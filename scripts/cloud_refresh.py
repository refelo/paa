"""Complete cloud indexes after a user-requested sync; run via SSH as maintenance."""
import json
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paa.library import read_settings
from paa.search import build_index
from paa.workspace import Workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync-id')
    parser.add_argument('--fingerprint')
    args = parser.parse_args()
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
    os.environ['VOYAGE_API_KEY'] = (credentials / 'voyage').read_text().strip()
    settings = read_settings()
    from paa.cards import read
    from paa.locking import file_lock
    local = Path(settings['index_dir']).parent
    with file_lock(local / '.cloud-import.lock'):
        previous = read(local / 'last-sync.json')
        if args.sync_id and (previous.get('sync_id'), previous.get('data_fingerprint')) != (args.sync_id, args.fingerprint):
            raise ValueError('Refresh snapshot no longer matches this synchronization.')
        return refresh(settings, previous)


def refresh(settings, previous):
    images = build_index(settings, allow_online=True)
    workspace = Workspace(settings)
    card_status = workspace.status()['card_vector_index']
    cards = workspace.index_cards(allow_online=True) if card_status['state'] != 'ready' else card_status
    result = {'images': images, 'cards': cards, 'status': workspace.status(),
              'sync_id': previous.get('sync_id'), 'data_fingerprint': previous.get('data_fingerprint')}
    destination = Path(settings['index_dir']).parent / 'cloud-refresh.json'
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
