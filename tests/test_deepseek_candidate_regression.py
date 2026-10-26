import asyncio
import time

import httpx
import pytest

from proxy.a6_client import A6Client
from test_pool_parallel import setup
from test_pool_manager import policy,row
from proxy.a6_probe import TargetProbe


@pytest.mark.asyncio
async def test_changed_test_tokens_clear_stale_summary_then_discovery_repopulates(tmp_path):
    m,c,p=await setup(tmp_path,29)
    await m.discover(p,c)
    old=m.db.get(p['id']);assert old['discovery_summary']['eligible']==29
    changed=await m.policy({**old,'test_token_ids':[2,3],'test_token_id':2},p['id'])
    assert changed['candidates']==[] and changed['discovery_summary'] is None
    assert changed['id'] in m.discovery_due
    found=await m.discover(changed,c)
    assert len(found)==10 and all(r['eligible'] for r in found)
    assert m.db.get(p['id'])['discovery_summary']['shown']==10
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_exact_matching_log_accepted_when_total_count_is_stale():
    calls=[]
    def response(req):
        calls.append(req)
        return httpx.Response(200,json={'success':True,'data':{'total':2,'items':[{'request_id':'req','token_id':2,'type':2,'quota':19,'other':{'actual_channel_id':123}}]}})
    c=A6Client('fake-pat',23,httpx.MockTransport(response));rs=await c.logs('req',2)
    assert len(rs)==1 and rs[0]['quota']==19 and len(calls)==1
    await c.close()


@pytest.mark.asyncio
async def test_empty_delayed_log_returns_empty_for_probe_polling():
    c=A6Client('fake-pat',23,httpx.MockTransport(lambda req:httpx.Response(200,json={'success':True,'data':{'total':1,'items':[]}})))
    assert await c.logs('req',2)==[]
    await c.close()


@pytest.mark.asyncio
async def test_admin_log_failure_does_not_put_seller_into_day_cooldown(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,1)
    m.auto.outcome(p['id'],100,'unknown','a6_incomplete_logs',time.time(),'old')
    calls=[]
    async def probe(self,pool,candidate,secret,reserve):
        calls.append(candidate['channel_id'])
        return {'verification_status':'verified','probability':.99,'actual_channel_id':100,'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',probe)
    job={'id':'new','progress':0};await m.verify(p,c,job)
    assert calls==[100] and job['search']['cooldown_count']==0
    await m.close();await m.monitor.close()
