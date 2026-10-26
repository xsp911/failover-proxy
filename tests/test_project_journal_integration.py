import pytest
import httpx

from proxy.app import create_app
from proxy.pool_automation import apply_members
from test_pool_parallel import setup


@pytest.mark.asyncio
async def test_confirmed_publication_records_delta_but_noop_does_not(tmp_path):
    m,c,p=await setup(tmp_path,2);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    rows=await m.discover(p,c);await apply_members(m,p,c,rows)
    events=m.membership_history.events(p['id']);assert len(events)==1 and events[0]['added']==[100,101]
    await apply_members(m,p,c,rows);assert len(m.membership_history.events(p['id']))==1
    await apply_members(m,p,c,rows[:1]);e=m.membership_history.events(p['id'])[0];assert e['removed']==[101] and e['after']==[100]
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_events_api_local_authenticated_and_preserves_deleted_history(tmp_path):
    app=create_app(tmp_path);app.state.store.initialize_admin('admin','password')
    app.state.pools.membership_history.record('pool_test',before=[],after=[123],reason='verified_pool_sync',source='system')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        assert (await c.get('/admin/a6/pools/pool_test/events')).status_code==401
        await c.post('/admin/login',json={'username':'admin','password':'password'})
        r=await c.get('/admin/a6/pools/pool_test/events');assert r.status_code==200 and r.json()['events'][0]['added']==[123]
    await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
