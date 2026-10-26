import pytest

from proxy.pool_manager import PoolError
from proxy.pool_gateway import guarded_config
from test_pool_parallel import setup
from proxy.pool_automation import apply_members


@pytest.mark.asyncio
async def test_published_pool_delete_parks_work_route_and_removes_policy(tmp_path):
    m,c,p=await setup(tmp_path,1);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    members=await m.discover(p,c);await apply_members(m,p,c,members)
    assert await m.delete(p['id'],p['revision'],True)
    assert m.db.get(p['id']) is None
    assert m.auto.state(p['id'])['enabled'] is False
    draft=[d for t,_,d in c.writes if t==p['token_id']][-1]
    assert draft['channels'] and draft['caps']['input_price_micros']==1
    assert draft['auto_pool_enabled'] is False and draft['fallback_enabled'] is False
    assert m.deleted.blocked(p['model'],'secret-for-1')
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_delete_foreign_remote_rejected_without_touching_it(tmp_path):
    m,c,p=await setup(tmp_path,1)
    await c.save_subscription(1,p['model'],m.route_draft(p,[999]))
    with pytest.raises(PoolError,match='auto_remote_route_conflict'):await m.delete(p['id'],p['revision'],True)
    assert len(c.writes)==1 and m.db.get(p['id'])
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_deleted_pool_remains_locally_fail_closed_without_policy(tmp_path):
    m,c,p=await setup(tmp_path,1)
    await m.delete(p['id'],p['revision'],True)
    config={'providers':[{'id':'a6','base_url':'https://api.a6api.com/v1','api_key':'sk-secret-for-1'}],'models':[{'name':'alias','routes':[{'provider_id':'a6','upstream_model':p['model']}]}]}
    with pytest.raises(PoolError,match='managed_pool_deleted'):await guarded_config(m,config,'alias')
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_run_now_uses_auto_path_for_enabled_automatic_pool(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,1);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    seen=[]
    async def enqueue(id,kind,confirm):seen.append((id,kind,confirm));return {'id':'newjob'}
    m.enqueue=enqueue
    await m.run_now(p['id'],True)
    assert seen==[(p['id'],'auto_refresh',True)]
    await m.close();await m.monitor.close()
