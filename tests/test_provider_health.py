import asyncio
import json
from contextlib import contextmanager

import pytest

from proxy.model_monitor import ModelMonitor
from proxy.provider_health import enrich
from proxy.store import Store
from test_engine import MODEL, config

NOW = 1_800_000_000


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr('proxy.provider_health.time.time', lambda: NOW)
    return Store(tmp_path)


def info():
    return {
        'settings': {'enabled': True, 'targets': [
            {'provider_id': 'a', 'model': 'model', 'reference_model': 'reference'}]},
        'providers': [{'id': 'a', 'enabled': True, 'models': ['model', 'unchecked']},
                      {'id': 'b', 'enabled': True, 'models': ['model']}],
        'states': [], 'blocked_provider_ids': [],
    }


def attempt(store, provider='a', status='error', code=500, ts=NOW-1, **extra):
    store.record_attempt({'provider_id': provider, 'request_id': 'request-1',
                          'ts': ts, 'status': status, 'http_status': code,
                          'error_kind': 'upstream_http_error' if status == 'error' else None,
                          **extra})


def health(store, snapshot=None, provider='a'):
    result = {'providers': [{'id': provider, 'runtime': {'cooldown': True}}]}
    assert enrich(store, snapshot or info(), result) is result
    assert result['providers'][0]['runtime'] == {'cooldown': True}
    return result['providers'][0]['health']


def test_empty_and_unchecked(store):
    result = health(store)
    assert result['last_attempt'] is result['last_error'] is result['last_success_at'] is None
    assert result['recent'] == {'window_seconds': 900, 'attempts': 0, 'errors': 0,
                                'cancelled': 0, 'consecutive_errors': 0}
    assert result['billing'] == {'status': 'unknown', 'observed_at': None, 'code': None}
    assert not result['quarantined']
    assert result['models'] == [
        {'model': 'model', 'reference_model': 'reference', 'status': 'unchecked',
         'latest_status': None, 'checked_at': None, 'error_kind': None},
        {'model': 'unchecked', 'reference_model': None, 'status': 'unchecked',
         'latest_status': None, 'checked_at': None, 'error_kind': None}]


@pytest.mark.parametrize('code', [401, 429, 500])
def test_non_402_is_not_balance_evidence(store, code):
    attempt(store, code=code)
    result = health(store)
    assert result['last_attempt']['http_status'] == code
    assert result['billing']['status'] == 'unknown'
    assert result['models'][0]['status'] == 'unchecked'
    assert not result['quarantined']


def test_402_recovers_on_new_success_or_other_error(store):
    attempt(store, code=402, ts=NOW-10)
    before = health(store)
    assert before['billing'] == {'status': 'payment_required', 'code': 402,
                                  'observed_at': before['last_attempt']['ts']}
    attempt(store, status='success', code=200, ts=NOW-9)
    after = health(store)
    assert after['last_error']['http_status'] == 402
    assert after['billing']['status'] == 'unknown'
    assert after['last_success_at'] == after['last_attempt']['ts']
    assert after['recent']['consecutive_errors'] == 0
    attempt(store, code=401, ts=NOW-8)
    assert health(store)['billing']['status'] == 'unknown'
    attempt(store, code=429, ts=NOW-7)
    assert health(store)['billing']['status'] == 'unknown'


def test_cancelled_is_not_error_and_breaks_streak(store):
    attempt(store, code=402, ts=NOW-3)
    attempt(store, status='cancelled', code=None, ts=NOW-2, error_kind='cancelled')
    result = health(store)
    assert result['last_attempt']['status'] == 'cancelled'
    assert result['last_error']['http_status'] == 402
    assert result['billing']['status'] == 'unknown'
    assert result['recent'] == {'window_seconds': 900, 'attempts': 2, 'errors': 1,
                                'cancelled': 1, 'consecutive_errors': 0}
    attempt(store, ts=NOW-1)
    assert health(store)['recent']['consecutive_errors'] == 1


def test_provider_local_latest_not_global_30_sample(store):
    attempt(store, ts=NOW-100, code=401, request_id='request-a')
    for index in range(45):
        attempt(store, provider='b', ts=NOW-99+index, code=429, request_id=f'b-{index}')
    result = {'providers': [{'id': 'a'}, {'id': 'b'}, {'id': 'empty'}]}
    enrich(store, info(), result)
    a, b, empty = [p['health'] for p in result['providers']]
    assert a['last_attempt']['request_id'] == 'request-a'
    assert a['last_error']['http_status'] == 401
    assert a['recent']['attempts'] == 1
    assert b['last_attempt']['request_id'] == 'b-44'
    assert b['recent']['attempts'] == b['recent']['consecutive_errors'] == 45
    assert empty['last_attempt'] is None


def test_exact_bounded_recent_counts_and_timestamp_ties(store):
    attempt(store, ts=NOW-901)
    attempt(store, ts=NOW-900)
    attempt(store, status='success', code=200, ts=NOW-2)
    attempt(store, ts=NOW-2)
    attempt(store, ts=NOW-2)
    attempt(store, ts=NOW+1, code=402)  # Future observations cannot become current.
    result = health(store)
    assert result['recent'] == {'window_seconds': 900, 'attempts': 4, 'errors': 3,
                                'cancelled': 0, 'consecutive_errors': 2}
    assert result['last_attempt']['http_status'] == 500


@pytest.mark.parametrize('status', ['verified', 'suspect'])
def test_current_reference_only_preserves_conclusive_status(store, status):
    snapshot = info()
    current = {'provider_id': 'a', 'model': 'model', 'reference_model': 'reference',
               'status': status, 'latest_status': 'error', 'checked_at': '2027-01-15T07:59:59Z',
               'latest_result': {'error_kind': 'upstream_timeout', 'raw_payload': 'SECRET'}}
    snapshot['states'] = [current,
        {**current, 'reference_model': 'old-reference', 'status': 'suspect'},
        {**current, 'model': 'manual-nonrouting', 'status': 'suspect'}]
    snapshot['blocked_provider_ids'] = ['a'] if status == 'suspect' else []
    result = health(store, snapshot)
    assert result['models'][0]['status'] == status
    assert result['models'][0]['latest_status'] == 'error'
    assert result['models'][0]['error_kind'] == 'upstream_timeout'
    assert result['quarantined'] == (status == 'suspect')
    current['stale_reference'] = True
    assert health(store, snapshot)['models'][0]['status'] == 'unchecked'
    snapshot['providers'][0]['enabled'] = False
    assert health(store, snapshot)['models'] == []


def test_no_payload_messages_or_secrets_and_no_writes(store, monkeypatch):
    attempt(store, error_detail='SECRET full message https://secret.example sk-secret',
            error_kind='unsafe secret/body', request_id='Bearer secret credential')
    connection = store._connection
    statements = []

    @contextmanager
    def readonly_connection():
        with connection() as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(store, '_connection', readonly_connection)
    result = health(store)
    text = json.dumps(result)
    assert not any(value in text for value in ['SECRET', 'secret', 'payload', 'message', 'Bearer', 'https://'])
    assert result['last_error']['error_kind'] == 'upstream_error'
    assert result['last_attempt']['request_id'] is None
    assert all(not sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE')) for sql in statements)
    assert any('ORDER BY ts DESC,rowid DESC LIMIT 1' in sql for sql in statements)
    with connection() as conn:
        plan = conn.execute('EXPLAIN QUERY PLAN SELECT ts FROM attempts WHERE provider_id=? '
                            'AND ts<=? ORDER BY ts DESC,rowid DESC LIMIT 1', ('a', NOW)).fetchall()
    assert 'attempts_provider_ts' in str([tuple(row) for row in plan])


def test_real_monitor_info_excludes_changed_binding_and_reference(tmp_path, store):
    config(store)
    monitor = ModelMonitor(store, tmp_path)
    p = store.get_config(True)['providers'][0]
    settings = monitor.db.settings()
    monitor.db.save_settings({**settings, 'enabled': True, 'targets': [
        {'provider_id': p['id'], 'model': MODEL, 'reference_model': MODEL}]}, settings['revision'])

    def record(identifier, status, probability, key=None):
        monitor.db.record_result({
            'id': identifier, 'provider_id': p['id'], 'provider_name': p['name'],
            'model': MODEL, 'reference_model': MODEL,
            'configuration_key': key or monitor.binding_key(p, MODEL, MODEL),
            'status': status, 'target_probability': probability,
            'checked_at': NOW-10 if status != 'error' else NOW-1, 'source': 'manual',
            'error_kind': 'upstream_timeout' if status == 'error' else None,
            'duration_ms': 1, 'reference_version': 'test', 'candidates': []})

    record('verified', 'verified', .99)
    record('error', 'error', None)
    snapshot = asyncio.run(monitor.info())
    row = health(store, snapshot, p['id'])['models'][0]
    assert row['status'] == 'verified' and row['latest_status'] == 'error'
    changed = store.get_config()
    changed['providers'][0]['api_key'] = 'new-secret-key'
    store.save_config(changed, changed['revision'])
    assert health(store, asyncio.run(monitor.info()), p['id'])['models'][0]['status'] == 'unchecked'
    # A reference-only change also invalidates historical/manual conclusions.
    current = monitor.db.settings()
    monitor.db.save_settings({**current, 'targets': [
        {'provider_id': p['id'], 'model': MODEL, 'reference_model': 'other-reference'}]}, current['revision'])
    row = health(store, asyncio.run(monitor.info()), p['id'])['models'][0]
    assert row['status'] == 'unchecked' and row['reference_model'] == 'other-reference'
    assert 'new-secret-key' not in json.dumps(row)


def test_a6_configured_provider_enrichment_performs_no_http(tmp_path, store, monkeypatch):
    import httpx

    def forbidden(*args, **kwargs):
        pytest.fail('health enrichment must not make HTTP calls')

    monkeypatch.setattr(httpx.Client, 'send', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)
    config(store)
    current = store.get_config()
    current['providers'][0].update(name='A6 provider', base_url='https://api.a6api.com/v1',
                                    api_key='a6-secret-credential')
    store.save_config(current, current['revision'])
    monitor = ModelMonitor(store, tmp_path)
    attempt(store, provider='1', code=402)
    snapshot = asyncio.run(monitor.info())
    result = store.stats('hour')
    runtime = {'quarantined': False, 'cooldown_remaining_s': 17}
    result['providers'][0]['runtime'] = runtime
    enrich(store, snapshot, result)
    first = result['providers'][0]
    assert first['name'] == 'A6 provider' and first['runtime'] == runtime
    assert first['health']['billing']['status'] == 'payment_required'
    assert first['health']['last_attempt']['http_status'] == 402
    assert first['health']['models'][0]['status'] == 'unchecked'
    assert 'a6-secret-credential' not in json.dumps(result)
