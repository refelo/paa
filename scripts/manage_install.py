"""Project-only Windows installation; no global registration or credential discovery."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paa.installation import LocalInstallation, build_runtime  # noqa: E402


def run(command, *, cwd):
    temporary = ROOT / '.local/install-build/tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8='1', PYTHONNOUSERSITE='1',
               TMP=str(temporary), TEMP=str(temporary), PIP_DISABLE_PIP_VERSION_CHECK='1',
               PIP_CACHE_DIR=str(ROOT / '.local/install-build/cache'))
    env.pop('PYTHONPATH', None)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'recover', 'status', 'uninstall', 'rollback'])
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--aesthetics', choices=['enable', 'disable'],
                        help='Omission preserves the saved choice; first install remains unanswered')
    parser.add_argument('--library', type=Path, help='An explicitly authorized Eagle library; no discovery')
    parser.add_argument('--allow-preview', action='store_true', help='Authorize preview sharing for this target')
    parser.add_argument('--wheelhouse', type=Path, help='Install dependencies offline from this wheel directory')
    args = parser.parse_args(argv)
    target = args.target.absolute()
    installer = LocalInstallation(ROOT, target)
    if args.action == 'install':
        installer.preflight()
        target.mkdir(parents=True, exist_ok=True)
        runtime = build_runtime(ROOT, target, run=run, wheelhouse=args.wheelhouse)
        result = installer.install(runtime, aesthetics=args.aesthetics, library=args.library,
                                   allow_preview=args.allow_preview)
    elif args.action in ('recover', 'uninstall', 'rollback'):
        result = getattr(installer, args.action)()
    else:
        receipt = installer.receipt()
        result = {'installed': receipt is not None and receipt.get('state') != 'uninstalled', 'receipt': receipt,
                  'pending_recovery': installer.pending.exists()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
