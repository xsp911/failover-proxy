from concurrent.futures import ThreadPoolExecutor

import pytest
from proxy.pool_billing import BillingStore


def test_exact_charge_replaces_reserve_and_frees_budget(tmp_path):
    b=BillingStore(tmp_path);r=b.reserve('pool','job',1,2,.05,.05,now=1)
    assert b.reserve('pool','job',1,2,.001,.05,now=1) is None
    b.bind(r,'req');b.settle(r,'req',1,5,500000)
    assert b.stats('pool')['actual_cost_usd']==.00001
    assert b.reserve('pool','job',1,2,.04,.05,now=1)


def test_settlement_idempotent_no_double_spend_or_wrong_token(tmp_path):
    b=BillingStore(tmp_path);r=b.reserve('pool','job',1,2,.1,1);b.bind(r,'req')
    with pytest.raises(ValueError):b.settle(r,'req',2,5000,500000)
    for _ in range(3):b.settle(r,'req',1,5000,500000)
    assert b.stats('pool')['actual_cost_usd']==.01
    with pytest.raises(ValueError):b.settle(r,'req',1,5001,500000)
    r2=b.reserve('pool','job',1,2,.1,1)
    with pytest.raises(ValueError):b.bind(r2,'req')


def test_atomic_shared_daily_pending_limit_and_day_rollover(tmp_path):
    b=BillingStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as e:
        results=list(e.map(lambda i:b.reserve('pool','job'+str(i),1,2,.01,1,.05,now=1),range(20)))
    assert sum(r is not None for r in results)==5
    assert b.stats('pool')['pending_reserved_usd']==.05
    assert b.reserve('pool','nextday',1,2,.01,1,.05,now=86401) is None
    assert b.stats('pool',day='1970-01-02')['pending_reserved_usd']==.05


def test_cancel_unsent_only_and_reopen_persists_pending(tmp_path):
    b=BillingStore(tmp_path);r=b.reserve('pool','job',1,2,.1,1)
    assert b.cancel_unsent(r)
    assert b.stats('pool')['total_requests']==0
    r=b.reserve('pool','job',1,2,.1,1);b.bind(r,'req')
    assert not b.cancel_unsent(r)
    reopened=BillingStore(tmp_path)
    assert reopened.pending()[0]['request_id']=='req'
    assert reopened.stats('pool')['pending_reserved_usd']==.1
