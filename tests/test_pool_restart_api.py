import httpx
import pytest

from proxy.app import create_app
from test_pool_manager import FakeClient,policy


@pytest.mark.asyncio
async def test_restart_endpoint_csrf_and_explicit_paid_consent(tmp_path):
    app=create_app(tmp_path);app.state.store.initialize_admin('admin','password');app.state.a6.save('fake-console-pat-12345',23984)
    fake=FakeClient();app.state.pools.client=lambda:fake
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        h={'X-CSRF-Token':(await c.post('/admin/login',json={'username':'admin','password':'password'})).json()['csrf_token']}
        p=(await c.post('/admin/a6/pools',json=policy(),headers=h)).json()
        path='/admin/a6/pools/'+p['id']+'/restart'
        assert (await c.post(path,json={'confirm_cost':True})).status_code==403
        assert (await c.post(path,json={},headers=h)).status_code==400
        calls=[]
        async def restart(id,confirm):calls.append((id,confirm));return {'id':'restart-test','restarted':True}
        app.state.pools.restart=restart
        r=await c.post(path,json={'confirm_cost':True},headers=h)
        assert r.status_code==202 and r.json()['restarted']
        assert calls==[(p['id'],True)] and not fake.writes
    await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
