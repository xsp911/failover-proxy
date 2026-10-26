from proxy.pool_auto_store import PoolAutoStore


def test_generic_audit_refill_does_not_erase_daily_budget_wait(tmp_path):
    state=PoolAutoStore(tmp_path);state.configure('pool',True,1000)
    state.request_refill('pool',1000,86400,'daily_budget')
    state.request_refill('pool',1500,None,'fresh_candidates')
    data=state.state('pool');assert data['refresh_not_before']==86400 and data['next_refresh_at']==86400 and data['refresh_reason']=='daily_budget'
    assert not state.claim_due('pool','refresh',86399)
    assert state.claim_due('pool','audit',4600),'hourly model audits remain unchanged'
