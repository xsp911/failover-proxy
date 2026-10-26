import json

import httpx
import pytest

from proxy.app import create_app
from test_engine import config, MODEL, frame, wire


def setup(app):
    config(app.state.store)
    c=app.state.store.get_config()
    c['models'][0].update(context_length=900000,max_output_tokens=32768)
    app.state.store.save_config(c,c['revision'])
    key=app.state.store.create_key('feedback test',[])['token']
    return {'Authorization':'Bearer '+key}


@pytest.mark.asyncio
async def test_api_metadata_and_attribution_json_sse(tmp_path):
    def upstream(req):
        body=json.loads(req.content)
        if req.url.host=='p1.invalid':return httpx.Response(503,json={'error':{'message':'down'}})
        if body.get('stream'):
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame('okay'),frame(reason='stop'),{'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':71}}]))
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'okay'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':71}})
    app=create_app(tmp_path,transport=httpx.MockTransport(upstream));h=setup(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        m=(await client.get('/v1/models',headers=h)).json()['data'][0]
        assert m['context_length']==900000 and m['max_output_tokens']==32768
        for streaming in (False,True):
            r=await client.post('/v1/chat/completions',headers=h,json={'model':MODEL,'messages':[],'stream':streaming})
            assert r.status_code==200,r.text
            assert r.headers['x-upstream']=='Provider 2'
            assert r.headers['x-request-id'].startswith('req_')
            if streaming:
                events=[json.loads(x[6:]) for x in r.text.split('\n\n') if x.startswith('data: ') and x[6:]!='[DONE]']
                assert next(x['usage'] for x in events if 'usage' in x)['total_tokens']==71
            else:assert r.json()['usage']['total_tokens']==71
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_context_error_safe_detail_http_and_no_fallback(tmp_path):
    attempts=[]
    def upstream(req):
        attempts.append(req.url.host)
        return httpx.Response(400,json={'error':{'code':'context_length_exceeded','message':'Maximum context length exceeded; prompt=TOPSECRET https://secret.invalid/?api_key=sk-secret'}})
    app=create_app(tmp_path,transport=httpx.MockTransport(upstream));h=setup(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        r=await client.post('/v1/chat/completions',headers=h,json={'model':MODEL,'messages':[]})
    assert r.status_code==400 and r.json()['error']['code']=='context_length_exceeded'
    assert 'context' in r.json()['error']['message'].lower()
    assert all(x not in r.text for x in ('TOPSECRET','sk-secret','https://secret'))
    assert attempts==['p1.invalid']
    assert not app.state.engine.cooldowns
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_metadata_unknown_is_null_not_invented(tmp_path):
    app=create_app(tmp_path);config(app.state.store)
    token=app.state.store.create_key('test',[])['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        row=(await c.get('/v1/models',headers={'Authorization':'Bearer '+token})).json()['data'][0]
    assert row['context_length'] is None and row['max_output_tokens'] is None
    await app.state.engine.close()
