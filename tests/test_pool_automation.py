import asyncio
import time

import pytest

from proxy.a6_probe import TargetProbe
from proxy.pool_manager import PoolError,iso
from proxy.pool_automation import automate,apply_members,tick
from test_pool_parallel import setup
from test_pool_manager import row


async def automatic(tmp_path,n=4):
    m,c,p=await setup(tmp_path,n)
    p=await m.policy({**p,'enabled':True,'auto_publish':True,'auto_daily_usd':1,'auto_daily_requests':60},p['id'])
    return m,c,p


@pytest.mark.asyncio
async def test_auto_refresh_tests_publishes_strict_verified_only(tmp_path,monkeypatch):
    m,c,p=await automatic(tmp_path)
    async def probe(self,pool,candidate,secret,reserve):
        await reserve(candidate,{'prompt':'fake test'})
        status='suspect' if candidate['channel_id']==101 else 'verified'
        return {'verification_status':status,'probability':.01 if status=='suspect' else .99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',probe)
    job=await m.enqueue(p['id'],'auto_refresh',True);await m.tasks[job['id']]
    j=m.jobs[job['id']];assert j['status']=='done',j
    current=m.db.get(p['id']);assert current['status']=='published'
    assert 101 not in [x['channel_id'] for x in current['members']]
    assert len(current['members'])==3
    work=[d for t,_,d in c.writes if t==1][-1]
    assert work['auto_pool_enabled'] is False and work['fallback_enabled'] is False
    assert m.auto.state(p['id'])['daily_requests']==4
    assert j['result']['requires_publication'] is False
    assert m.auto.remote(p['id'])['state']=='owned'
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_hour_audit_removes_low_member_before_other_tests_finish(tmp_path,monkeypatch):
    m,c,p=await automatic(tmp_path,2)
    candidates=await m.discover(p,c)
    for candidate in candidates:
        m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'verified_at':iso(time.time())})
    members=await m.discover(p,c);await apply_members(m,p,c,members);p=m.db.get(p['id'])
    removed=asyncio.Event();continue_other=asyncio.Event()
    async def probe(self,pool,candidate,secret,reserve):
        if candidate['channel_id']==101:
            await removed.wait();await continue_other.wait()
            return {'verification_status':'verified','probability':.99,'actual_channel_id':101,'request_ids':['req-two']}
        return {'verification_status':'suspect','probability':.001,'actual_channel_id':100,'request_ids':['req-low']}
    monkeypatch.setattr(TargetProbe,'run',probe)
    original=c.save_subscription
    async def save(token,model,draft,before=None):
        r=await original(token,model,draft,before)
        if token==1 and [x['channel_id'] for x in draft['channels']]==[101]:removed.set()
        return r
    c.save_subscription=save
    job=await m.enqueue(p['id'],'auto_audit',True)
    await asyncio.wait_for(removed.wait(),5)
    assert [x['channel_id'] for x in m.db.get(p['id'])['members']]==[101]
    continue_other.set();await m.tasks[job['id']]
    assert m.jobs[job['id']]['status']=='done'
    assert m.jobs[job['id']]['removed_channels']==[100]
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_last_member_removed_parks_remote_not_delete_or_empty(tmp_path):
    m,c,p=await automatic(tmp_path,1)
    candidate=(await m.discover(p,c))[0]
    await apply_members(m,p,c,[candidate]);await apply_members(m,p,c,[])
    state=m.db.get(p['id']);assert state['members']==[] and state['status']=='blocked'
    draft=[d for t,_,d in c.writes if t==1][-1]
    assert draft['channels']==[{'channel_id':100,'priority':None}]
    assert draft['caps']['input_price_micros']==1 and draft['fallback_enabled'] is False
    assert m.auto.remote(p['id'])['state']=='parked'
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_unmanaged_remote_cannot_be_overwritten_automatically(tmp_path):
    m,c,p=await automatic(tmp_path,1)
    await c.save_subscription(1,p['model'],m.route_draft(p,[999]))
    with pytest.raises(PoolError,match='auto_remote_route_conflict'):await automate(m,p,c,{'id':'auto-test','kind':'auto_refresh','automatic':True,'progress':0})
    assert len(c.writes)==1
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_banned_seller_not_candidate_and_success_resets_streak(tmp_path):
    m,c,p=await automatic(tmp_path,2)
    for i in range(4):m.auto.outcome(p['id'],100,'suspect',None,time.time(),str(i))
    found=await m.discover(p,c);assert [x['channel_id'] for x in found]==[101]
    m.auto.unban(p['id'],100)
    assert len(await m.discover(p,c))==2
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_disabled_legacy_pool_no_automatic_tasks(tmp_path):
    m,c,p=await setup(tmp_path,1);await tick(m,time.time())
    assert not m.tasks and not c.writes
    await m.close();await m.monitor.close()
