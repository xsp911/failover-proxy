import pytest

from proxy.detector_store import DetectorStore
from test_detector_store import binding,settings,result,state


def test_threshold_reclassifies_without_new_probe_or_rewriting_history(tmp_path):
    s=DetectorStore(tmp_path)
    current=s.save_settings(settings(),0)
    s.record_result(result('sample',target_probability=.92))
    assert s.blocked_provider_ids([binding()])=={'p1'}
    original=s.latest_results()[0]
    s.save_settings({**current,'threshold_percent':90},current['revision'])
    assert s.settings()['threshold_percent']==90
    assert not s.blocked_provider_ids([binding()])
    assert state(s)['status']=='verified'
    assert s.latest_results()==[original]
    journal=s.journal();assert len(journal)==2
    assert journal[0]['source']=='threshold_change'
    assert journal[0]['from_status']=='suspect' and journal[0]['to_status']=='verified'
    assert journal[0]['sample_checked_at']==original['checked_at']
    current=s.settings();s.save_settings({**current,'threshold_percent':94},current['revision'])
    assert s.blocked_provider_ids([binding()])=={'p1'}
    assert len(s.journal())==3
    assert DetectorStore(tmp_path).settings()['threshold_percent']==94


def test_threshold_boundary_and_inflight_policy_changes(tmp_path):
    s=DetectorStore(tmp_path);c=s.save_settings(settings(threshold_percent=90),0)
    s.record_result(result('at90',status='suspect',target_probability=.90))
    assert state(s)['status']=='verified'
    s.record_result(result('below90',status='verified',target_probability=.89999))
    assert state(s)['status']=='suspect'
    s.record_result(result('network',status='error',error_kind='upstream_timeout'))
    c=s.settings();s.save_settings({**c,'threshold_percent':85.5},c['revision'])
    assert state(s)['status']=='verified'
    assert state(s)['latest_status']=='error'
    assert len(s.latest_results())==3


@pytest.mark.parametrize('cutoff',[50,90,90.5,100])
def test_valid_custom_threshold(tmp_path,cutoff):
    s=DetectorStore(tmp_path)
    assert s.save_settings(settings(threshold_percent=cutoff),0)['threshold_percent']==cutoff
