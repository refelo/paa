"""Build and exercise fresh Windows installs using only synthetic local data."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from check_mcp import check  # noqa: E402
from paa.cards import read, save  # noqa: E402
from paa.installation import LocalInstallation, build_runtime  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    args = parser.parse_args()
    directory = ROOT / '.local/qa/stage-b' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True)
    temporary = directory / 'tmp'
    temporary.mkdir()
    env = dict(os.environ, TMP=str(temporary), TEMP=str(temporary), PYTHONUTF8='1',
               PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INDEX='1',
               PIP_CACHE_DIR=str(directory / 'pip-cache'))
    for name in ('VOYAGE_API_KEY', 'PYTHONPATH', 'PAA_WORKSPACE'):
        env.pop(name, None)
    results = []
    log = directory / 'build.log'
    def run(command, *, cwd):
        with log.open('a', encoding='utf-8') as output:
            subprocess.run(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, check=True)
    try:
        for choice in (None, 'disable', 'enable'):
            target = directory / (choice or 'unanswered')
            target.mkdir()
            runtime = build_runtime(ROOT, target, run=run, wheelhouse=args.wheelhouse)
            installer = LocalInstallation(ROOT, target)
            installer.install(runtime, aesthetics=choice)
            before = (target / '.local/install/receipt.json').read_bytes()
            installer.install(runtime)
            assert (target / '.local/install/receipt.json').read_bytes() == before
            library = target / 'synthetic.library'
            if choice == 'disable':
                item = library / 'images/SYNTH001.info'
                item.mkdir(parents=True)
                image = Image.new('RGB', (640, 400), 'white')
                draw = ImageDraw.Draw(image)
                draw.rectangle((60, 80, 250, 310), fill='#b32f42')
                draw.ellipse((340, 100, 550, 310), fill='#198565')
                draw.text((65, 345), 'SYNTH001 - local installation fixture', fill='black')
                image.save(item / 'shapes.png')
                save(item / 'metadata.json', {'id': 'SYNTH001', 'name': 'shapes', 'ext': 'png',
                     'tags': ['synthetic', 'shapes'], 'annotation': '【ai生成】synthetic shapes fixture',
                     'star': 0, 'comments': [], 'folders': []})
                installer.install(runtime, library=library, allow_preview=True)
            result = asyncio.run(check(target, 'SYNTH001' if choice == 'disable' else None))
            loaded = subprocess.check_output([runtime['python'], '-I', '-c', 'import paa; print(paa.__file__)'],
                                             cwd=target, env=env, text=True).strip()
            assert Path(loaded).is_relative_to(target / '.local/app')
            result['installed_module'] = loaded
            process_env = dict(env, PAA_WORKSPACE=str(target))
            if choice == 'disable':
                command = [runtime['python'], '-I', '-X', 'utf8', '-m', 'paa', 'maintain', 'run', '--no-courses']
                maintenance = json.loads(subprocess.check_output(command, cwd=target, env=process_env, text=True, encoding='utf-8'))
                assert maintenance['status'] == 'completed', maintenance
                assert maintenance['receipts']['cloud']['status'] == 'not_requested'
                result['maintenance'] = maintenance
                failed = subprocess.run([*command, '--cloud'], cwd=target, env=process_env,
                                        capture_output=True, text=True, encoding='utf-8')
                failure = json.loads(failed.stdout)
                assert failed.returncode == 1 and failure['stage'] == 'preflight'
                result['missing_cloud_rejected'] = True
            if choice == 'enable':
                installer.install(runtime, aesthetics='disable')
                result['disabled_after_enable'] = asyncio.run(check(target))
                assert read(target / '.local/workspace.json')['aesthetics'] == 'disable'
            results.append(result)
        save(directory / 'acceptance.json', {'ok': True, 'results': results,
             'boundary': 'fresh venv and installed CLI/SDK; not desktop chat or real Voyage'})
    except Exception as error:
        save(directory / 'acceptance.json', {'ok': False, 'results': results, 'error': repr(error)})
        raise
    print(json.dumps({'ok': True, 'report': str(directory / 'acceptance.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
