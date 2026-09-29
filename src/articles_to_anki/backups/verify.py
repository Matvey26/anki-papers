"""Download a backup and verify every payload hash and SQLite integrity."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

from .backend import create_backend


def verify_archive(archive: Path) -> dict:
    with tarfile.open(archive, 'r:gz') as package:
        members = {}
        for member in package.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('Unexpected archive member type')
            name = member.name.removeprefix('./')
            if name in members:
                raise ValueError('Duplicate archive member')
            members[name] = member
        manifest = json.load(package.extractfile(members['manifest.json']))
        entries = manifest['files']
        paths = {entry['path'] for entry in entries}
        if manifest['version'] != 1 or len(paths) != len(entries):
            raise ValueError('Invalid manifest')
        if paths != set(members) - {'manifest.json'} or 'data/app.sqlite3' not in paths:
            raise ValueError('Manifest does not cover archive')
        for entry in entries:
            member = members[entry['path']]
            with package.extractfile(member) as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if member.size != entry['size'] or digest != entry['sha256']:
                raise ValueError('Payload checksum mismatch')
        databases = 0
        with tempfile.TemporaryDirectory(prefix='anki-backup-verify-') as temporary:
            for name, member in members.items():
                if Path(name).suffix not in {'.sqlite', '.sqlite3', '.anki2'}:
                    continue
                restored = Path(temporary) / 'database.sqlite3'
                with package.extractfile(member) as source, restored.open('wb') as destination:
                    shutil.copyfileobj(source, destination)
                with closing(sqlite3.connect(restored.as_uri() + '?mode=ro', uri=True)) as db:
                    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ValueError('Restored database integrity check failed')
                databases += 1
    return {'status': 'verified', 'files': len(entries), 'databases': databases,
            'archive_bytes': archive.stat().st_size}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--name', help='Default: latest completed backup')
    args = parser.parse_args(argv)
    try:
        backend = create_backend('yandex_disk', data_dir=args.data_dir)
        backups = backend.list_backups()
        name = args.name or backups[-1].name
        with tempfile.TemporaryDirectory(prefix='anki-backup-download-') as temporary:
            archive = Path(temporary) / 'backup.tar.gz'
            if not backend.download(name, archive):
                raise FileNotFoundError('Backup missing')
            result = verify_archive(archive)
        print(json.dumps(dict(result, name=name)))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
