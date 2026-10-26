"""Pool policy isolation, CAS, money parsing and evidence redaction."""
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from proxy.pool_store import PoolStore


def policy(**changes):
    return {'name': 'Claude pool', 'token_id': 11, 'test_token_id': 22,
            'model': 'claude-opus-4', 'reference_model': 'claude-opus-4',
            'max_input_usd': '2.000001', 'max_output_usd': 10.5,
            'max_cache_read_usd': '0.1', 'max_cache_write_usd': 3, **changes}


def candidate(**changes):
    return {'channel_id': 33, 'listing_id': 44, 'model_name': 'claude-opus-4',
            'supplier': 'Supplier', 'configuration_key': 'a' * 64,
            'verification_status': 'verified', 'probability': .98,
            'verified_at': '2026-10-23T10:00:00Z', 'actual_channel_id': 33,
            'request_ids': ['request-123'], 'reference_version': 'abc123', **changes}


@pytest.fixture
def store(tmp_path):
    return PoolStore(tmp_path / 'private')


def test_private_database_defaults_and_reopen(tmp_path):
    folder = tmp_path / 'private'
    folder.mkdir(mode=0o755)
    db = PoolStore(folder)
    row = db.save(policy())
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
    assert db.path.name == 'a6_pools.db'
    with db.connection() as c:
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        for sidecar in (folder / 'a6_pools.db-wal', folder / 'a6_pools.db-shm'):
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    assert row['revision'] == 1
    assert row['enabled'] is False and row['auto_publish'] is False
    assert row['verification_ttl_hours'] == 24
    assert row['threshold_percent'] == 90
    assert row['max_probe_requests'] == 60
    assert row['candidates'] == row['members'] == []
    assert row['status'] == 'draft'
    assert PoolStore(folder).get(row['id']) == row


def test_policy_revision_conflict_and_unique_alias(store):
    first = store.save(policy())
    with pytest.raises(ValueError, match='duplicate_token_model'):
        store.save(policy(name='Other name'))
    distinct = store.save(policy(model='other', test_token_id=23))
    with pytest.raises(ValueError, match='duplicate_token_model'):
        store.save(policy(id=distinct['id']), distinct['revision'])
    assert store.get(distinct['id'])['model'] == 'other'
    for revision in (None, True, 0, 2):
        with pytest.raises(ValueError):
            store.save(policy(id=first['id']), revision)
    updated = store.save(policy(id=first['id'], name='Renamed'), first['revision'])
    assert updated['revision'] == 2 and updated['name'] == 'Renamed'
    with pytest.raises(ValueError, match='revision_conflict'):
        store.update_runtime(first['id'], 1, status='ready')
    with pytest.raises(ValueError, match='revision_conflict'):
        store.delete(first['id'], 1)
    assert store.delete(first['id'], 2) is True
    assert store.delete(first['id'], 2) is False
    assert store.get(first['id']) is None


@pytest.mark.parametrize('bad', [True, False, 'NaN', 'sNaN', 'Infinity', '-Infinity', float('nan'),
                                 float('inf'), -1, '-0.000001', 1000.000001, '0.0000001', [], {}])
@pytest.mark.parametrize('field', ['max_input_usd', 'max_output_usd', 'max_cache_read_usd', 'max_cache_write_usd'])
def test_invalid_prices(store, field, bad):
    with pytest.raises(ValueError, match='invalid_' + field):
        store.save(policy(**{field: bad}))
    assert store.list() == []


def test_price_precision_and_limits(store):
    row = store.save(policy(max_input_usd='1.0000000', max_output_usd='1e-6',
                            max_cache_read_usd=0, max_cache_write_usd='1000'))
    assert row['max_input_usd'] == 1.0
    assert row['max_output_usd'] == .000001
    assert isinstance(row['max_cache_read_usd'], float)
    assert row['max_cache_write_usd'] == 1000.0


@pytest.mark.parametrize('changes', [
    {'name': ''}, {'name': 'x' * 101}, {'name': ' '}, {'name': 'a\nb'},
    {'token_id': True}, {'token_id': 0}, {'token_id': '1'}, {'test_token_id': None},
    {'test_token_id': 11}, {'model': 'model name'}, {'model': 'model\n'},
    {'reference_model': 'x' * 201}, {'protocol': 'http'}, {'enabled': 1},
    {'auto_publish': True}, {'auto_publish': 'false'}, {'max_probe_usd': .0001},
    {'max_probe_usd': 101}, {'max_probe_requests': 2}, {'max_candidates': 101},
    {'max_members': 21}, {'refresh_minutes': 4}, {'ttl_hours': 169},
    {'verification_ttl_hours': 0}, {'ttl_hours': 2, 'verification_ttl_hours': 3},
    {'min_cache_hit_percent': -1}, {'min_success_percent': 101}, {'threshold_percent': 49},
    {'threshold_percent': float('nan')}, {'min_success_percent': True},
])
def test_policy_validation(store, changes):
    with pytest.raises(ValueError):
        store.save(policy(**changes))
    assert store.list() == []


def test_policy_secret_and_runtime_input_not_retained(store):
    row = store.save(policy(api_key='sk-secret', token='secret', pat='source-secret',
                            candidates=[{'response': 'sensitive'}], status='published',
                            headers={'Authorization': 'secret'}, ttl_hours=48))
    assert row['verification_ttl_hours'] == 48
    assert row['status'] == 'draft' and row['candidates'] == []
    blob = json.dumps(row)
    assert 'secret' not in blob and 'api_key' not in blob and 'Authorization' not in blob
    with store.connection() as c:
        assert 'secret' not in c.execute('SELECT policy FROM pools').fetchone()[0]


def test_runtime_no_revision_advance_and_allowlist(store):
    row = store.save(policy())
    updated = store.update_runtime(row['id'], 1, status='ready',
                                   candidates=[candidate(api_key='secret', response='private')],
                                   last_error='probe_timeout')
    assert updated['revision'] == 1
    assert updated['updated_at'] == row['updated_at']
    assert updated['max_input_usd'] == row['max_input_usd']
    assert 'api_key' not in updated['candidates'][0]
    assert 'response' not in updated['candidates'][0]
    assert updated['last_error'] == 'probe_timeout'
    with pytest.raises(ValueError, match='invalid_runtime_field'):
        store.update_runtime(row['id'], 1, model='other')
    with pytest.raises(ValueError):
        store.update_runtime(row['id'], 1, last_error='Bearer secret value')
    with pytest.raises(ValueError):
        store.update_runtime(row['id'], 1, candidates=[candidate()] * 101)
    with pytest.raises(ValueError):
        store.update_runtime(row['id'], 1, members=[candidate()] * 21)
    with pytest.raises(ValueError):
        store.update_runtime(row['id'], 1, candidates=[candidate(probability=float('nan'))])
    assert store.get(row['id']) == updated


def test_verification_revision_fingerprint_binding(store):
    row = store.save(policy())
    record = store.save_verification(row['id'], 1, candidate(response='private', key='secret',
                                      rounds=[{'round': 1, 'probability': .98, 'output': 'private'}]))
    assert record['revision'] == 1 and record['pool_id'] == row['id']
    assert record == store.verification(row['id'], 33)
    assert 'private' not in json.dumps(record) and 'secret' not in json.dumps(record)
    assert record['rounds'] == [{'round': 1, 'probability': .98}]
    for changes in ({'configuration_key': None}, {'configuration_key': 'bad'},
                    {'model_name': 'other'}, {'channel_id': True}, {'probability': None}):
        with pytest.raises(ValueError):
            store.save_verification(row['id'], 1, candidate(**changes))
    updated = store.save(policy(id=row['id']), 1)
    assert updated['revision'] == 2
    assert store.verification(row['id'], 33) is None
    with pytest.raises(ValueError, match='revision_conflict'):
        store.save_verification(row['id'], 1, candidate())


def test_published_delete_blocked_after_policy_edit(store):
    row = store.save(policy())
    store.update_runtime(row['id'], 1, status='published', members=[candidate()],
                         last_sync_at='2026-10-23T10:00:00Z')
    with pytest.raises(ValueError, match='published_pool_requires_explicit_detach'):
        store.delete(row['id'], 1)
    edited = store.save(policy(id=row['id'], name='Changed'), 1)
    assert edited['last_sync_at'] and edited['members']
    with pytest.raises(ValueError, match='published_pool_requires_explicit_detach'):
        store.delete(row['id'], 2)


def test_events_sanitized_filter_and_limit(store):
    first = store.save(policy())
    second = store.save(policy(model='other', test_token_id=23))
    event = store.append_event(first['id'], 'verification_changed',
                               {'channel_id': 33, 'from_status': 'unknown', 'to_status': 'verified',
                                'raw_response': 'private', 'api_key': 'secret'})
    store.append_event(second['id'], 'discovered', {})
    assert len(store.events()) == 2
    assert store.events(first['id']) == [event]
    assert len(store.events(limit=1)) == 1
    assert 'private' not in json.dumps(event) and 'secret' not in json.dumps(event)
    with pytest.raises(ValueError):
        store.append_event(first['id'], 'bad event with secret', {})
    with pytest.raises(ValueError):
        store.events(limit=True)


def test_atomic_concurrent_updates(store):
    row = store.save(policy())
    def update(name):
        try:
            return store.save(policy(id=row['id'], name=name), 1)['revision']
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(update, ['One', 'Two']))
    assert sorted(map(str, result)) == ['2', 'revision_conflict']


@pytest.mark.parametrize('changes', [{'test_token_id': 22}, {'token_id': 99, 'test_token_id': 11}, {'token_id': 22, 'test_token_id': 99}])
def test_test_token_cannot_overlap_other_pool(store, changes):
    store.save(policy())
    with pytest.raises(ValueError, match='test_token_in_use'):
        store.save(policy(model='other', **changes))
    assert len(store.list()) == 1


def test_same_work_token_separate_models_allowed(store):
    store.save(policy())
    row = store.save(policy(model='other', test_token_id=23))
    assert row['token_id'] == 11
    assert len(store.list()) == 2


def test_pool_cap(store):
    for number in range(100):
        store.save(policy(model='model-' + str(number), test_token_id=1000 + number))
    with pytest.raises(ValueError, match='pool_limit_reached'):
        store.save(policy(model='overflow'))
    assert len(store.list()) == 100
