import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from yadisk.exceptions import PathNotFoundError

from articles_to_anki.backups import yandex_disk as disk
from articles_to_anki.backups.snapshot import build_snapshot
from articles_to_anki.backups.verify import verify_archive


@pytest.fixture
def storage(monkeypatch, tmp_path):
    class Client:
        files = {}
        corrupt = False
        fail_move = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def mkdir(self, path):
            pass

        def upload(self, source, path, **kwargs):
            self.files[path] = Path(source).read_bytes()

        def get_meta(self, path):
            content = self.files[path]
            return SimpleNamespace(size=len(content), sha256='bad' if self.corrupt else hashlib.sha256(content).hexdigest())

        def move(self, source, target, **kwargs):
            assert kwargs['wait'] and not kwargs['overwrite']
            if self.fail_move:
                raise RuntimeError('move failed')
            self.files[target] = self.files.pop(source)

        def remove(self, path, **kwargs):
            assert kwargs['permanently'] is False
            self.files.pop(path, None)

        def download(self, path, destination):
            if path not in self.files:
                raise PathNotFoundError('missing')
            Path(destination).write_bytes(self.files[path])

        def listdir(self, path):
            return [SimpleNamespace(name=p.rsplit('/', 1)[-1], size=len(v), type='file') for p, v in self.files.items()]

    client = Client()
    backend = disk.YandexDiskBackupBackend(tmp_path / 'unused.json')
    monkeypatch.setattr(backend, '_client', lambda: client)
    return backend, client


def test_upload_list_download_delete_roundtrip(storage, tmp_path):
    backend, client = storage
    source = tmp_path / 'source'
    source.write_bytes(b'archive content')
    name = 'anki-papers-test.tar.gz'
    backend.upload(source, name)
    client.files['app:/backups/.upload-incomplete'] = b'incomplete'
    assert [item.name for item in backend.list_backups()] == [name]
    destination = tmp_path / 'restored'
    assert backend.download(name, destination)
    assert destination.read_bytes() == source.read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o600
    backend.delete(name)
    backend.delete(name)
    assert not backend.download(name, destination)
    assert destination.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('failure', ['corrupt', 'fail_move'])
def test_failed_upload_is_never_published(storage, tmp_path, failure):
    backend, client = storage
    setattr(client, failure, True)
    source = tmp_path / 'source'
    source.write_bytes(b'archive')
    with pytest.raises(RuntimeError):
        backend.upload(source, 'anki-papers-test.tar.gz')
    assert not client.files


def test_corrupt_download_preserves_destination(storage, tmp_path):
    backend, client = storage
    client.files['app:/backups/anki-papers-test.tar.gz'] = b'bad'
    client.corrupt = True
    destination = tmp_path / 'restored'
    destination.write_bytes(b'previous')
    with pytest.raises(RuntimeError):
        backend.download('anki-papers-test.tar.gz', destination)
    assert destination.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.backup-*'))


@pytest.mark.parametrize('name', ['../outside.tar.gz', 'disk:/other', 'anki-papers-../../x.tar.gz'])
def test_rejects_paths_outside_backups(storage, tmp_path, name):
    backend, _ = storage
    with pytest.raises(ValueError):
        backend.upload(tmp_path / 'absent', name)


def test_verifies_downloaded_archive_and_detects_damage(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    with sqlite3.connect(data / 'app.sqlite3') as db:
        db.execute('CREATE TABLE example (id INTEGER)')
        db.execute('INSERT INTO example VALUES (1)')
    (data / 'document.txt').write_text('persistent data')
    archive = build_snapshot(data, tmp_path)
    result = verify_archive(archive)
    assert result['files'] == 2
    assert result['databases'] == 1
    import tarfile
    snapshot = tmp_path / 'snapshot'
    (snapshot / 'data/document.txt').write_text('corrupted content')
    with tarfile.open(archive, 'w:gz') as package:
        package.add(snapshot, arcname='.')
    with pytest.raises(ValueError, match='checksum'):
        verify_archive(archive)
