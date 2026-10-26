import time

import pytest

from proxy.a6_probe import TargetProbe
from proxy.pool_automation import apply_members
from test_pool_parallel import setup
from test_pool_manager import row


@pytest.mark.asyncio
async def test_checks_next_eligible_beyond_initial_display_batch(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,12)
    p=await m.policy({**p,'max_candidates':3,'max_members':2,'max_probe_requests':60},p['id'])
    attempted=[]
    async def probe(self,pool,candidate,secret,reserve):
        attempted.append(candidate['channel_id'])
        status='verified' if candidate['channel_id']>=106 else 'unknown'
        return {'verification_status':status,'probability':.99 if status=='verified' else None,'reason':None if status=='verified' else 'upstream_sse_error','actual_channel_id':candidate['channel_id'] if status=='verified' else None,'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',probe)
    job={'id':'progressive-run','progress':0}
    results=await m.verify(p,c,job)
    assert any(id>=106 for id in attempted) and len(attempted)==len(set(attempted))
    assert len([r for r in results if r['verification_status']=='verified'])>=2
    assert job['result']['stop_reason']=='target_reached'
    assert len(m.db.get(p['id'])['candidates'])<=p['max_candidates']
    assert not any(t==p['token_id'] for t,_,_ in c.writes)
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_recent_failures_skipped_until_24hours_and_unseen_first(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,8)
    now=time.time()
    for id in (100,101,102):m.auto.outcome(p['id'],id,'unknown','upstream_sse_error',now,'old-run')
    for id in (103,104):m.auto.outcome(p['id'],id,'suspect',None,now-86401,'yesterday-run')
    p=await m.policy({**p,'test_token_ids':[2],'max_members':1},p['id'])
    attempted=[]
    async def probe(self,pool,candidate,secret,reserve):
        attempted.append(candidate['channel_id'])
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',probe)
    job={'id':'new-run','progress':0};await m.verify(p,c,job)
    assert attempted==[105]
    assert job['search']['cooldown_count']==3
    assert all(m.auto.list_records(p['id'])[i]['failures']==1 for i in range(3))
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_all_failed_waits_not_rebills_before_day(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,5)
    calls=0
    async def probe(self,pool,candidate,secret,reserve):
        nonlocal calls
        calls+=1
        return {'verification_status':'unknown','probability':None,'reason':'upstream_sse_error','actual_channel_id':None}
    monkeypatch.setattr(TargetProbe,'run',probe)
    first={'id':'first-run','progress':0};await m.verify(p,c,first)
    assert calls==5
    second={'id':'second-run','progress':0};await m.verify(p,c,second)
    assert calls==5 and second['search']['cooldown_count']==5
    assert second['result']['stop_reason']=='cooldown_wait'
    assert [r['failures'] for r in m.auto.list_records(p['id'])]==[1]*5
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_existing_empty_remote_route_no_internal_error(tmp_path):
    m,c,p=await setup(tmp_path,1)
    p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    d=m.route_draft(p,[100]);d['channels']=[]
    remote=await c.save_subscription(1,p['model'],d)
    m.auto.save_remote(p['id'],remote['id'],remote['config_version'],d,'owned')
    assert await apply_members(m,p,c,[]) is None
    assert m.db.get(p['id'])['status']=='blocked'
    await m.close();await m.monitor.close()
