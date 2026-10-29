import httpx
import pytest
from fastapi.testclient import TestClient
from proxy.app import create_app


def setup(tmp_path,handler=None):
    handler=handler or (lambda request:httpx.Response(200,json={'ip':'203.0.113.45'}))
    app=create_app(tmp_path,transport=httpx.MockTransport(handler));app.state.store.initialize_admin('admin','test-password-long');return app


def login(client):
    r=client.post('/admin/login',json={'username':'admin','password':'test-password-long'});assert r.status_code==200
    return {'X-CSRF-Token':r.json()['csrf_token']}


def test_access_filter_live_save_no_spoof_or_admin_lockout(tmp_path):
    app=setup(tmp_path)
    with TestClient(app,client=('198.51.100.10',4444)) as client:
        h=login(client);token=client.post('/admin/keys',json={'name':'test'},headers=h).json()['token'];auth={'Authorization':'Bearer '+token}
        config=client.get('/admin/config').json();config['api_access']={'enabled':True,'allowed_ips':['192.168.1.1/24']}
        r=client.put('/admin/config',json=config,headers=h);assert r.status_code==200,r.text
        assert r.json()['api_access']['allowed_ips']==['192.168.1.0/24']
        assert client.get('/v1/models',headers={**auth,'X-Forwarded-For':'192.168.1.22','X-Real-IP':'192.168.1.22'}).status_code==403
        assert client.post('/v1/chat/completions',headers=auth,content=b'not json').status_code==403
        assert client.get('/admin/config').status_code==200 and client.get('/health/liveliness').status_code==200
        info=client.get('/admin/access').json();assert info['client_ip']=='198.51.100.10' and info['forwarded_headers_trusted'] is False
        config=client.get('/admin/config').json();config['api_access']['allowed_ips']=['198.51.100.10'];assert client.put('/admin/config',json=config,headers=h).status_code==200
        assert client.get('/v1/models',headers=auth).status_code==200
        config=client.get('/admin/config').json();config['api_access']={'enabled':False,'allowed_ips':[]};assert client.put('/admin/config',json=config,headers=h).status_code==200
        assert client.get('/v1/models',headers=auth).status_code==200


def test_proxy_check_auth_masking_echo_and_csrf(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request);assert request.url.host=='api.ipify.org';assert 'authorization' not in request.headers;assert 'cookie' not in request.headers
        return httpx.Response(200,json={'ip':'203.0.113.45'})
    app=setup(tmp_path,handler)
    with TestClient(app) as client:
        assert client.post('/admin/proxies/p/check',json={}).status_code==401
        h=login(client);config=client.get('/admin/config').json();config['outbound_proxies']=[{'id':'p','name':'Proxy','scheme':'http','host':'127.0.0.1','port':3128,'username':'user','password':'proxy-secret'}]
        result=client.put('/admin/config',json=config,headers=h);assert result.status_code==200,result.text;assert 'proxy-secret' not in result.text and '_password_encrypted' not in result.text
        assert client.post('/admin/proxies/p/check',json={}).status_code==403
        result=client.post('/admin/proxies/p/check',json={},headers=h);assert result.status_code==200 and result.json()['ip']=='203.0.113.45';assert len(calls)==1
        assert client.post('/admin/proxies/missing/check',json={},headers=h).status_code==404


@pytest.mark.asyncio
@pytest.mark.parametrize('response',[httpx.Response(302,headers={'location':'http://169.254.169.254'}),httpx.Response(200,json={'ip':'not-an-ip'}),httpx.Response(200,content=b'x'*1025),httpx.Response(200,json={'ip':'::1%eth0'})])
async def test_proxy_echo_failures_sanitized(response):
    from proxy.proxy_check import check_proxy
    profile={'id':'p','name':'test','scheme':'http','host':'127.0.0.1','port':3128,'username':'u','password':'do-not-leak'}
    result=await check_proxy(profile,httpx.MockTransport(lambda _:response));assert result['ok'] is False and 'do-not-leak' not in str(result)
