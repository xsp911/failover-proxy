import pytest
from proxy.pool_store import PoolStore
from test_pool_manager import policy


@pytest.mark.parametrize('empty',[None,''])
def test_optional_caps_roundtrip_null_not_zero(tmp_path,empty):
    db=PoolStore(tmp_path);p=policy()
    for key in ('max_input_usd','max_cache_read_usd','max_cache_write_usd'):p[key]=empty
    saved=db.save(p)
    assert saved['max_output_usd']==.008
    assert saved['max_input_usd'] is None and saved['max_cache_read_usd'] is None and saved['max_cache_write_usd'] is None
    assert PoolStore(tmp_path).get(saved['id'])==saved


def test_absent_caps_are_unconstrained(tmp_path):
    db=PoolStore(tmp_path);p=policy()
    for key in ('max_input_usd','max_output_usd','max_cache_read_usd','max_cache_write_usd'):p.pop(key)
    saved=db.save(p)
    assert all(saved[k] is None for k in ('max_input_usd','max_output_usd','max_cache_read_usd','max_cache_write_usd'))


def test_prediction_safe_evidence_roundtrip(tmp_path):
    db=PoolStore(tmp_path);p=db.save(policy());r={'channel_id':123,'model_name':p['model'],'configuration_key':'a'*64,'verification_status':'suspect','probability':.001,'verified_at':'2026-10-23T12:00:00+00:00','predicted_model':'alternative-model','predicted_probability':.99}
    db.save_verification(p['id'],p['revision'],r)
    saved=db.verification(p['id'],123)
    assert saved['predicted_model']=='alternative-model' and saved['predicted_probability']==.99
    with pytest.raises(ValueError):db.save_verification(p['id'],p['revision'],{**r,'predicted_model':'bad\nmodel'})
    with pytest.raises(ValueError):db.save_verification(p['id'],p['revision'],{**r,'predicted_probability':1.01})
