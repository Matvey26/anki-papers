"""Authorization-code bootstrap and automatic Yandex OAuth renewal."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import yadisk

from .oauth_store import OAuthStore

SCOPE = 'cloud_api:disk.app_folder'
REDIRECT_URI = 'https://oauth.yandex.ru/verification_code'
RENEW_INTERVAL = 90 * 86400


def begin(path: Path, client_id: str, client_secret: str) -> str:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    pending = {
        'client_id': client_id, 'client_secret': client_secret,
        'verifier': verifier, 'created_at': time.time(),
        'state': secrets.token_urlsafe(32),
    }
    with OAuthStore(path).locked() as store:
        store.save(pending)
    return 'https://oauth.yandex.ru/authorize?' + urlencode({
        'response_type': 'code', 'client_id': client_id,
        'redirect_uri': REDIRECT_URI, 'scope': SCOPE,
        'state': pending['state'], 'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })


def _with_tokens(credentials: dict, token, now: float) -> dict:
    if not token.access_token or not token.refresh_token:
        raise ValueError('OAuth response is missing tokens')
    lifetime = float(token.expires_in)
    if lifetime <= 0:
        raise ValueError('OAuth response has invalid lifetime')
    # Renew early enough for daily checks and network outages; short-lived
    # tokens are also renewed on demand by access_token().
    interval = min(RENEW_INTERVAL, max(1, lifetime - min(7 * 86400, lifetime / 2)))
    return {
        'client_id': credentials['client_id'],
        'client_secret': credentials['client_secret'],
        'access_token': token.access_token, 'refresh_token': token.refresh_token,
        'obtained_at': now, 'expires_at': now + lifetime,
        'refresh_at': now + interval,
    }


def complete(pending_path: Path, token_path: Path, code: str) -> None:
    with OAuthStore(pending_path).locked() as pending_store:
        pending = pending_store.read()
        if time.time() - pending['created_at'] > 3600:
            raise ValueError('Authorization attempt expired; start again')
        with OAuthStore(token_path).locked() as store:
            with yadisk.Client(pending['client_id'], pending['client_secret']) as client:
                token = client.get_token(code.strip(), code_verifier=pending['verifier'], timeout=30, n_retries=0)
            store.save(_with_tokens(pending, token, time.time()))
        pending_path.unlink()


def refresh(path: Path, *, force: bool = False) -> dict:
    with OAuthStore(path).locked() as store:
        state = store.read()
        now = time.time()
        if force or now >= state['refresh_at']:
            with yadisk.Client(state['client_id'], state['client_secret']) as client:
                token = client.refresh_token(state['refresh_token'], timeout=30, n_retries=0)
            state = _with_tokens(state, token, time.time())
            store.save(state)
        return state


def access_token(path: Path) -> str:
    """Future Disk backend must call this instead of caching a token."""
    return refresh(path)['access_token']


def status(path: Path) -> dict:
    if not path.exists():
        return {'status': 'not_configured'}
    with OAuthStore(path).locked() as store:
        state = store.read()
    return {
        'status': 'expired' if time.time() >= state['expires_at'] else 'ready',
        'expires_at': state['expires_at'], 'refresh_at': state['refresh_at'],
    }
