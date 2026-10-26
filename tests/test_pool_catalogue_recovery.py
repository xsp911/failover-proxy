import pytest
from proxy.a6_client import A6Error
from proxy.a6_probe import TargetProbe
from test_pool_parallel import setup


@pytest.mark.asyncio
async def test_transient_token_catalogue_failure_does_not_kill_worker(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,2)
    p=await m.policy({**p,'test_token_ids':[2],'test_token_id':2,'max_members':2},p['id'])
    search=c.search;token_calls=0
    async def changing(token,model):
        nonlocal token_calls
        if token==2:
            token_calls+=1
            if token_calls==1:raise A6Error('a6_search_snapshot_changed')
        return await search(token,model)
    c.search=changing
    async def passed(self,pool,candidate,secret,reserve):
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',passed)
    job={'id':'catalogue-recovery','progress':0};await m.verify(p,c,job)
    assert job['workers'][0]['status']=='done' and job['progress']==2
    assert token_calls==2 and job['result']['verified']==2
    await m.close();await m.monitor.close()
