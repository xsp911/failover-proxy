import asyncio

import httpx
import pytest

from proxy.app import create_app
from test_pool_manager import FakeClient,policy,row


@pytest.mark.asyncio
async def test_auth_csrf_multiple_models_discovery_preview(tmp_path):
    app=create_app(tmp_path);app.state.store.initialize_admin('admin','password')
    app.state.a6.save('fake-console-pat-12345',23984)
    fake=FakeClient();fake.rows=[row(),row(model='grok-4.7',channel=456)]
    app.state.pools.client=lambda:fake
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        assert (await c.get('/admin/a6/pools')).status_code==401
        h={'X-CSRF-Token':(await c.post('/admin/login',json={'username':'admin','password':'password'})).json()['csrf_token']}
        assert (await c.post('/admin/a6/pools',json=policy())).status_code==403
        a=await c.post('/admin/a6/pools',json=policy(),headers=h);assert a.status_code==200,a.text
        b=await c.post('/admin/a6/pools',json=policy('B','grok-4.7',3,4),headers=h);assert b.status_code==200,b.text
        id=a.json()['id'];info=(await c.get('/admin/a6/pools')).json();assert len(info['pools'])==2
        job=(await c.post('/admin/a6/pools/'+id+'/discover',json={},headers=h)).json()
        await app.state.pools.tasks[job['id']]
        assert app.state.pools.jobs[job['id']]['status']=='done'
        preview=(await c.post('/admin/a6/pools/'+id+'/preview',json={},headers=h)).json()
        assert preview['can_publish'] is False and preview['channels']==[]
        assert (await c.post('/admin/a6/pools/'+id+'/verify',json={},headers=h)).status_code==400
        assert not fake.writes
        deleted=await c.request('DELETE','/admin/a6/pools/'+id,json={'revision':a.json()['revision']},headers=h)
        assert deleted.status_code==200
    await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
