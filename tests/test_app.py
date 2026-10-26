import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.config import validate_config
from proxy.engine import destination, GatewayError
from test_engine import config, MODEL, frame, wire


@pytest.fixture
def client(tmp_path):
    def upstream(req):
        data=json.loads(req.content) if req.method=='POST' else {}
        if req.method=='GET':return httpx.Response(200,json={'data':[]})
        if data.get('stream'):return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame('okay'),frame(reason='stop')]))
        return httpx.Response(200,json={'choices':[{'index':0,'message':{'role':'assistant','content':'okay'},'finish_reason':'stop'}]})
    app=create_app(tmp_path,transport=httpx.MockTransport(upstream))
    app.state.store.initialize_admin('admin','correct-test-password')
    config(app.state.store)
    with TestClient(app) as c:
        c.store=app.state.store
        c.application=app
        yield c


def login(c):
    r=c.post('/admin/login',json={'username':'admin','password':'correct-test-password'})
    assert r.status_code==200,r.text
    return {'X-CSRF-Token':r.json()['csrf_token']}


def test_auth_csrf_config(client):
    c=client
    assert c.get('/admin/config').status_code==401
    assert c.get('/v1/models').status_code==401
    headers=login(c)
    r=c.get('/admin/config');assert r.status_code==200
    assert 'secret-1' not in r.text
    config=r.json()
    assert c.put('/admin/config',json=config).status_code==403
    r=c.put('/admin/config',json=config,headers=headers);assert r.status_code==200,r.text
    assert c.put('/admin/config',json=config,headers=headers).status_code==409
    assert c.post('/admin/logout',headers=headers).status_code==200
    assert c.get('/admin/session').status_code==401


def test_origin_block(client):
    assert client.post('/admin/login',json={'username':'admin','password':'correct-test-password'},headers={'Origin':'https://evil.invalid'}).status_code==403


def test_keys_and_models(client):
    headers=login(client)
    r=client.post('/admin/keys',json={'name':'agent','models':[MODEL]},headers=headers)
    assert r.status_code==200,r.text
    token=r.json()['token'];id=r.json()['key']['id'];auth={'Authorization':'Bearer '+token}
    assert token not in client.get('/admin/keys').text
    assert client.get('/v1/models',headers=auth).json()['data'][0]['supports_vision'] is True
    r=client.post('/v1/chat/completions',headers=auth,json={'model':MODEL,'messages':[]})
    assert r.status_code==200 and r.json()['model']==MODEL
    r=client.post('/v1/chat/completions',headers=auth,json={'model':MODEL,'messages':[],'stream':True})
    assert r.status_code==200 and '[DONE]' in r.text and 'okay' in r.text
    assert client.application.state.inflight['n']==0
    assert client.patch('/admin/keys/'+id,json={'enabled':False},headers=headers).status_code==200
    assert client.get('/v1/models',headers=auth).status_code==401
    assert client.delete('/admin/keys/'+id,headers=headers).status_code==200


def test_validation_and_stats(client):
    h=login(client)
    c=client.get('/admin/config').json()
    c['settings']['trust_private_upstreams']=False
    c['providers'][0]['base_url']='http://127.0.0.1/v1'
    assert client.put('/admin/config',json=c,headers=h).status_code==400
    stats=client.get('/admin/stats?period=hour')
    assert stats.status_code==200 and 'generated_at' in stats.json()
    assert client.get('/admin/stats?period=wrong').status_code==400
    assert client.get('/admin/health').json()['buffered_mode'] is True
    assert client.post('/admin/providers/1/test',headers=h).json()['ok'] is True


def test_bad_json_and_overrides(client):
    h=login(client)
    token=client.post('/admin/keys',headers=h,json={'name':'test'}).json()['token']
    auth={'Authorization':'Bearer '+token}
    assert client.post('/v1/chat/completions',headers=auth,content='{"model": NaN}').status_code==400
    assert client.post('/v1/chat/completions',headers=auth,json={'model':MODEL,'messages':[],'api_base':'https://evil.invalid'}).status_code==400
    assert client.application.state.inflight['n']==0


@pytest.mark.parametrize('url',['http://example.com/v1','https://localhost/v1','https://127.0.0.1/v1','https://169.254.169.254/v1','https://user:password@example.com/v1','https://example.com/v1?api_key=x'])
def test_ssrf_config(client,url):
    c=client.store.get_config()
    c['settings']['trust_private_upstreams']=False
    c['providers'][0]['base_url']=url
    with pytest.raises(ValueError):validate_config(c)
