import copy
import json
import sqlite3

import pytest

from proxy.config import validate_config
from proxy.outbound_proxy import normalize_profiles, profile_for, proxy_url, resolve_proxy
from proxy.store import Store


def profile(**overrides):
    return {'id': 'shared', 'name': 'Shared', 'scheme': 'http', 'host': '127.0.0.1',
            'port': 8080, 'username': 'name', 'password': 'proxy-secret-unique', **overrides}


def config(store, **overrides):
    return {**store.get_config(), 'outbound_proxies': [profile()], **overrides}


def raw(store):
    with sqlite3.connect(store.db_path) as conn:
        return json.loads(conn.execute('SELECT payload FROM config').fetchone()[0])


def test_encryption_masking_preserve_by_id_and_clear(tmp_path):
    store = Store(tmp_path)
    incoming = config(store)
    original = copy.deepcopy(incoming)
    public = store.save_config(incoming, 0)
    assert incoming == original
    stored = raw(store)['outbound_proxies'][0]
    assert 'password' not in stored and stored['_password_encrypted']
    assert 'proxy-secret-unique' not in json.dumps(raw(store))
    assert public['outbound_proxies'][0]['has_password'] is True
    assert 'password' not in public['outbound_proxies'][0]
    assert '_password_encrypted' not in public['outbound_proxies'][0]
    assert store.get_config(True)['outbound_proxies'][0]['password'] == 'proxy-secret-unique'
    public['outbound_proxies'][0]['password'] = ''
    public['outbound_proxies'][0]['host'] = 'localhost'
    public = store.save_config(public, 1)
    assert store.get_config(True)['outbound_proxies'][0]['password'] == 'proxy-secret-unique'
    public['outbound_proxies'][0]['clear_password'] = True
    public = store.save_config(public, 2)
    assert not public['outbound_proxies'][0]['has_password']
    assert store.get_config(True)['outbound_proxies'][0]['password'] == ''
    assert '_password_encrypted' not in raw(store)['outbound_proxies'][0]


def test_password_not_copied_to_different_profile_id(tmp_path):
    store = Store(tmp_path)
    public = store.save_config(config(store), 0)
    public['outbound_proxies'][0]['id'] = 'replacement'
    store.save_config(public, 1)
    assert store.get_config(True)['outbound_proxies'][0]['password'] == ''


def test_missing_profiles_preserved_on_legacy_save(tmp_path):
    store = Store(tmp_path)
    assert 'outbound_proxies' not in store.get_config()
    assert 'outbound_proxies' not in validate_config(store.get_config())
    public = store.save_config(config(store), 0)
    del public['outbound_proxies']
    store.save_config(public, 1)
    assert store.get_config(True)['outbound_proxies'][0]['password'] == 'proxy-secret-unique'


def test_provider_reference_and_transaction_rollback(tmp_path):
    store = Store(tmp_path)
    value = config(store)
    value['providers'] = [{'id': 'p', 'name': 'P', 'priority': 1, 'enabled': True,
                           'base_url': 'https://example.org', 'proxy_id': 'shared'}]
    clean = validate_config(value)
    assert clean['providers'][0]['proxy_id'] == 'shared'
    public = store.save_config(clean, 0)
    public['outbound_proxies'] = []
    with pytest.raises(ValueError, match='^invalid_proxy_reference$'):
        validate_config(public)
    with pytest.raises(ValueError, match='^invalid_proxy_reference$'):
        store.save_config(public, 1)
    assert store.get_config()['revision'] == 1
    public['providers'][0]['proxy_id'] = None
    store.save_config(public, 1)
    assert store.get_config()['outbound_proxies'] == []


def test_separate_a6_reference_preserved_and_deletion_protected(tmp_path):
    store = Store(tmp_path)
    public = store.save_config(config(store), 0)
    payload = raw(store)
    payload['_a6_proxy_id'] = 'shared'
    with sqlite3.connect(store.db_path) as conn:
        conn.execute('UPDATE config SET payload=?', (json.dumps(payload),))
    assert '_a6_proxy_id' not in store.get_config(True)
    public = store.save_config(public, 1)
    assert raw(store)['_a6_proxy_id'] == 'shared'
    public['outbound_proxies'] = []
    public['_a6_proxy_id'] = None
    with pytest.raises(ValueError, match='^proxy_referenced_by_a6$'):
        store.save_config(public, 2)
    assert store.get_config()['revision'] == 2


@pytest.mark.parametrize('updates', [
    {'scheme': 'ftp'}, {'port': True}, {'port': 0}, {'port': 65536}, {'port': '80'},
    {'host': 'http://user:secret@host'}, {'host': 'a/b'}, {'host': 'evil@localhost'},
    {'host': 'localhost\n'}, {'host': 'fe80::1%eth0'}, {'username': None},
    {'password': 'secret\n'}, {'password': '\ud800'}, {'username': '\udfff'},
    {'clear_password': 'yes'}, {'unexpected': 'secret'},
    {'clear_password': True}, {'enabled': 1}, {'host': '[localhost]'}, {'host': 'host..'},
])
def test_strict_profiles_and_sanitized_errors(updates):
    with pytest.raises(ValueError) as error:
        normalize_profiles([profile(**updates)])
    assert 'secret' not in str(error.value)


def test_duplicate_and_non_object_profiles():
    for value in (None, {}, [None], [profile(), profile()]):
        with pytest.raises(ValueError):
            normalize_profiles(value)


@pytest.mark.parametrize('scheme', ['http', 'https', 'socks5', 'socks5h'])
def test_ipv6_credentials_and_local_proxy_allowed(scheme):
    p = profile(scheme=scheme, host='[::1]', username='a:@/ ü', password='p:@/?#% ü')
    assert proxy_url(p) == f'{scheme}://a%3A%40%2F%20%C3%BC:p%3A%40%2F%3F%23%25%20%C3%BC@[::1]:8080'
    assert normalize_profiles([p])[0]['host'] == '::1'
    assert proxy_url(profile(host='localhost', username='', password='')) == 'http://localhost:8080'


def test_resolution_fails_closed_and_masked_secret_unavailable():
    cfg = {'outbound_proxies': [profile()]}
    assert resolve_proxy is profile_for
    assert resolve_proxy(cfg, None) is None
    assert resolve_proxy(cfg, '') is None
    assert resolve_proxy(cfg, 'shared')['id'] == 'shared'
    for reference in ('missing', True, []):
        with pytest.raises(ValueError):
            resolve_proxy(cfg, reference)
    with pytest.raises(ValueError, match='^proxy_disabled$'):
        resolve_proxy({'outbound_proxies': [profile(enabled=False)]}, 'shared')
    masked = profile(has_password=True)
    del masked['password']
    with pytest.raises(ValueError, match='^proxy_password_unavailable$'):
        resolve_proxy({'outbound_proxies': [masked]}, 'shared')


def test_api_access_validation_and_persistence(tmp_path):
    store = Store(tmp_path)
    value = store.get_config()
    value['api_access'] = {'enabled': True, 'allowed_ips': ['192.168.1.14/24']}
    public = store.save_config(validate_config(value), 0)
    assert public['api_access'] == {'enabled': True, 'allowed_ips': ['192.168.1.0/24']}
    del public['api_access']
    store.save_config(public, 1)
    assert store.get_config()['api_access']['enabled'] is True
