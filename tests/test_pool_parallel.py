import asyncio
import copy

import httpx
import pytest

from proxy.a6_probe import TargetProbe
from proxy.pool_manager import PoolManager,PoolError
from proxy.a6_settings import A6Settings
from proxy.model_monitor import ModelMonitor
from proxy.store import Store
from test_pool_manager import FakeClient,policy,row


class MultipleTokensClient(FakeClient):
    async def tokens(self):return [{'id':i,'status':1,'name':str(i)} for i in range(1,10)]
    async def token_secret(self,id):return 'secret-for-'+str(id)


async def setup(tmp_path,candidates=8):
    store=Store(tmp_path);a6=A6Settings(store);a6.save('fake-test-pat-123456',23984);monitor=ModelMonitor(store,tmp_path)
    m=PoolManager(store,a6,monitor,tmp_path);c=MultipleTokensClient();c.rows=[row(channel=100+i) for i in range(candidates)];m.client=lambda:c
    p=await m.policy({**policy(),'test_token_ids':[2,3,4,5],'max_probe_requests':6})
    return m,c,p


@pytest.mark.asyncio
async def test_four_candidates_overlap_each_key_isolated_and_parked(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path)
    p=await m.policy({**p,'max_members':8},p['id'])
    active=0;peak=0;bytoken={};perkey=0;attribution=[]
    async def run(self,pool,candidate,secret,reserve):
        nonlocal active,peak,perkey
        token=pool['test_token_id'];active+=1;peak=max(peak,active);bytoken[token]=bytoken.get(token,0)+1;perkey=max(perkey,bytoken[token])
        assert secret=='secret-for-'+str(token)
        attribution.append((token,candidate['channel_id']))
        try:
            await asyncio.sleep(.03)
            return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-'+str(candidate['channel_id'])]}
        finally:active-=1;bytoken[token]-=1
    monkeypatch.setattr(TargetProbe,'run',run)
    job={'progress':0}
    results=await m.verify(p,c,job)
    assert peak==4 and perkey==1 and len(attribution)==8
    assert job['progress']==8 and len(job['workers'])==4
    assert {t for t,_ in attribution}=={2,3,4,5}
    assert all(x['verification_status']=='verified' for x in results)
    assert {write[0] for write in c.writes}=={2,3,4,5}
    for token in (2,3,4,5):
        parked=[d for t,_,d in c.writes if t==token][-1]
        assert parked['caps']['input_price_micros']==1 and parked['fallback_enabled'] is False
        assert m.operations.get(token,p['model'])['state']=='parked'
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_shared_budget_never_multiplied_by_four_workers(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path)
    p=await m.policy({**p,'max_members':8},p['id'])
    executed=0
    async def run(self,pool,candidate,secret,reserve):
        nonlocal executed
        try:
            await reserve(candidate,{'prompt':'synthetic'})
            executed+=1
            await asyncio.sleep(.01)
            return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
        except Exception as e:
            return {'verification_status':'unknown','probability':None,'actual_channel_id':None,'reason':getattr(e,'kind','failed')}
    monkeypatch.setattr(TargetProbe,'run',run)
    job={'progress':0}
    await m.verify(p,c,job)
    assert executed==6 and job['probe_requests']==6
    assert job['reserved_cost_usd']<=p['max_probe_usd']
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_cancel_all_four_children_and_cleanup_owned_routes(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path)
    entered=0;ready=asyncio.Event();stopped=0
    async def run(*args):
        nonlocal entered,stopped
        entered+=1
        if entered==4:ready.set()
        try:await asyncio.sleep(60)
        finally:stopped+=1
    monkeypatch.setattr(TargetProbe,'run',run)
    job={'progress':0};task=asyncio.create_task(m.verify(p,c,job));await ready.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert stopped==4
    assert all(m.operations.get(token,p['model'])['state']=='parked' for token in (2,3,4,5))
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_single_legacy_key_retains_serial_behavior(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,3)
    current={**p,'test_token_id':2,'test_token_ids':[2]}
    p=await m.policy(current,p['id'])
    active=peak=0
    async def run(self,pool,candidate,secret,reserve):
        nonlocal active,peak
        active+=1;peak=max(peak,active);await asyncio.sleep(.01);active-=1
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',run)
    await m.verify(p,c,{'progress':0})
    assert peak==1
    await m.close();await m.monitor.close()
