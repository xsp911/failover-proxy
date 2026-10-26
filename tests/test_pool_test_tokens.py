"""Multi-test-token policy compatibility, isolation and transactional races."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from proxy.pool_store import PoolStore


def policy(**changes):
    return {'name': 'Pool', 'token_id': 11, 'test_token_ids': [21, 22, 23, 24],
            'model': 'model-a', 'reference_model': 'reference',
            'max_input_usd': 1, 'max_output_usd': 2,
            'max_cache_read_usd': 0, 'max_cache_write_usd': 0, **changes}


@pytest.fixture
def store(tmp_path):
    return PoolStore(tmp_path / 'private')


def test_legacy_policy_write_normalizes_and_persists(store):
    raw = policy(test_token_id=22)
    del raw['test_token_ids']
    row = store.save(raw)
    assert row['test_token_ids'] == [22]
    assert row['test_token_id'] == 22
    with store.connection() as c:
        saved = json.loads(c.execute('SELECT policy FROM pools').fetchone()[0])
    assert saved['test_token_ids'] == [22]
    assert saved['test_token_id'] == 22


def test_legacy_database_read_hydrates_without_rewrite(store):
    row = store.save(policy(test_token_ids=[22]))
    with store.connection() as c:
        raw = json.loads(c.execute('SELECT policy FROM pools').fetchone()[0])
        del raw['test_token_ids']
        c.execute('UPDATE pools SET policy=? WHERE id=?', (json.dumps(raw), row['id']))
        before = dict(c.execute('SELECT * FROM pools').fetchone())
    reopened = PoolStore(store.path.parent)
    assert reopened.get(row['id']) == row
    assert reopened.list() == [row]
    with reopened.connection() as c:
        assert dict(c.execute('SELECT * FROM pools').fetchone()) == before
    updated = reopened.save({**raw, 'id': row['id']}, row['revision'])
    assert updated['test_token_ids'] == [22]
    assert updated['revision'] == 2
    with reopened.connection() as c:
        assert json.loads(c.execute('SELECT policy FROM pools').fetchone()[0])['test_token_ids'] == [22]


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_test_ids_save_and_reopen_preserve_order_and_first(store, count):
    ids = [24, 22, 23, 21][:count]
    row = store.save(policy(test_token_ids=ids, test_token_id=ids[0]))
    assert row['test_token_ids'] == ids
    assert row['test_token_id'] == ids[0]
    assert PoolStore(store.path.parent).get(row['id']) == row
    with store.connection() as c:
        saved = json.loads(c.execute('SELECT policy FROM pools').fetchone()[0])
    assert saved['test_token_ids'] == ids
    assert saved['test_token_id'] == ids[0]


@pytest.mark.parametrize('ids', [[], [21, 22, 23, 24, 25], [21, 21], [21, 22, 21],
                               [True], [False], [21, True], [21, False], [None],
                               ['21'], [21.0], [0], [-1], [2**63], None, {}, 21, (21,)])
def test_invalid_test_token_ids_rejected_atomically(store, ids):
    with pytest.raises(ValueError, match='invalid_test_token_ids'):
        store.save(policy(test_token_ids=ids))
    assert store.list() == []


@pytest.mark.parametrize('legacy', [22, True, False, None, '21', 21.0])
def test_first_mismatch_or_invalid_legacy_rejected(store, legacy):
    with pytest.raises(ValueError, match='conflicting_test_token_id|invalid_test_token_id'):
        store.save(policy(test_token_id=legacy))
    assert store.list() == []


@pytest.mark.parametrize('legacy', [True, False, None, '21', 21.0])
def test_invalid_legacy_only_rejected(store, legacy):
    raw = policy(test_token_id=legacy)
    del raw['test_token_ids']
    with pytest.raises(ValueError, match='invalid_test_token_id'):
        store.save(raw)
    assert store.list() == []


@pytest.mark.parametrize('position', range(4))
def test_own_work_token_cannot_be_any_test_token(store, position):
    ids = [21, 22, 23, 24]
    ids[position] = 11
    with pytest.raises(ValueError, match='separate_test_token_required'):
        store.save(policy(test_token_ids=ids))
    assert store.list() == []


@pytest.mark.parametrize('existing_position', range(4))
@pytest.mark.parametrize('new_position', range(4))
@pytest.mark.parametrize('update', [False, True])
def test_every_test_token_pair_is_isolated(store, existing_position, new_position, update):
    first = store.save(policy())
    raw = policy(token_id=12, model='model-b', test_token_ids=[31, 32, 33, 34])
    prior = store.save(raw) if update else None
    ids = list(raw['test_token_ids'])
    ids[new_position] = first['test_token_ids'][existing_position]
    raw['test_token_ids'] = ids
    if prior:
        raw['id'] = prior['id']
    with pytest.raises(ValueError, match='test_token_in_use'):
        store.save(raw, prior['revision'] if prior else None)
    assert store.get(first['id']) == first
    if prior:
        assert store.get(prior['id']) == prior
    assert len(store.list()) == (2 if update else 1)


@pytest.mark.parametrize('position', range(4))
@pytest.mark.parametrize('direction', ['new_test_existing_work', 'new_work_existing_test'])
@pytest.mark.parametrize('update', [False, True])
def test_all_work_test_role_overlaps_rejected(store, position, direction, update):
    first = store.save(policy())
    raw = policy(token_id=12, model='model-b', test_token_ids=[31, 32, 33, 34])
    prior = store.save(raw) if update else None
    if direction == 'new_test_existing_work':
        raw['test_token_ids'][position] = first['token_id']
    else:
        raw['token_id'] = first['test_token_ids'][position]
    if prior:
        raw['id'] = prior['id']
    with pytest.raises(ValueError, match='test_token_in_use'):
        store.save(raw, prior['revision'] if prior else None)
    assert store.get(first['id']) == first
    if prior:
        assert store.get(prior['id']) == prior
    assert len(store.list()) == (2 if update else 1)


@pytest.mark.parametrize('direction', ['new_test_existing_work', 'new_work_existing_test', 'shared_test'])
def test_legacy_database_rows_participate_in_isolation(store, direction):
    first = store.save(policy(test_token_ids=[22]))
    with store.connection() as c:
        raw = json.loads(c.execute('SELECT policy FROM pools').fetchone()[0])
        del raw['test_token_ids']
        c.execute('UPDATE pools SET policy=? WHERE id=?', (json.dumps(raw), first['id']))
    raw = policy(token_id=12, model='model-b', test_token_ids=[31, 32, 33, 34])
    if direction == 'new_test_existing_work':
        raw['test_token_ids'][3] = 11
    elif direction == 'new_work_existing_test':
        raw['token_id'] = 22
    else:
        raw['test_token_ids'][3] = 22
    with pytest.raises(ValueError, match='test_token_in_use'):
        store.save(raw)
    assert store.list() == [first]


def test_shared_work_token_distinct_models_and_unique_model_unchanged(store):
    first = store.save(policy())
    second = store.save(policy(model='model-b', test_token_ids=[31, 32, 33, 34]))
    assert first['token_id'] == second['token_id']
    with pytest.raises(ValueError, match='duplicate_token_model'):
        store.save(policy(test_token_ids=[41, 42, 43, 44]))
    with pytest.raises(ValueError, match='duplicate_token_model'):
        store.save({**second, 'model': first['model']}, second['revision'])
    assert store.get(second['id']) == second


def test_update_retains_own_tokens_and_releases_removed_ids(store):
    first = store.save(policy())
    second = store.save({**first, 'test_token_ids': [21, 22, 25, 26]}, first['revision'])
    assert second['revision'] == 2
    assert second['test_token_ids'] == [21, 22, 25, 26]
    third = store.save(policy(token_id=12, model='model-b', test_token_ids=[23, 24]))
    assert third['test_token_ids'] == [23, 24]


@pytest.mark.parametrize('update', [False, True])
@pytest.mark.parametrize('overlap', ['shared_test', 'work_test'])
def test_concurrent_cross_pool_overlap_has_one_winner(store, update, overlap):
    raw = [policy(), policy(token_id=12, model='model-b', test_token_ids=[31, 32, 33, 34])]
    prior = [store.save(value) for value in raw] if update else []
    if overlap == 'shared_test':
        raw[0]['test_token_ids'] = [21, 22, 23, 99]
        raw[1]['test_token_ids'] = [31, 32, 33, 99]
    else:
        raw[0]['token_id'] = 99
        raw[1]['test_token_ids'] = [31, 32, 33, 99]
    if update:
        for value, row in zip(raw, prior):
            value['id'] = row['id']
    barrier = Barrier(2)

    def save(index):
        separate_store = PoolStore(store.path.parent)
        barrier.wait(timeout=10)
        try:
            separate_store.save(raw[index], prior[index]['revision'] if update else None)
            return 'saved'
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, range(2)))
    assert sorted(results) == ['saved', 'test_token_in_use']
    rows = store.list()
    assert len(rows) == (2 if update else 1)
    if update:
        assert sorted(row['revision'] for row in rows) == [1, 2]
    for left in rows:
        for right in rows:
            if left['id'] != right['id']:
                assert not set(left['test_token_ids']) & (set(right['test_token_ids']) | {right['token_id']})
                assert left['token_id'] not in right['test_token_ids']
