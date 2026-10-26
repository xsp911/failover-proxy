import time

import pytest

from proxy.a6_probe import TargetProbe
from proxy.pool_automation import apply_members
from proxy.pool_manager import iso
from test_pool_parallel import setup


@pytest.mark.asyncio
async def test_hour_audit_schedules_fresh_replacements_not_six_hour_wait(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,4);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    candidates=await m.discover(p,c)
    first=candidates[0]
    m.db.save_verification(p['id'],p['revision'],{**first,'verification_status':'verified','probability':.99,'actual_channel_id':first['channel_id'],'verified_at':iso(time.time())})
    first=(await m.discover(p,c))[0];await apply_members(m,p,c,[first])
    before=time.time()
    assert m.auto.claim_due(p['id'],'refresh',before)
    m.auto.finish(p['id'],'refresh',now=before)
    assert m.auto.state(p['id'])['next_refresh_at']>=before+21600
    async def passed(self,pool,candidate,secret,reserve):return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',passed)
    j=await m.enqueue(p['id'],'auto_audit',True);await m.tasks[j['id']]
    job=m.jobs[j['id']]
    assert job['status']=='done' and job['result']['refill_scheduled'] is True
    assert m.auto.state(p['id'])['next_refresh_at']<=before+65
    assert len(m.db.get(p['id'])['members'])==1
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_old_failed_evidence_not_displayed_as_never_checked(tmp_path):
    m,c,p=await setup(tmp_path,1);now=time.time();m.auto.outcome(p['id'],100,'suspect',None,now,'old')
    candidates=await m.discover(p,c)
    assert candidates[0]['verification_status']=='unverified'
    assert candidates[0]['last_check_status']=='suspect'
    assert candidates[0]['last_checked_at'] and candidates[0]['next_check_at']
    await m.close();await m.monitor.close()
