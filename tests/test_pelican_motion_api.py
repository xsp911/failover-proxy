import base64
import httpx
import pytest
from proxy.app import create_app


@pytest.mark.asyncio
async def test_motion_analysis_local_cached_authenticated_frames(tmp_path,monkeypatch):
    app=create_app(tmp_path);app.state.store.initialize_admin('admin','password');id='pelican_'+'b'*32
    app.state.pelican.db.save({'id':id,'status':'done','svg_available':True},'<svg xmlns="http://www.w3.org/2000/svg"><circle r="20"/></svg>')
    calls=[]
    async def capture(svg):
        calls.append(svg);return {'shape_count':1,'frame_times':[0,.5,1,1.5],'changed_pixel_shares':[0,0,0],'motion_detected':False,'semantic_quality_evaluated':False,'frames':[base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()]*4}
    monkeypatch.setattr('proxy.pelican_motion.capture',capture)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        assert (await c.get('/admin/pelican/'+id+'/motion')).status_code==401
        login=await c.post('/admin/login',json={'username':'admin','password':'password'});headers={'X-CSRF-Token':login.json()['csrf_token']}
        assert (await c.post('/admin/pelican/'+id+'/motion',json={})).status_code==403
        path='/admin/pelican/'+id+'/motion'
        first=await c.post(path,json={},headers=headers);assert first.status_code==200 and not first.json()['semantic_quality_evaluated'] and 'frames' not in first.json()
        assert (await c.post(path,json={},headers=headers)).status_code==200 and len(calls)==1
        image=await c.get('/admin/pelican/'+id+'/frames/0');assert image.status_code==200 and image.headers['content-type']=='image/png'
        assert (await c.get('/admin/pelican/'+id+'/frames/4')).status_code==404
    await app.state.pelican.close();await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
