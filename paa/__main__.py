from __future__ import annotations

import argparse
import json
from pathlib import Path

import os


def main():
    parser = argparse.ArgumentParser(description="PAA：图片、人工信息与知识卡协同；项目唯一主命令")
    parser.add_argument('--settings', type=Path, help='显式的项目私有配置；默认.local/workspace.json')
    parser.add_argument('--workspace', type=Path, help='明确的数据及维护工作区根目录')
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="启动 MCP stdio 服务，不监听网络端口")
    commands.add_parser("status", help="查看索引覆盖范围")
    doctor_parser = commands.add_parser('doctor', help='只读安装、资料与恢复诊断')
    doctor_parser.add_argument('--summary', action='store_true')
    doctor_parser.add_argument('--cloud', action='store_true', help='明确通过现有SSH检查云端')
    for name in ('maintain', 'courses', 'cloud'):
        command = commands.add_parser(name)
        command.add_argument('arguments', nargs=argparse.REMAINDER)
    verify_parser = commands.add_parser('verify')
    verify_parser.add_argument('--target', choices=['local', 'cloud', 'all'], default='local')
    commands.add_parser('user-reference', help='读取用户参考卡')
    pending = commands.add_parser('notes-pending', help='查看待备注图片')
    pending.add_argument('--id', action='append', default=[])
    pending.add_argument('--bootstrap', action='store_true', help='接纳当前已有AI备注，不改备注正文')
    distill = commands.add_parser('distill', help='V2资料阅读、雏形暂存与阶段归并')
    distill.add_argument('--base', type=Path, help='只用于显式迁移或核查旧清单')
    distill.add_argument('arguments', nargs=argparse.REMAINDER)
    commands.add_parser('credential-set', help='隐藏输入并保存当前Windows账户加密的Voyage凭据')
    card_index = commands.add_parser('cards-index', help='重建当前知识卡Voyage索引；复用同一API客户端')
    card_index.add_argument('--online', action='store_true')
    card_index.add_argument('--rebuild', action='store_true')
    indexing = commands.add_parser("index", help="只读授权图库，生成或替换项目内索引")
    indexing.add_argument("--limit", type=int, default=None)
    indexing.add_argument('--rebuild', action='store_true')
    indexing.add_argument('--online', action='store_true', help='授权范围内显式重建Voyage索引，需要预算与进程凭据')
    searching = commands.add_parser("search", help="本地检索并写出 JSON；不会自行向模型发送预览")
    searching.add_argument("query", nargs="?", default="")
    searching.add_argument("--reference-id")
    searching.add_argument("--reference-path")
    searching.add_argument("--limit", type=int, default=20, help='本轮候选数，正整数，默认20，无固定张数上限；可排除已看ID继续检索')
    searching.add_argument('--route', choices=['vector', 'metadata', 'hybrid'], default='hybrid')
    searching.add_argument('--tag', action='append', default=[])
    searching.add_argument('--exclude-id', action='append', default=[])
    network = searching.add_mutually_exclusive_group()
    network.add_argument('--online', action='store_true', default=None)
    network.add_argument('--offline', dest='online', action='store_false')
    searching.add_argument("--output", type=Path)
    getting = commands.add_parser('get', help='按素材ID重取当前图片及人工信息')
    getting.add_argument('asset_id')
    getting.add_argument('--output', type=Path)
    cards = commands.add_parser('cards-search', help='检索有条件和出处的知识卡')
    cards.add_argument('query')
    cards.add_argument('--limit', type=int, default=3)
    cards.add_argument('--route', choices=['keyword', 'hybrid'], default='keyword')
    card_network = cards.add_mutually_exclusive_group()
    card_network.add_argument('--online', action='store_true', default=None)
    card_network.add_argument('--offline', dest='online', action='store_false')
    cards.add_argument('--output', type=Path)
    card = commands.add_parser('card-get', help='按卡片ID取得有效正文与版本')
    card.add_argument('card_id')
    card.add_argument('--output', type=Path)
    note = commands.add_parser('note', help='预览AI备注草稿；--apply仅在授权范围写回')
    note.add_argument('draft', type=Path)
    note.add_argument('--apply', action='store_true')
    note.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.workspace:
        os.environ['PAA_WORKSPACE'] = str(args.workspace.resolve(strict=True))
    from .library import LOCAL, PROJECT, read_settings
    maintenance = args.command in ('index', 'cards-index', 'note', 'distill', 'credential-set', 'notes-pending', 'maintain', 'courses', 'cloud', 'verify')
    if maintenance and not Path.cwd().resolve().is_relative_to(PROJECT):
        parser.error('维护仅在PAA工作区执行；其他项目只调用检索。')
    if args.command == "serve":
        from .server import create_server
        create_server(args.settings).run(transport="stdio")
        return
    from contextlib import ExitStack
    with ExitStack() as stack:
        if maintenance:
            from .locking import file_lock
            stack.enter_context(file_lock(LOCAL / '.maintenance.lock'))
        if args.command == 'credential-set':
            from getpass import getpass
            from .credentials import save_key
            save_key(getpass('Voyage key (hidden): '))
            print('已保存项目加密凭据；不显示密钥。')
            return
        from .search import build_index
        from .workspace import Workspace
        settings = read_settings(args.settings)
        if args.command == 'distill':
            from .distillation import main as distill_main
            arguments = (['--base', str(args.base)] if args.base else []) + args.arguments
            distill_main(arguments, settings['card_store'])
            return
        workspace = Workspace(settings)
        if args.command == "index":
            result = build_index(settings, args.limit, allow_online=args.online, rebuild=args.rebuild)
        elif args.command == 'doctor':
            if args.summary or args.cloud:
                from .automation import diagnostics
                result = diagnostics(workspace, cloud=args.cloud)
            else:
                from .maintenance import doctor
                result = doctor(workspace)
        elif args.command in ('maintain', 'courses', 'cloud', 'verify'):
            if args.command == 'maintain':
                from .automation import main as dispatch
                result = dispatch(args.arguments, workspace)
            elif args.command == 'courses':
                from .courses import main as dispatch
                result = dispatch(args.arguments, workspace)
            elif args.command == 'cloud':
                from .cloud import main as dispatch
                result = dispatch(args.arguments)
            else:
                from .verification import main as dispatch
                result = dispatch(['--target', args.target], workspace)
        elif args.command == 'user-reference':
            result = workspace.get_user_reference()
        elif args.command == 'notes-pending':
            from .maintenance import notes_pending
            result = notes_pending(workspace, ids=args.id, bootstrap=args.bootstrap)
        elif args.command == "status":
            result = workspace.status()
        elif args.command == 'get':
            result = workspace.images.get(args.asset_id)
        elif args.command == 'cards-search':
            result = workspace.search_cards(args.query, args.limit, args.route, args.online)
        elif args.command == 'card-get':
            result = workspace.get_card(args.card_id)
        elif args.command == 'cards-index':
            result = workspace.index_cards(args.online, args.rebuild)
        elif args.command == 'note':
            from .notes import apply_draft
            result = apply_draft(workspace, args.draft, apply=args.apply)
        else:
            result = workspace.images.search(args.query, reference_id=args.reference_id,
                                             reference_path=args.reference_path, limit=args.limit,
                                             exclude_ids=args.exclude_id, route=args.route, tags=args.tag,
                                             allow_online=args.online)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if getattr(args, "output", None):
            destination = args.output.resolve()
            if not destination.is_relative_to(LOCAL.resolve()):
                raise ValueError("检索输出只允许写入项目 .local。")
            if settings.get('library') and destination.is_relative_to(settings['library']):
                raise ValueError('检索输出不能写进素材库。')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8")
            print(destination)
        else:
            print(rendered)
        if args.command in ('maintain', 'cloud', 'verify') and isinstance(result, dict) and result.get('status') == 'failed':
            raise SystemExit(1)


if __name__ == "__main__":
    main()
