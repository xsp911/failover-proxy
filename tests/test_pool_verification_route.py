import time
from pathlib import Path

import httpx
import pytest

from proxy.pool_manager import PoolManager,PoolError
from proxy.a6_probe import TargetProbe
from proxy.a6_settings import A6Settings
from proxy.model_monitor import ModelMonitor
from proxy.store import Store
from test_pool_manager import FakeClient,policy,row


@pytest.mark.asyncio
async def test_verification_uses_only_test_token_and_parks_route(tmp_path,monkeypatch):
    store=Store(tmp_path);a6=A6Settings(store);a6.save('fake-pat-test-123456',23984);monitor=ModelMonitor(store,tmp_path)
    manager=PoolManager(store,a6,monitor,tmp_path);client=FakeClient();client.rows=[row()];manager.client=lambda:client
    p=await manager.policy(policy())
    async def success(self,pool,candidate,secret,reserve):
        await reserve(candidate,{'prompt':'fake challenge'})
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['request-test']}
    monkeypatch.setattr(TargetProbe,'run',success)
    job={'progress':0,'total':0}
    result=await manager.verify(p,client,job)
    assert job['probe_requests']==1
    assert all(x[0]==p['test_token_id'] for x in client.writes)
    assert len(client.writes)==2
    parked=client.writes[-1][2]
    assert parked['caps']['input_price_micros']==1 and parked['min_cache_hit_rate_bps'] is None
    assert parked['auto_pool_enabled'] is False and parked['fallback_enabled'] is False
    assert result[0]['verification_status']=='verified'
    assert manager.operations.get(p['test_token_id'],p['model'])['state']=='parked'
    await manager.close();await monitor.close()


@pytest.mark.asyncio
async def test_unmanaged_test_route_not_overwritten(tmp_path):
    store=Store(tmp_path);a6=A6Settings(store);a6.save('fake-pat-test-123456',23984);monitor=ModelMonitor(store,tmp_path)
    m=PoolManager(store,a6,monitor,tmp_path);c=FakeClient();c.rows=[row()];m.client=lambda:c
    p=await m.policy(policy());c.routes=[{'id':9,'token_id':p['test_token_id'],'model_name':p['model'],'config_version':1,'channels':[{'channel_id':123}]}]
    with pytest.raises(PoolError,match='unmanaged'):await m.verify(p,c,{'progress':0})
    assert not c.writes
    await m.close();await monitor.close()
