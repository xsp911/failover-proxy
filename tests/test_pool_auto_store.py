"""Automatic maintenance persistence; isolated SQLite only, no paid traffic."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import stat

import pytest

from proxy.pool_auto_store import AutoStore, PoolAutoStore
from proxy.pool_store import PoolStore


def policy(**changes):
    return {'name': 'Auto pool', 'token_id': 11, 'test_token_id': 22,
            'model': 'claude-opus-4', 'reference_model': 'claude-opus-4',
            'max_input_usd': 2, 'max_output_usd': 10,
            'max_cache_read_usd': .1, 'max_cache_write_usd': 3, **changes}


@pytest.fixture
def store(tmp_path):
    return PoolAutoStore(tmp_path / 'private')


def test_policy_auto_opt_in_defaults_and_enabled_requirement(tmp_path):
    db = PoolStore(tmp_path)
    row = db.save(policy())
    assert (row['auto_publish'], row['auto_daily_usd'], row['auto_daily_requests']) == (False, 5.0, 600)
    with pytest.raises(ValueError, match='auto_pool_must_be_enabled'):
        db.save(policy(id=row['id'], auto_publish=True), row['revision'])
    changed = db.save(policy(id=row['id'], enabled=True, auto_publish=True,
                             auto_daily_usd='0.001', auto_daily_requests=3), row['revision'])
    assert changed['auto_publish'] and changed['enabled']
    assert changed['auto_daily_usd'] == .001 and changed['auto_daily_requests'] == 3


@pytest.mark.parametrize('key,value', [
    ('auto_daily_usd', True), ('auto_daily_usd', 'NaN'), ('auto_daily_usd', 'Infinity'),
    ('auto_daily_usd', '0.000999'), ('auto_daily_usd', '100.000001'),
    ('auto_daily_usd', '0.0010001'), ('auto_daily_usd', None),
    ('auto_daily_requests', True), ('auto_daily_requests', 2),
    ('auto_daily_requests', 10001), ('auto_daily_requests', '600'),
    ('auto_publish', 1),
])
def test_policy_invalid_auto_limits(tmp_path, key, value):
    with pytest.raises(ValueError, match='invalid_' + key):
        PoolStore(tmp_path).save(policy(**{key: value}))


def test_legacy_policy_defaults_no_rewrite_or_runtime_loss(tmp_path):
    db = PoolStore(tmp_path)
    row = db.save(policy())
    db.update_runtime(row['id'], 1, status='published', last_error='known_safe_error')
    with db.connection() as c:
        raw = json.loads(c.execute('SELECT policy FROM pools').fetchone()[0])
        for field in ('auto_publish', 'auto_daily_usd', 'auto_daily_requests'):
            raw.pop(field)
        legacy = json.dumps(raw)
        c.execute('UPDATE pools SET policy=? WHERE id=?', (legacy, row['id']))
    reopened = PoolStore(tmp_path)
    hydrated = reopened.get(row['id'])
    assert (hydrated['auto_publish'], hydrated['auto_daily_usd'], hydrated['auto_daily_requests']) == (False, 5.0, 600)
    assert hydrated['status'] == 'published' and hydrated['last_error'] == 'known_safe_error'
    assert hydrated['revision'] == row['revision'] and hydrated['updated_at'] == row['updated_at']
    with reopened.connection() as c:
        assert c.execute('SELECT policy FROM pools').fetchone()[0] == legacy


def test_private_database_reopen_and_symlink_guard(tmp_path):
    db = PoolAutoStore(tmp_path / 'private')
    assert AutoStore is PoolAutoStore
    assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.path.parent.stat().st_mode) == 0o700
    with db.connection() as c:
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        for suffix in ('-wal', '-shm'):
            sidecar = db.path.with_name(db.path.name + suffix)
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    db.configure('pool1', True, 100)
    assert PoolAutoStore(db.path.parent).state('pool1') == db.state('pool1')
    link = tmp_path / 'link'
    link.symlink_to(db.path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match='unsafe_data_directory'):
        PoolAutoStore(link)
    folder = tmp_path / 'unsafe'
    folder.mkdir()
    (folder / 'a6_pool_auto.db').symlink_to(db.path)
    with pytest.raises(OSError):
        PoolAutoStore(folder)


def test_schedule_idempotence_and_disable_preserves_other_state(store):
    assert store.state('pool1')['next_refresh_at'] is None
    first = store.configure('pool1', True, 100)
    assert first['next_refresh_at'] == 100 and first['next_audit_at'] == 3700
    assert store.configure('pool1', True, 200) == first
    assert store.claim_due('pool1', 'refresh', 200)
    assert store.reserve('pool1', 5, 600, '.1', 200)
    store.outcome('pool1', 1, 'suspect', None, 200)
    disabled = store.configure('pool1', False, 201)
    assert disabled['next_refresh_at'] is None and disabled['next_audit_at'] is None
    assert disabled['lease_until'] == 7400 and disabled['daily_requests'] == 1
    assert store.list_records('pool1')[0]['failures'] == 1
    assert store.configure('pool1', False, 300) == disabled
    assert not store.claim_due('pool1', 'refresh', 10000)
    store.finish('pool1', 'refresh', now=301)
    enabled = store.configure('pool1', True, 400)
    assert enabled['next_refresh_at'] == 400 and enabled['next_audit_at'] == 4000


def test_atomic_claim_pool_wide_lease_and_no_catchup(store):
    store.configure('pool1', True, 0)
    with ThreadPoolExecutor(max_workers=10) as executor:
        claimed = list(executor.map(lambda _: store.claim_due('pool1', 'refresh', 100000), range(10)))
    assert sum(claimed) == 1
    state = store.state('pool1')
    assert state['next_refresh_at'] == 121600 and state['lease_until'] == 107200
    assert state['refresh_lease_until'] == 107200 and state['audit_lease_until'] is None
    assert not store.claim_due('pool1', 'audit', 100001)
    assert store.claim_due('pool1', 'audit', 107200)
    assert store.state('pool1')['next_audit_at'] == 110800
    # Finishing a different operation must not clear the current lease.
    store.finish('pool1', 'refresh', now=107201)
    assert store.state('pool1')['lease_kind'] == 'audit'
    finished = store.finish('pool1', 'audit', 'Bearer secret content', now=107202)
    assert finished['lease_until'] is None and finished['last_error'] == 'auto_operation_failed'
    assert finished['last_audit_at'] == finished['last_finished_at'] == 107202
    assert not store.claim_due('pool1', 'audit', 107203)


def test_reservation_exact_atomic_no_refund_and_utc_rollover(store):
    now = datetime(2026, 10, 23, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    with ThreadPoolExecutor(max_workers=20) as executor:
        reservations = list(executor.map(lambda _: store.reserve('pool1', '.001', 10000, Decimal('.0001'), now), range(30)))
    assert sum(reservations) == 10
    assert store.state('pool1')['daily_requests'] == 10
    assert store.state('pool1')['daily_reserved_usd'] == .001
    store.finish('pool1', 'refresh', 'probe_failed', now=now)
    assert not store.reserve('pool1', '.001', 10000, '.000001', now)
    assert store.reserve('pool1', '.001', 3, 0, now + 1)
    assert store.state('pool1')['day'] == '2026-10-24'
    assert store.state('pool1')['daily_requests'] == 1
    assert store.reserve('pool1', '.001', 3, 0, now + 1)
    assert store.reserve('pool1', '.001', 3, 0, now + 1)
    assert not store.reserve('pool1', '.001', 3, 0, now + 1)
    assert store.state('pool1')['daily_requests'] == 3
    assert store.state('pool1')['daily_reserved_usd'] == 0


@pytest.mark.parametrize('reason', [
    'probe_budget_exhausted', 'auto_daily_budget_exhausted', 'cancelled',
    'channel_attribution_mismatch', 'targeted_probe_channel_mismatch', 'a6_http_500',
    'consume_log_missing', 'test_route_cleanup_failed', 'probe_timeout_or_network',
    'probe_network_error', 'totally_unknown',
])
def test_infrastructure_unknown_does_not_count(store, reason):
    store.outcome('pool1', 1, 'suspect', None, 1)
    for now in range(2, 7):
        record = store.outcome('pool1', 1, 'unknown', reason, now)
    assert record['failures'] == 1 and not record['banned']


@pytest.mark.parametrize('reason', [
    'upstream_sse_error', 'invalid_finish_reason', 'probe_insufficient_numbers',
    'targeted_probe_http_400', 'targeted_probe_http_429', 'targeted_probe_http_500',
    'upstream_http_503', 'upstream_timeout', 'upstream_connection',
])
def test_four_consecutive_failures_ban_and_explicit_unban(store, reason):
    for index in range(1, 5):
        record = store.outcome('pool1', 1, 'unknown', reason, index, 'run_' + str(index))
        assert record['failures'] == index
        assert record['banned'] == (index == 4)
    assert store.is_banned('pool1', 1) and not store.is_banned('pool2', 1)
    verified = store.outcome('pool1', 1, 'verified', None, 5, 'verified_run')
    assert verified['failures'] == 0 and verified['banned']
    assert store.unban('pool1', 1) and not store.is_banned('pool1', 1)
    assert not store.unban('pool1', 999)


def test_verified_resets_streak_and_run_id_is_persistent(store):
    for index in range(3):
        store.outcome('pool1', 1, 'suspect', 'low_model_probability', index)
    assert store.outcome('pool1', 1, 'verified', None, 4)['failures'] == 0
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(lambda _: store.outcome('pool1', 1, 'suspect', None, 5, 'same_run'), range(10)))
    reopened = PoolAutoStore(store.path.parent)
    assert reopened.outcome('pool1', 1, 'suspect', None, 6, 'same_run')['failures'] == 1
    assert reopened.outcome('pool1', 2, 'suspect', None, 6, 'same_run')['failures'] == 1
    assert reopened.list_records('pool1')[0]['channel_id'] == 1
    assert reopened.list_records('pool1')[1]['channel_id'] == 2


def test_ownership_roundtrip_allowlist_uncertain_and_budget_independence(store):
    assert store.remote('pool1') is None
    draft = {'channels': [{'channel_id': 4, 'priority': None, 'api_key': 'secret'}],
             'caps': {'input_price_micros': 2000000, 'output_price_micros': 3000000,
                      'cache_read_price_micros': 0, 'cache_write_price_micros': 0,
                      'per_request_price_micros': 0, 'token': 'secret'},
             'sort_strategy': 'price_asc', 'user_priorities': {},
             'auto_pool_enabled': False, 'fallback_enabled': False,
             'min_ratio': '', 'max_ratio': '', 'min_cache_hit_rate_bps': None,
             'Authorization': 'Bearer secret'}
    result = store.save_remote('pool1', 123, 7, draft, 'owned')
    assert result['subscription_id'] == 123 and result['config_version'] == 7
    assert 'secret' not in json.dumps(result)
    reopened = PoolAutoStore(store.path.parent)
    assert reopened.remote('pool1') == result
    uncertain = reopened.save_remote('pool1', None, None, result['draft'], 'uncertain')
    assert uncertain['state'] == 'uncertain'
    reopened.configure('pool1', False, 100)
    assert reopened.remote('pool1') == uncertain


@pytest.mark.parametrize('action', [
    lambda db: db.configure('pool1', 1, 0),
    lambda db: db.configure('pool1', True, float('nan')),
    lambda db: db.claim_due('pool1', 'arbitrary_sql', 0),
    lambda db: db.reserve('pool1', 5, 600, '-.001', 0),
    lambda db: db.reserve('pool1', 5, 600, 'NaN', 0),
    lambda db: db.reserve('pool1', 5, 600, '.0000001', 0),
    lambda db: db.outcome('pool1', True, 'suspect', None, 0),
    lambda db: db.outcome('pool1', 1, 'invalid status', None, 0),
    lambda db: db.save_remote('pool1', 1, True, {}, 'owned'),
    lambda db: db.save_remote('pool1', 1, 0, {'caps': {'input_price_micros': -1}}, 'owned'),
])
def test_invalid_inputs_rejected(store, action):
    with pytest.raises(ValueError):
        action(store)
