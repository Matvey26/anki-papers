"""OAuth setup commands. Secrets and provider errors never go to logs."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path

from . import yandex_auth


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Set up and renew Yandex Disk OAuth')
    parser.add_argument('--state-file', type=Path, default=Path('.oauth/yandex.json'))
    parser.add_argument('--pending-file', type=Path, default=Path('.oauth/pending.json'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('begin')
    commands.add_parser('complete')
    renew = commands.add_parser('refresh')
    renew.add_argument('--force', action='store_true')
    commands.add_parser('status')
    args = parser.parse_args(argv)
    try:
        if args.command == 'begin':
            print(yandex_auth.begin(args.pending_file, os.environ['YANDEX_DISK_CLIENT_ID'], os.environ['YANDEX_DISK_CLIENT_SECRET']))
            return
        if args.command == 'complete':
            yandex_auth.complete(args.pending_file, args.state_file, getpass.getpass('Yandex authorization code: '))
        if args.command == 'refresh':
            if args.state_file.exists():
                yandex_auth.refresh(args.state_file, force=args.force)
            else:
                print(json.dumps({'status': 'skipped', 'reason': 'not_configured'}))
                return
        print(json.dumps(yandex_auth.status(args.state_file)))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
