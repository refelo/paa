"""Focused runtime checks; interactive acceptance is recorded separately."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from . import __version__
from .cards import read, save
from .library import LOCAL, PROJECT


def verify_local(workspace):
    active = read(LOCAL / 'app/active.json')
    if active['version'] != __version__ or Path(active['workspace']).resolve() != PROJECT:
        raise ValueError('安装版与候选代码版本/数据位置不一致。')
    script = Path(__file__).resolve().parents[1] / 'scripts/check_mcp.py'
    result = subprocess.run([sys.executable, str(script), '--installed'], cwd=PROJECT,
                            capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('安装版MCP检查失败：' + result.stderr[-2500:])
    report = json.loads(result.stdout.strip())
    if report['runtime_version'] != __version__:
        raise ValueError('MCP进程实际版本与候选版本不符。')
    status = workspace.status()
    if status['indexed'] != status['eligible'] or status['card_vector_index']['state'] != 'ready':
        raise ValueError('当前本机索引尚未覆盖有效资料。')
    return {'installed_mcp': report, 'desktop_connection_verified': False,
            'notice': '本结果不是桌面自然语言/图像可见性验收。'}


def main(argv, workspace):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['local', 'cloud', 'all'], default='local')
    args = parser.parse_args(argv)
    report = {'checked_at': time.time(), 'version': __version__, 'target': args.target}
    if args.target in ('local', 'all'):
        report['local'] = verify_local(workspace)
    if args.target in ('cloud', 'all'):
        from .cloud import CloudManager
        report['cloud'] = CloudManager(PROJECT).operation('verify')
    destination = LOCAL / 'maintenance/verification' / (time.strftime('%Y%m%d-%H%M%S') + '.json')
    save(destination, report)
    return {**report, 'record': str(destination)}
