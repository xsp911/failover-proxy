import httpx
import pytest
from proxy.app import create_app


@pytest.mark.asyncio
async def test_reveal_requires_admin_csrf_no_list_secret_and_no_cache(tmp_path):
    app=create_app(tmp_path,transport=httpx.MockTransport(lambda _:httpx.Response(200,json={})));app.state.store.initialize_admin('admin','test-password')
    created=app.state.store.create_key('test',[]);token=created['token'];key=created['key']['id']
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
            path='/admin/keys/'+key+'/reveal'
            assert (await c.post(path,json={})).status_code==401
            r=await c.post('/admin/login',json={'username':'admin','password':'test-password'});h={'X-CSRF-Token':r.json()['csrf_token']}
            assert token not in (await c.get('/admin/keys')).text
            assert (await c.post(path,json={})).status_code==403
            r=await c.post(path,json={},headers=h);assert r.status_code==200 and r.json()['token']==token
            assert 'no-store' in r.headers['cache-control']
            assert (await c.post('/admin/keys/missing/reveal',json={},headers=h)).status_code==409
            await c.post('/admin/logout',headers=h)
            assert (await c.post(path,json={},headers=h)).status_code==401
    finally:
        await app.state.pelican.close();await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
