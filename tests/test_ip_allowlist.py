import pytest
from proxy.ip_allowlist import normalize_access,peer_allowed,APIIPAllowlistMiddleware


def test_canonical_cidr_and_deduplicate():
    assert normalize_access({'enabled':True,'allowed_ips':['192.168.1.1/24','192.168.1.0/24','2001:db8::1/64']})=={'enabled':True,'allowed_ips':['192.168.1.0/24','2001:db8::/64']}


@pytest.mark.parametrize('entry',['host.invalid','1.2.3.4/99','127.1','::1%lo','https://1.2.3.4','',True,None])
def test_invalid_cidr_rejected(entry):
    with pytest.raises(ValueError):normalize_access({'enabled':True,'allowed_ips':[entry]})


def test_enabled_empty_rejected_and_missing_disabled():
    with pytest.raises(ValueError,match='empty_ip_allowlist'):normalize_access({'enabled':True,'allowed_ips':[]})
    assert peer_allowed(None,'whatever')
    assert peer_allowed({'enabled':False,'allowed_ips':[]},None)
    assert not peer_allowed({'enabled':True,'allowed_ips':[]},'127.0.0.1')


def test_ipv4_ipv6_and_mapped_addresses():
    policy=normalize_access({'enabled':True,'allowed_ips':['192.168.1.1/24','2001:db8::/64']})
    assert peer_allowed(policy,'192.168.1.99')
    assert peer_allowed(policy,'::ffff:192.168.1.99')
    assert peer_allowed(policy,'2001:db8::ff')
    assert not peer_allowed(policy,'192.168.2.1')
    assert not peer_allowed(policy,'2001:db8:1::1')
    assert not peer_allowed(policy,None)
    assert not peer_allowed(policy,'garbage')
    assert normalize_access({'enabled':True,'allowed_ips':['::ffff:192.0.2.42/120']})['allowed_ips']==['192.0.2.0/24']


@pytest.mark.asyncio
async def test_filter_only_api_ignores_forwarded_headers_and_body():
    class Store:
        def get_config(self):return {'api_access':{'enabled':True,'allowed_ips':['192.0.2.0/24']}}
    calls=[]
    async def app(scope,receive,send):calls.append(scope['path'])
    async def receive():raise AssertionError('denied body must not be read')
    async def send(message):messages.append(message)
    middleware=APIIPAllowlistMiddleware(app,Store());messages=[]
    scope={'type':'http','path':'/v1/chat/completions','client':('203.0.113.2',1000),'headers':[(b'x-forwarded-for',b'192.0.2.5'),(b'x-real-ip',b'192.0.2.5')]}
    await middleware(scope,receive,send);assert not calls and messages[0]['status']==403
    for path in ('/','/admin/config','/health/liveliness','/v10/not-api'):
        await middleware({**scope,'path':path},receive,send)
    assert calls==['/','/admin/config','/health/liveliness','/v10/not-api']
    await middleware({**scope,'client':('192.0.2.55',5)},receive,send);assert calls[-1]=='/v1/chat/completions'
