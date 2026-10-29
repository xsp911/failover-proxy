import pytest
import httpx
from proxy.app import create_app


@pytest.mark.asyncio
async def test_local_pool_listing_never_needs_a6_or_credentials(tmp_path):
    calls=[]
    def upstream(req):calls.append(req);raise AssertionError('Local list must not request upstream')
    app=create_app(tmp_path,transport=httpx.MockTransport(upstream));app.state.store.initialize_admin('admin','test-password')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
            assert (await c.get('/admin/a6/pools?include_tokens=false')).status_code==401
            r=await c.post('/admin/login',json={'username':'admin','password':'test-password'});assert r.status_code==200
            r=await c.get('/admin/a6/pools?include_tokens=false');assert r.status_code==200,r.text
            data=r.json();assert data['pools']==[] and data['tokens']==[] and data['tokens_included'] is False
            assert not calls
    finally:
        await app.state.pelican.close();await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()


@pytest.mark.asyncio
async def test_separate_catalog_uses_cached_validated_token_client(tmp_path):
    calls=[]
    def upstream(req):
        calls.append(req.url.path);assert req.url.host=='api.a6api.com'
        return httpx.Response(200,json={'success':True,'data':{'total':1,'items':[{'id':1,'name':'key','status':1,'key':'never-return-raw-secret'}]}})
    app=create_app(tmp_path,transport=httpx.MockTransport(upstream));app.state.store.initialize_admin('admin','test-password');app.state.a6.save('SYNTHETIC_LONG_PAT_FOR_FAST_LIST_123',42)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
            await c.post('/admin/login',json={'username':'admin','password':'test-password'})
            assert (await c.get('/admin/a6/pools?include_tokens=false')).status_code==200;assert not calls
            r=await c.get('/admin/a6/pools/tokens');assert r.status_code==200 and r.json()['tokens'][0]['id']==1
            assert 'never-return-raw-secret' not in r.text and len(calls)==1
            assert (await c.get('/admin/a6/pools?include_tokens=false')).status_code==200 and len(calls)==1
    finally:
        await app.state.pelican.close();await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
