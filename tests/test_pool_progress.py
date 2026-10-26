import asyncio

import httpx
import pytest

from proxy.a6_client import A6Client,A6Error
from proxy.a6_probe import TargetProbe
from proxy.pool_manager import PoolManager
from proxy.model_monitor import ModelMonitor
from proxy.a6_settings import A6Settings
from proxy.store import Store
from test_a6_client import page,row as listing,response,MODEL
from test_pool_manager import FakeClient,policy,row


@pytest.mark.asyncio
async def test_pagination_retries_entire_pass_after_real_count_change(monkeypatch):
    calls=[];passes=0
    def upstream(req):
        nonlocal passes
        n=int(req.url.params['page']);calls.append(n)
        if n==1:passes+=1
        total=102 if passes==1 and n==2 else 101
        rows=[listing(i) for i in range(1,101)] if n==1 else [listing(101)]
        return response(page(rows,total=total,snapshot='page-'+str(n)))
    c=A6Client('fake-pat-test',23,httpx.MockTransport(upstream))
    rows=await c.search(7,MODEL)
    assert len(rows)==101 and calls==[1,2,1,2,1]
    await c.close()


@pytest.mark.asyncio
async def test_candidate_result_visible_before_next_candidate_finishes(tmp_path,monkeypatch):
    s=Store(tmp_path);a6=A6Settings(s);a6.save('fake-pat-1234567890',23);monitor=ModelMonitor(s,tmp_path);m=PoolManager(s,a6,monitor,tmp_path)
    c=FakeClient();c.rows=[row(),row(channel=124)];m.client=lambda:c
    p=await m.policy(policy());blocked=asyncio.Event();resume=asyncio.Event()
    async def run(self,pool,candidate,secret,reserve):
        if candidate['channel_id']==124:
            blocked.set();await resume.wait()
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',run)
    job={'progress':0}
    task=asyncio.create_task(m.verify(p,c,job));await blocked.wait()
    current=m.db.get(p['id'])
    first=next(x for x in current['candidates'] if x['channel_id']==123)
    assert first['verification_status']=='verified' and job['progress']==1
    assert job['phase']=='testing' and job['current_channel_id']==124
    resume.set();await task
    await m.close();await monitor.close()


@pytest.mark.asyncio
async def test_preflight_error_persisted_as_pool_last_error(tmp_path):
    s=Store(tmp_path);a6=A6Settings(s);a6.save('fake-pat-1234567890',23);monitor=ModelMonitor(s,tmp_path);m=PoolManager(s,a6,monitor,tmp_path)
    c=FakeClient();m.client=lambda:c;p=await m.policy(policy())
    async def unstable(*_):raise A6Error('a6_search_snapshot_changed')
    c.search=unstable
    j=await m.enqueue(p['id'],'verify',True);await m.tasks[j['id']]
    assert m.jobs[j['id']]['status']=='error' and m.jobs[j['id']]['phase']=='discovery'
    assert m.db.get(p['id'])['last_error']=='a6_search_snapshot_changed'
    await m.close();await monitor.close()
