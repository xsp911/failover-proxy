import httpx
import pytest

from proxy.app import create_app
from test_engine import config


@pytest.mark.asyncio
async def test_auth_csrf_private_image_csp_and_no_public_gallery(tmp_path):
    app=create_app(tmp_path);app.state.store.initialize_admin('admin','password');config(app.state.store)
    id='pelican_'+'a'*32
    app.state.pelican.db.save({'id':id,'status':'done','svg_available':True},'<svg xmlns="http://www.w3.org/2000/svg"><text>safe</text></svg>')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        assert (await client.get('/admin/pelican')).status_code==401
        assert (await client.get('/admin/pelican/'+id+'/svg')).status_code==401
        r=await client.post('/admin/login',json={'username':'admin','password':'password'});csrf=r.json()['csrf_token']
        assert (await client.post('/admin/pelican',json={})).status_code==403
        assert (await client.post('/admin/pelican',json={},headers={'X-CSRF-Token':csrf})).status_code==400
        info=(await client.get('/admin/pelican')).json();assert info['timeout_seconds']==300
        image=await client.get('/admin/pelican/'+id+'/svg');assert image.status_code==200
        csp=image.headers['content-security-policy'];assert "script-src 'none'" in csp and 'sandbox' in csp and "connect-src 'none'" in csp
        assert image.headers['cache-control']=='no-store' and image.headers['x-content-type-options']=='nosniff'
        assert (await client.get('/admin/pelican/invalid/svg')).status_code==404
    await app.state.pelican.close();await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
