"""Private Yandex Disk app-folder storage with checked, atomic publication."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

import yadisk
from yadisk.exceptions import DirectoryExistsError, PathNotFoundError

from .backend import BackupBackend, BackupInfo
from .yandex_auth import access_token

ROOT = 'app:/backups'
NAME = re.compile(r'anki-papers-[A-Za-z0-9_-]+\.tar\.gz\Z')


def _path(name: str) -> str:
    if not NAME.fullmatch(name):
        raise ValueError('Invalid backup name')
    return f'{ROOT}/{name}'


def _check(client, remote: str, local: Path) -> None:
    meta = client.get_meta(remote)
    with local.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if meta.size != local.stat().st_size or meta.sha256 != digest:
        raise RuntimeError('Backup checksum mismatch')


class YandexDiskBackupBackend(BackupBackend):
    def __init__(self, token_file: Path):
        self.token_file = token_file

    def _client(self):
        return yadisk.Client(token=access_token(self.token_file), default_args={
            'timeout': (15, 120), 'n_retries': 2,
        })

    def upload(self, archive: Path, name: str) -> None:
        final = _path(name)
        temporary = f'{ROOT}/.upload-{uuid.uuid4().hex}'
        with self._client() as client:
            with suppress(DirectoryExistsError):
                client.mkdir(ROOT)
            try:
                client.upload(str(archive), temporary, overwrite=False)
                _check(client, temporary, archive)
                client.move(temporary, final, overwrite=False, wait=True, poll_timeout=120, n_retries=0)
            finally:
                # Only our temporary object is eligible for cleanup. Never remove
                # the completed archive after an ambiguous network response.
                with suppress(Exception):
                    client.remove(temporary, permanently=False, wait=True, poll_timeout=30)

    def download(self, name: str, destination: Path) -> bool:
        remote = _path(name)
        fd, temporary_name = tempfile.mkstemp(prefix='.backup-', dir=destination.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with self._client() as client:
                try:
                    client.download(remote, str(temporary))
                except PathNotFoundError:
                    return False
                _check(client, remote, temporary)
            temporary.replace(destination)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def list_backups(self) -> list[BackupInfo]:
        with self._client() as client:
            try:
                result = [BackupInfo(item.name, item.size) for item in client.listdir(ROOT)
                          if item.type == 'file' and NAME.fullmatch(item.name)]
            except PathNotFoundError:
                return []
        return sorted(result, key=lambda item: item.name)

    def delete(self, name: str) -> None:
        remote = _path(name)
        with self._client() as client:
            with suppress(PathNotFoundError):
                client.remove(remote, permanently=False, wait=True, poll_timeout=120)
