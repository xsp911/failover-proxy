import copy
import json

import pytest

from proxy.detector_evidence import sanitize_evidence
from proxy.detector_store import DetectorStore
from test_detector_store import binding, result, settings, state


def evidence(first=.2, second=.3, first_status='suspect', second_status='suspect'):
    rounds = [{'round': 1, 'status': first_status, 'target_probability': first}]
    if second != 'missing':
        rounds.append({'round': 2, 'status': second_status, 'target_probability': second})
    return {'rounds': rounds, 'confirmation': {
        'required': True, 'confirmed': True, 'status': 'confirmed',
        'first_probability': first, 'second_probability': None if second == 'missing' else second,
        'threshold_percent': 95}}


def challenge(**changes):
    return {'round': 1, 'id': 'query-01', 'expected_count': 250,
            'parsed_numbers': 250, 'minimum_numbers': 200, 'accepted': True,
            'finish_reason': 'stop', 'attempts': 1, 'errors': [], **changes}


def test_evidence_privacy_and_persistence(tmp_path):
    store = DetectorStore(tmp_path)
    sentinel = 'NEVER STORE RAW KEYS PROMPTS RESPONSES'
    data = evidence()
    data['challenges'] = [challenge(raw_text=sentinel, response={'api_key': sentinel})]
    for item in data['rounds']:
        item.update(raw_text=sentinel, response={'key': sentinel})
    data['confirmation'].update(response=sentinel, api_key=sentinel)
    saved = store.record_result(result(**data, raw_text=sentinel))
    assert saved['challenges'] == [challenge()]
    assert saved['rounds'] == evidence()['rounds']
    assert saved['confirmation']['confirmed'] is True
    assert DetectorStore(tmp_path).latest_results() == [saved]
    assert sentinel not in json.dumps(store.latest_results() + store.states() + store.journal())
    assert sentinel.encode() not in b''.join(path.read_bytes() for path in tmp_path.iterdir() if path.is_file())


@pytest.mark.parametrize('change', [
    {'round': True}, {'round': 0}, {'round': 3}, {'id': 'raw secret'},
    {'expected_count': True}, {'parsed_numbers': 65537}, {'minimum_numbers': -1},
    {'accepted': 1}, {'attempts': 0}, {'attempts': 4}, {'finish_reason': 'raw'},
    {'errors': ['bad error']}, {'errors': ['timeout'] * 4}, {'errors': {'raw': 'text'}},
])
def test_challenge_strict_bounds(change):
    with pytest.raises(ValueError):
        sanitize_evidence({'challenges': [challenge(**change)]})


@pytest.mark.parametrize('data', [
    {'challenges': [challenge()] * 7}, {'rounds': [] * 0 + [{'round': 1, 'status': 'verified', 'target_probability': .5}] * 3},
    {'rounds': [{'round': 1, 'status': 'suspect', 'target_probability': True}]},
    {'rounds': [{'round': 1, 'status': 'suspect', 'target_probability': float('nan')}]},
    {'rounds': [{'round': 1, 'status': 'suspect', 'target_probability': .5, 'prediction': 'raw secret'}]},
    {'rounds': [{'round': 1, 'status': 'suspect', 'target_probability': .5}] * 2},
    {'confirmation': {**evidence()['confirmation'], 'required': 1}},
    {'confirmation': {**evidence()['confirmation'], 'threshold_percent': 101}},
    {'confirmation': {**evidence()['confirmation'], 'first_probability': {'response': 'raw'}}},
])
def test_malformed_evidence_rejected_without_state_change(tmp_path, data):
    store = DetectorStore(tmp_path)
    store.record_result(result('before'))
    before = store.states()
    with pytest.raises(ValueError):
        store.record_result(result('after', **data))
    assert store.states() == before
    assert len(store.latest_results()) == 1


@pytest.mark.parametrize('first,second,first_status,second_status,status', [
    (.2, .3, 'suspect', 'verified', 'suspect'),
    (.2, .99, 'suspect', 'verified', 'verified'),
    (.99, .2, 'verified', 'suspect', 'unknown'),
    (.2, 'missing', 'suspect', 'suspect', 'unknown'),
    (.99, 'missing', 'verified', 'suspect', 'verified'),
    (.2, None, 'suspect', 'error', 'unknown'),
    (.2, .3, 'suspect', 'unknown', 'unknown'),
    (.2, .3, 'unknown', 'suspect', 'unknown'),
    (None, .99, 'unknown', 'verified', 'verified'),
])
def test_current_policy_not_caller_confirmation_controls_result(tmp_path, first, second, first_status, second_status, status):
    store = DetectorStore(tmp_path)
    saved = store.record_result(result(**evidence(first, second, first_status, second_status)))
    assert saved['status'] == status
    assert saved['confirmation']['confirmed'] is (status == 'suspect')
    if status == 'unknown':
        assert saved['error_kind'] == 'low_score_not_confirmed'
        assert state(store)['status'] == 'unknown'


@pytest.mark.parametrize('first,second,threshold,expected', [
    (.8, .85, 90, 'suspect'), (.8, .85, 80, 'verified'),
    (.92, .8, 90, 'unknown'), (.8, .92, 90, 'verified'),
    (.92, 'missing', 95, 'unknown'), (.92, 'missing', 90, 'verified'),
    (.8, .9, 90, 'verified'), (.9, .8, 90, 'unknown'),
])
def test_threshold_reclassifies_complete_evidence_without_rewriting_history(tmp_path, first, second, threshold, expected):
    store = DetectorStore(tmp_path)
    store.save_settings(settings(threshold_percent=50), 0)
    store.record_result(result(**evidence(first, second)))
    original = copy.deepcopy(store.latest_results())
    store.save_settings(settings(threshold_percent=threshold), 1)
    assert state(store)['status'] == expected
    assert store.blocked_provider_ids([binding()]) == ({'p1'} if expected == 'suspect' else set())
    assert store.latest_results() == original


@pytest.mark.parametrize('status,error', [
    ('error', 'upstream_timeout'), ('unknown', 'configuration_changed'),
    ('unknown', 'monitor_disabled_or_target_removed'),
])
def test_invalidated_or_failed_checks_cannot_clear_or_create_conclusions(tmp_path, status, error):
    store = DetectorStore(tmp_path)
    store.record_result(result('prior'))
    saved = store.record_result(result('failed', status=status, error_kind=error, **evidence(.99, .99)))
    assert saved['status'] == status
    assert state(store)['status'] == 'suspect'
    assert state(store)['conclusive_result']['id'] == 'prior'
    assert len(store.journal()) == 1


def test_confirmation_probabilities_without_calibrated_rounds_do_not_quarantine(tmp_path):
    store = DetectorStore(tmp_path)
    store.record_result(result(confirmation=evidence()['confirmation']))
    assert state(store)['status'] == 'unknown'
    assert not store.journal()


def test_pending_evidence_preserves_prior_conclusive_result(tmp_path):
    store = DetectorStore(tmp_path)
    store.record_result(result('prior', status='verified'))
    store.record_result(result('pending', **evidence(.2, 'missing')))
    assert state(store)['status'] == 'verified'
    assert state(store)['latest_status'] == 'unknown'
    assert state(store)['conclusive_result']['id'] == 'prior'
    assert len(store.journal()) == 1
