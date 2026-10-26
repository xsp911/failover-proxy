import copy

import pytest

from proxy.a6_client import A6Error
from proxy.a6_probe import TargetProbe
from proxy.pool_cleanup import park_owned,parked_matches
from test_pool_parallel import setup
from test_pool_manager import FakeClient,policy


@pytest.mark.asyncio
async def test_one_worker_failure_keeps_good_results_ready_for_publication(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,4)
    original=c.token_secret
    async def token_key(id):
        if id==3:raise A6Error('a6_http_500')
        return await original(id)
    c.token_secret=token_key
    async def passed(self,pool,candidate,secret,reserve):
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',passed)
    job={'progress':0};result=await m.verify(p,c,job)
    assert any(r['verification_status']=='verified' for r in result)
    assert job['result']['verified']>=1 and job['result']['partial'] is True
    assert 'a6_http_500' in job['warnings']
    assert not m.db.get(p['id']).get('last_sync_at')
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_cleanup_accepted_write_then_500_confirmed_by_readback(tmp_path):
    m,c,p=await setup(tmp_path,1)
    draft=m.route_draft(p,[100]);route=await c.save_subscription(2,p['model'],draft)
    original=c.save_subscription
    async def ambiguous(*args,**kwargs):
        await original(*args,**kwargs)
        raise A6Error('a6_route_write_uncertain',409)
    c.save_subscription=ambiguous
    parked=await park_owned(m,c,p,2,route)
    assert parked['caps']['input_price_micros']==1
    assert m.operations.get(2,p['model'])['state']=='parked'
    assert len(c.writes)==2
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_cleanup_does_not_require_100percent_cache(tmp_path):
    m,c,p=await setup(tmp_path,1)
    route=await c.save_subscription(2,p['model'],m.route_draft(p,[100]))
    original=c.save_subscription
    async def reject_impossible(token,model,draft,existing=None):
        if draft.get('min_cache_hit_rate_bps')==10000:raise A6Error('a6_http_500')
        return await original(token,model,draft,existing)
    c.save_subscription=reject_impossible
    await park_owned(m,c,p,2,route)
    assert c.writes[-1][2]['min_cache_hit_rate_bps'] is None
    assert c.writes[-1][2]['fallback_enabled'] is False
    await m.close();await m.monitor.close()
