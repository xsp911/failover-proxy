import pytest
import httpx

from proxy.a6_probe import TargetProbe
from proxy.a6_client import A6Error
from test_pool_parallel import setup


@pytest.mark.asyncio
async def test_four_worker_live_activity_has_request_stages(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,4)
    stages=[]
    async def probe(self,pool,candidate,secret,reserve):
        await self.activity('connecting')
        await self.activity('streaming',http_status=200)
        stages.append(pool['test_token_id'])
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test']}
    monkeypatch.setattr(TargetProbe,'run',probe)
    job={'id':'activity','progress':0};await m.verify(p,c,job)
    assert len(set(stages))==4
    assert job['last_activity_at'] and job['updated_at']
    assert all(w.get('last_activity_at') and w.get('request_stage')=='streaming' for w in job['workers'])
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_failed_remote_delete_disables_auto_until_explicit_recovery(tmp_path):
    from proxy.pool_automation import apply_members
    m,c,p=await setup(tmp_path,1);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    await apply_members(m,p,c,await m.discover(p,c))
    async def fail(*args,**kwargs):raise A6Error('a6_route_write_uncertain',409)
    c.save_subscription=fail
    with pytest.raises(A6Error):await m.delete(p['id'],p['revision'],True)
    remaining=m.db.get(p['id'])
    assert not remaining['enabled'] and not remaining['auto_publish'] and remaining['status']=='blocked'
    assert m.deleted.blocked(p['model'],'secret-for-1')
    await m.close();await m.monitor.close()
