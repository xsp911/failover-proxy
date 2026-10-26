import json
import os
import sqlite3
import stat

import pytest

from proxy.capability_store import CapabilityStore, RETENTION_SECONDS, routing_fingerprint


NOW = 1_800_000_000


def record(store, **kwargs):
    fields = dict(provider_id='provider-1', model='org/model:latest', protocol='openai', request_id='req-1', now=NOW)
    fields.update(kwargs)
    return store.record(**fields)


def test_passive_evidence_accepted_is_not_output_or_limit(tmp_path):
    store = CapabilityStore(tmp_path)
    row = record(store, requested_output_tokens=32000, observed_output_tokens=12, reasoning_tokens=4,
                 http_status=200, finish_reason='length', outcome='error', error_kind='output_length',
                 context={'estimated_input_tokens': 100, 'estimated_total_tokens': 32100,
                          'context_length': 16000, 'method': 'chars_div_4', 'over_limit': True, 'reliability': 'heuristic'})
    assert row['parameter_accepted'] is True
    assert row['observed_output_tokens'] == 12
    record(store, request_id='req-2', requested_output_tokens=64000, observed_output_tokens=5, http_status=400, outcome='error')
    record(store, request_id='req-3', observed_output_tokens=20, http_status=200)
    summary = store.summary(now=NOW)[0]
    assert summary['max_requested_accepted'] == 32000
    assert summary['max_observed_output'] == 20
    assert summary['status_counts'] == {'200': 2, '400': 1}
    assert 'ceiling' not in json.dumps(summary) and 'limit' not in summary
    assert store.observations(now=NOW)[0]['request_id'] == 'req-3'


def test_dedup_updates_data_preserves_id_and_sources_are_separate(tmp_path):
    store = CapabilityStore(tmp_path)
    first = record(store, http_status=200, requested_output_tokens=10)
    second = record(store, http_status=200, requested_output_tokens=20, observed_output_tokens=8, now=NOW + 1)
    assert first['id'] == second['id']
    assert len(store.observations(now=NOW + 1)) == 1
    assert store.observations(now=NOW + 1)[0]['observed_output_tokens'] == 8
    record(store, source='pelican')
    record(store, provider_id='provider-2')
    assert len(store.observations(provider_id='provider-1', now=NOW + 1)) == 2
    assert len(store.summary(now=NOW + 1)) == 3


@pytest.mark.parametrize('field,value', [
    ('provider_id', 'https://secret.example/key'), ('request_id', 'secret\nkey'),
    ('model', 'https://host/model'), ('model', 'raw prompt text'), ('protocol', 'x' * 161),
    ('error_kind', 'Error with secret sk-test'), ('finish_reason', {'api_key': 'raw'}),
    ('requested_output_tokens', True), ('observed_output_tokens', -1), ('reasoning_tokens', 1.5),
    ('http_status', True), ('http_status', 600), ('outcome', 'unknown'), ('source', 'active_probe'),
    ('routing_fingerprint', 'api-key'), ('context', {'prompt': 'secret'}),
    ('context', {'response': 'secret'}), ('context', {'api_key': 'secret'}),
    ('context', {'over_limit': 'true'}), ('context', {'method': 'https://secret'}),
    ('context', {'reliability': 'verified'}), ('context', {'estimated_input_tokens': float('inf')}),
    ('now', float('nan')),
])
def test_invalid_metadata_never_persists(tmp_path, field, value):
    store = CapabilityStore(tmp_path)
    with pytest.raises(ValueError):
        record(store, **{field: value})
    assert store.observations(now=NOW) == []


def test_routing_reference_is_hash_only_and_grouped(tmp_path):
    store = CapabilityStore(tmp_path)
    secret = {'url': 'https://private.example', 'api_key': 'sk-do-not-retain', 'reference_model': 'org/reference'}
    fingerprint = routing_fingerprint(secret)
    assert len(fingerprint) == 64 and fingerprint == routing_fingerprint(dict(reversed(list(secret.items()))))
    record(store, routing_fingerprint=fingerprint, source='fingerprint')
    record(store, request_id='req-2', routing_fingerprint='b' * 64, source='fingerprint')
    assert len(store.summary(now=NOW)) == 2
    assert all(row['routing_fingerprint'] in (fingerprint, 'b' * 64) for row in store.observations(now=NOW))
    assert b'sk-do-not-retain' not in store.path.read_bytes()
    assert b'private.example' not in store.path.read_bytes()


def test_retention_on_reads_and_records(tmp_path):
    store = CapabilityStore(tmp_path)
    record(store, now=NOW - RETENTION_SECONDS, request_id='boundary')
    record(store, now=NOW - RETENTION_SECONDS - 1, request_id='expired')
    assert [r['request_id'] for r in store.observations(now=NOW)] == ['boundary']
    assert store.summary(now=NOW + 1) == []
    record(store, now=NOW - RETENTION_SECONDS - 1)
    record(store, request_id='new')
    assert [r['request_id'] for r in store.observations(now=NOW)] == ['new']


def test_row_bound_newest_limit_and_validated_filters(tmp_path):
    store = CapabilityStore(tmp_path)
    template = record(store)
    template.pop('id')
    with sqlite3.connect(store.path) as c:
        c.executemany('INSERT INTO observations(ts,request_id,source,provider_id,model,payload) VALUES(?,?,?,?,?,?)',
                      ((NOW, 'bulk-' + str(i), 'gateway', 'provider-1', 'model', json.dumps(template)) for i in range(10020)))
    record(store, request_id='newest', now=NOW + 1)
    with sqlite3.connect(store.path) as c:
        assert c.execute('SELECT count(*) FROM observations').fetchone()[0] == 10000
    rows = store.observations(now=NOW + 1)
    assert len(rows) == 100 and rows[0]['request_id'] == 'newest'
    for limit in (True, 0, 101, -1):
        with pytest.raises(ValueError):
            store.observations(limit=limit)
    with pytest.raises(ValueError):
        store.observations(provider_id='bad\nprovider')


def test_database_and_root_private_permissions(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o755)
    store = CapabilityStore(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    os.chmod(store.path, 0o644)
    CapabilityStore(root)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


@pytest.mark.parametrize('target', ['root', 'database', 'journal', 'wal', 'shm', 'ancestor'])
def test_symlink_rejected_without_touching_target(tmp_path, target):
    real = tmp_path / 'real'
    real.mkdir()
    sentinel = real / 'sentinel'
    sentinel.write_text('unchanged')
    root = tmp_path / 'data'
    root.mkdir()
    if target == 'root':
        root = tmp_path / 'linked'
        root.symlink_to(real, target_is_directory=True)
    elif target == 'ancestor':
        link = tmp_path / 'linked'
        link.symlink_to(real, target_is_directory=True)
        root = link / 'nested'
    else:
        suffix = {'database': '', 'journal': '-journal', 'wal': '-wal', 'shm': '-shm'}[target]
        (root / ('capabilities.db' + suffix)).symlink_to(sentinel)
    with pytest.raises((ValueError, OSError)):
        CapabilityStore(root)
    assert sentinel.read_text() == 'unchanged'


def test_late_database_symlink_rejected(tmp_path):
    store = CapabilityStore(tmp_path)
    target = tmp_path / 'target'
    target.write_text('unchanged')
    # Replace only this test's own known database path.
    assert store.path == tmp_path / 'capabilities.db'
    store.path.unlink()
    store.path.symlink_to(target)
    with pytest.raises(ValueError):
        record(store)
    assert target.read_text() == 'unchanged'
