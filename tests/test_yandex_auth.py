from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from articles_to_anki.backups import oauth_cli, yandex_auth as auth
from articles_to_anki.backups.oauth_store import OAuthStore


@pytest.fixture
def provider(monkeypatch):
    class Client:
        calls = []
        failure = None

        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_token(self, code, **kwargs):
            self.calls.append(('code', code, kwargs))
            return SimpleNamespace(access_token='first', refresh_token='refresh-first', expires_in=365 * 86400)

        def refresh_token(self, token, **kwargs):
            self.calls.append(('refresh', token, kwargs))
            if self.failure:
                raise self.failure
            return SimpleNamespace(access_token='second', refresh_token='refresh-second', expires_in=365 * 86400)

    monkeypatch.setattr(auth.yadisk, 'Client', Client)
    monkeypatch.setattr(auth.time, 'time', lambda: 1000)
    return Client


def bootstrap(tmp_path):
    pending = tmp_path / 'pending.json'
    token = tmp_path / 'yandex.json'
    url = auth.begin(pending, 'client-id', 'client-secret')
    auth.complete(pending, token, '1234567')
    return token, url


def test_bootstrap_pkce_private_storage_and_safe_status(tmp_path, provider):
    path, url = bootstrap(tmp_path)
    query = parse_qs(urlparse(url).query)
    assert query['code_challenge_method'] == ['S256']
    assert query['scope'] == [auth.SCOPE]
    assert 'client-secret' not in url
    assert len(provider.calls[0][2]['code_verifier']) >= 43
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / 'pending.json').exists()
    state = OAuthStore(path).read()
    assert state['refresh_token'] == 'refresh-first'
    assert state['refresh_at'] == 1000 + 90 * 86400
    assert set(auth.status(path)) == {'status', 'expires_at', 'refresh_at'}


def test_refresh_only_when_due_persists_rotation(tmp_path, provider, monkeypatch):
    path, _ = bootstrap(tmp_path)
    assert auth.access_token(path) == 'first'
    assert len(provider.calls) == 1
    monkeypatch.setattr(auth.time, 'time', lambda: 1000 + 90 * 86400)
    assert auth.access_token(path) == 'second'
    assert OAuthStore(path).read()['refresh_token'] == 'refresh-second'
    auth.refresh(path, force=True)
    assert provider.calls[-1][1] == 'refresh-second'


def test_failed_refresh_keeps_previous_credentials_and_redacts_logs(tmp_path, provider, monkeypatch, capsys):
    path, _ = bootstrap(tmp_path)
    before = path.read_bytes()
    provider.failure = RuntimeError('DO-NOT-LOG-SECRET')
    with pytest.raises(SystemExit) as error:
        oauth_cli.main(['--state-file', str(path), 'refresh', '--force'])
    assert error.value.code == 1
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert 'DO-NOT-LOG-SECRET' not in output.out + output.err
    assert 'RuntimeError' in output.out


def test_missing_configuration_is_a_safe_timer_noop(tmp_path, capsys):
    oauth_cli.main(['--state-file', str(tmp_path / 'absent.json'), 'refresh'])
    assert 'not_configured' in capsys.readouterr().out


def test_short_lifetime_renews_before_expiry():
    state = auth._with_tokens({'client_id': 'id', 'client_secret': 'secret'}, SimpleNamespace(access_token='a', refresh_token='r', expires_in=3600), 1000)
    assert state['refresh_at'] == 2800
    assert state['expires_at'] == 4600


def test_invalid_response_does_not_replace_state(tmp_path, provider, monkeypatch):
    path, _ = bootstrap(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(provider, 'refresh_token', lambda *a, **kw: SimpleNamespace(access_token='a', refresh_token=None, expires_in=3600))
    with pytest.raises(ValueError):
        auth.refresh(path, force=True)
    assert path.read_bytes() == before


def test_atomic_write_failure_leaves_old_file(tmp_path, monkeypatch):
    path = tmp_path / 'token.json'
    with OAuthStore(path).locked() as store:
        store.save({'version': 1})
        monkeypatch.setattr('articles_to_anki.backups.oauth_store.json.dump', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):
            store.save({'version': 2})
        assert store.read() == {'version': 1}
    assert not list(tmp_path.glob('.oauth-*'))
