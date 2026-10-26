import asyncio
import time

import pytest

from proxy.pool_manager import PoolError,iso
from proxy.pool_automation import remote_before,tick
from proxy.pool_audit_confirmation import confirm_member
from test_pool_parallel import setup


@pytest.mark.asyncio
async def test_uncertain_route_must_match_sort_and_full_identity(tmp_path):
    m,c,p=await setup(tmp_path,1);draft=m.route_draft(p,[100]);r=await c.save_subscription(1,p['model'],draft)
    m.auto.save_remote(p['id'],r['id'],r['config_version'],draft,'uncertain')
    c.routes[0].update(config_version=2,sort_strategy='random')
    with pytest.raises(PoolError,match='auto_remote_route_conflict'):await remote_before(m,p,c)
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_operational_edit_preserves_hidden_verification(tmp_path):
    m,c,p=await setup(tmp_path,2);p=await m.policy({**p,'max_candidates':1},p['id'])
    for candidate in await m.discover(p,c,full=True):m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'verified_at':iso(time.time())})
    await m.discover(p,c);p=await m.policy({**p,'name':'renamed'},p['id'])
    assert m.db.verification(p['id'],101)['verification_status']=='verified'
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_manual_auto_run_advances_schedule_prevents_duplicate(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,1);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    async def fake_job(job,pool):
        job['status']='done';m.auto.finish(pool['id'],'refresh')
    m._job=fake_job
    j=await m.run_now(p['id'],True);await m.tasks[j['id']]
    state=m.auto.state(p['id']);assert state['next_refresh_at']>time.time()+21000 and state['last_refresh_at']
    count=len(m.jobs);await tick(m);assert len(m.jobs)==count
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_member_technical_failure_requires_second_check():
    calls=[]
    class Probe:
        async def run(self,*args):calls.append(1);return {'verification_status':'verified','probability':.99}
    result=await confirm_member(Probe(),{}, {},'secret',None,{'verification_status':'unknown','reason':'content_after_finish'})
    assert result['verification_status']=='verified' and len(calls)==1
    assert result['technical_confirmation']['confirmed'] is False
