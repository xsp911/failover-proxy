import time

import pytest

from proxy.a6_probe import TargetProbe
from proxy.pool_automation import apply_members
from proxy.pool_manager import iso
from test_pool_parallel import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('second_status,remaining',[('verified',1),('unknown',0)])
async def test_audit_only_drops_after_independent_second_technical_failure(tmp_path,monkeypatch,second_status,remaining):
    m,c,p=await setup(tmp_path,1);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    candidate=(await m.discover(p,c))[0]
    m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'actual_channel_id':100,'verified_at':iso(time.time())})
    await apply_members(m,p,c,await m.discover(p,c))
    calls=[]
    async def probe(self,pool,candidate,secret,reserve):
        calls.append(1)
        if len(calls)==1 or second_status=='unknown':return {'verification_status':'unknown','probability':None,'actual_channel_id':None,'reason':'content_after_finish'}
        return {'verification_status':'verified','probability':.99,'actual_channel_id':100,'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',probe)
    j=await m.enqueue(p['id'],'auto_audit',True);await m.tasks[j['id']]
    assert len(calls)==2 and len(m.db.get(p['id'])['members'])==remaining
    evidence=m.db.verification(p['id'],100)
    assert evidence['technical_confirmation']['confirmed']==(second_status=='unknown')
    history=m.auto.list_records(p['id'])[0]
    assert history['failures']==(1 if second_status=='unknown' else 0),'internal confirmations count as one seller-check'
    await m.close();await m.monitor.close()
