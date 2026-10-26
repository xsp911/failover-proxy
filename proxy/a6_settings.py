"""Encrypted A6API console credentials: PAT and numeric account id.

The PAT is a console credential with account-level power, so it is stored
encrypted with the existing Fernet key and only ever returned masked.
"""
import json
import re

import httpx

PAT = re.compile(r'[A-Za-z0-9+/=_.:\-]{16,512}\Z')
BASE = 'https://a6api.com'


def validate(pat, user_id):
    if not isinstance(pat, str) or not PAT.fullmatch(pat):
        raise ValueError('invalid_a6_pat')
    if isinstance(user_id, bool) or not isinstance(user_id, int) or not 1 <= user_id <= 2_147_483_647:
        raise ValueError('invalid_a6_user_id')
    return pat, user_id


def mask(pat):
    if not isinstance(pat, str) or not pat:
        return ''
    if len(pat) <= 8:
        return '*' * len(pat)
    return pat[:4] + '*' * 8 + pat[-2:]


class A6Settings:
    """Persisted inside the existing encrypted config row; no plaintext column."""
    validate = staticmethod(validate)

    def __init__(self, store):
        self.store = store

    def _raw(self):
        with self.store._connection() as conn:
            row = conn.execute('SELECT payload FROM config WHERE singleton=1').fetchone()
        return json.loads(row['payload'])

    def status(self):
        raw = self._raw()
        encrypted = raw.get('_a6_pat_encrypted')
        user_id = raw.get('a6_user_id')
        pat = self.store._fernet.decrypt(encrypted.encode()).decode() if encrypted else ''
        return {'configured': bool(encrypted and isinstance(user_id, int)),
                'pat_masked': mask(pat), 'has_pat': bool(encrypted),
                'user_id': user_id if isinstance(user_id, int) else None}

    def secret(self):
        raw = self._raw()
        encrypted = raw.get('_a6_pat_encrypted')
        user_id = raw.get('a6_user_id')
        if not encrypted or not isinstance(user_id, int):
            return None, None
        return self.store._fernet.decrypt(encrypted.encode()).decode(), user_id

    def save(self, pat, user_id):
        pat, user_id = validate(pat, user_id)
        encrypted = self.store._fernet.encrypt(pat.encode()).decode()
        with self.store._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT payload FROM config WHERE singleton=1').fetchone()
            raw = json.loads(row['payload'])
            raw['_a6_pat_encrypted'] = encrypted
            raw['a6_user_id'] = user_id
            conn.execute('UPDATE config SET payload=? WHERE singleton=1', (json.dumps(raw, ensure_ascii=False),))
        return self.status()

    def clear(self):
        with self.store._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT payload FROM config WHERE singleton=1').fetchone()
            raw = json.loads(row['payload'])
            raw.pop('_a6_pat_encrypted', None)
            raw.pop('a6_user_id', None)
            conn.execute('UPDATE config SET payload=? WHERE singleton=1', (json.dumps(raw, ensure_ascii=False),))
        return self.status()


async def verify(pat, user_id, transport=None):
    """Read-only check: whoami plus token count. Never creates or removes pins."""
    headers = {'Authorization': 'Bearer ' + pat, 'New-Api-User': str(user_id)}
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=httpx.Timeout(15, connect=10), trust_env=False,
                                     transport=transport, follow_redirects=False) as client:
            who = await client.get('/api/user/self', headers=headers)
            if who.status_code == 401:
                return {'ok': False, 'status': 401, 'error_kind': 'a6_auth_rejected'}
            if who.status_code != 200:
                return {'ok': False, 'status': who.status_code, 'error_kind': 'a6_unexpected_status'}
            try:
                data = who.json().get('data') or {}
            except ValueError:
                return {'ok': False, 'status': who.status_code, 'error_kind': 'a6_invalid_response'}
            actual = data.get('id')
            if actual != user_id:
                return {'ok': False, 'status': 200, 'error_kind': 'a6_user_id_mismatch'}
            tokens = await client.get('/api/token/?p=1&size=100', headers=headers)
            count = None
            if tokens.status_code == 200:
                try:
                    count = (tokens.json().get('data') or {}).get('total')
                except ValueError:
                    count = None
            return {'ok': True, 'status': 200, 'user_id': actual,
                    'display_name': str(data.get('display_name') or '')[:100],
                    'tokens_total': count if isinstance(count, int) else None}
    except (httpx.HTTPError, TimeoutError):
        return {'ok': False, 'status': None, 'error_kind': 'a6_connection_failed'}
