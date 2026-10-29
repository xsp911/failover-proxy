"""Outbound runtime regression tests. All sockets are local; no paid requests."""
import asyncio
import copy
import datetime
import json
import ssl
from types import SimpleNamespace

import certifi
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from proxy.proxy_transport import async_client, proxy_fingerprint
from proxy.engine import Engine, GatewayError
from proxy.a6_client import A6Client
from proxy.a6_probe import TargetProbe
from proxy.a6_settings import A6Settings, verify
from proxy import a6_marketplace
from proxy.pelican import PelicanManager
from proxy.pool_manager import PoolManager
from proxy.store import Store
from test_engine import config as configure, MODEL


PROFILE = {'id': 'office', 'name': 'Office', 'scheme': 'http', 'host': '127.0.0.1',
           'port': 9999, 'username': 'user', 'password': 'secret'}


def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'provider.invalid')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('provider.invalid')]), critical=False)
            .sign(key, hashes.SHA256()))
    certfile = tmp_path/'cert.pem'; keyfile = tmp_path/'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    return certfile, keyfile


@pytest.mark.asyncio
async def test_real_connect_preserves_pinned_ip_host_sni_and_verifies_tls(tmp_path, monkeypatch):
    certfile, keyfile = certificate(tmp_path)
    monkeypatch.setattr(certifi, 'where', lambda: str(certfile))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    sni = []; connects = []; requests = []; tasks = set()
    context.set_servername_callback(lambda sock, hostname, ctx: sni.append(hostname))

    async def proxy(reader, writer):
        try:
            connects.append(await reader.readuntil(b'\r\n\r\n'))
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n'); await writer.drain()
            await writer.start_tls(context)
            requests.append(await reader.readuntil(b'\r\n\r\n'))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
            await writer.drain()
        except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except (ssl.SSLError, ConnectionError): pass

    def accepted(reader, writer):
        task = asyncio.create_task(proxy(reader, writer)); tasks.add(task); task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accepted, '127.0.0.1', 0)
    profile = {**PROFILE, 'port': server.sockets[0].getsockname()[1]}
    try:
        async with async_client(profile) as client:
            response = await client.get('https://8.8.8.8/v1/models', headers={'Host': 'provider.invalid'},
                                        extensions={'sni_hostname': 'provider.invalid'})
            assert response.text == 'ok'
            assert client._transport._pool._ssl_context.check_hostname
            assert client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
        async with async_client(profile) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get('https://8.8.8.8/v1/models', headers={'Host': 'wrong.invalid'},
                                 extensions={'sni_hostname': 'wrong.invalid'})
    finally:
        server.close(); await server.wait_closed()
        if tasks: await asyncio.gather(*tasks)
    assert connects[0].startswith(b'CONNECT 8.8.8.8:443 HTTP/1.1')
    assert b'Proxy-Authorization: Basic dXNlcjpzZWNyZXQ=' in connects[0]
    assert sni == ['provider.invalid', 'wrong.invalid']
    assert b'Host: provider.invalid\r\n' in requests[0]
    assert b'Proxy-Authorization' not in requests[0]


@pytest.mark.asyncio
async def test_explicit_mock_transport_does_not_escape_for_proxy_profile():
    seen = []
    async with async_client(PROFILE, httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200))[1])) as client:
        assert (await client.get('https://provider.invalid/models')).status_code == 200
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_dead_proxy_never_falls_back_to_origin():
    origins = []
    async def origin(reader, writer):
        origins.append(True); writer.close()
    server = await asyncio.start_server(origin, '127.0.0.1', 0)
    unused = await asyncio.start_server(lambda r,w: w.close(), '127.0.0.1', 0)
    dead_port = unused.sockets[0].getsockname()[1]; unused.close(); await unused.wait_closed()
    try:
        async with async_client({**PROFILE, 'port': dead_port}, timeout=1) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get('http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]))
    finally:
        server.close(); await server.wait_closed()
    assert not origins


@pytest.mark.asyncio
async def test_engine_proxy_pool_rotation_bounded_and_missing_fail_closed(tmp_path):
    store = Store(tmp_path); configure(store)
    cfg = store.get_config(True); cfg['outbound_proxies'] = [copy.deepcopy(PROFILE)]
    provider = {**cfg['providers'][0], 'proxy_id': PROFILE['id']}
    seen = []
    engine = Engine(store, httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200, json={'data': []}))[1]))
    try:
        async with engine.provider_client(provider, cfg) as first:
            async with engine.provider_client(provider, cfg) as reused: assert reused is first
            cfg['outbound_proxies'][0]['password'] = 'rotated'
            async with engine.provider_client(provider, cfg) as rotated:
                assert rotated is not first and not first.is_closed
            assert not first.is_closed
        assert first.is_closed
        assert (await engine.test_provider(provider, cfg['settings'], cfg))['ok']
        for n in range(40):
            profile = {**PROFILE, 'id': 'proxy'+str(n)}
            async with engine.provider_client({'proxy_id': profile['id']}, {'outbound_proxies': [profile]}): pass
        assert len(engine.proxy_clients) <= 32
        count = len(seen)
        with pytest.raises(GatewayError, match='outbound_proxy_unavailable'):
            async with engine.provider_client({'proxy_id': 'missing'}, cfg): pass
        assert len(seen) == count
    finally:
        pooled = [entry['client'] for entry in engine.proxy_clients.values()]
        await engine.close()
    assert pooled and all(client.is_closed for client in pooled)


@pytest.mark.asyncio
async def test_a6_all_clients_honor_explicit_profile_under_mock(tmp_path):
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path == '/api/user/self': return httpx.Response(200, json={'data': {'id': 7}})
        if request.url.path == '/api/token/': return httpx.Response(200, json={'data': {'total': 0, 'items': []}})
        if request.url.path == '/api/marketplace/channels': return httpx.Response(200, json={'data': []})
        return httpx.Response(401)
    transport = httpx.MockTransport(handle)
    client = A6Client('synthetic_pat_00000000', 7, transport=transport, proxy=PROFILE)
    try:
        assert await client.tokens() == []
        assert TargetProbe(client, SimpleNamespace(), transport).proxy == PROFILE
    finally: await client.close()
    assert (await verify('synthetic_pat_00000000', 7, transport=transport, proxy=PROFILE))['ok']
    assert (await a6_marketplace.tokens('synthetic_pat_00000000', 7, transport=transport, proxy=PROFILE))['total'] == 0
    assert (await a6_marketplace.sellers('synthetic_pat_00000000', 7, MODEL, transport=transport, proxy=PROFILE))['matches'] == 0
    assert len(requests) == 5


def test_proxy_fingerprint_includes_credentials_without_secret():
    first = proxy_fingerprint(PROFILE)
    assert first != proxy_fingerprint({**PROFILE, 'password': 'rotated'})
    assert PROFILE['password'] not in first


@pytest.mark.asyncio
async def test_pelican_resolves_provider_proxy_before_request(tmp_path, monkeypatch):
    store = Store(tmp_path); configure(store)
    cfg = store.get_config(True); cfg['outbound_proxies'] = [PROFILE]
    monkeypatch.setattr(store, 'get_config', lambda *args: cfg)
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '<svg/>'}, 'finish_reason': 'stop'}]})
    manager = PelicanManager(store, tmp_path, httpx.MockTransport(handle))
    provider = {**cfg['providers'][0], 'proxy_id': PROFILE['id']}
    assert (await manager._generate(provider, cfg['settings'], MODEL, 'chat', 'test'))[0] == '<svg/>'
    provider['proxy_id'] = 'missing'
    with pytest.raises(GatewayError, match='outbound_proxy_unavailable'):
        await manager._generate(provider, cfg['settings'], MODEL, 'chat', 'test')
    assert len(seen) == 1


def test_pool_console_profile_is_separate_from_provider(tmp_path):
    store = Store(tmp_path); configure(store)
    cfg = store.get_config(True); cfg['outbound_proxies'] = [PROFILE]
    store.save_config(cfg, cfg['revision'])
    settings = A6Settings(store)
    settings.save('synthetic_pat_00000000', 7, proxy_id=PROFILE['id'])
    assert settings.proxy_id == PROFILE['id'] and settings.proxy()['password'] == PROFILE['password']
    assert settings.status()['proxy_id'] == PROFILE['id']
    settings.save('synthetic_pat_00000000', 7)
    assert settings.proxy_id == PROFILE['id']
    manager = PoolManager(store, settings, SimpleNamespace(), tmp_path, httpx.MockTransport(lambda r: httpx.Response(200)))
    client = manager.client()
    assert client.proxy['id'] == PROFILE['id'] and client.proxy['password'] == PROFILE['password']
    asyncio.run(client.close())
    settings.save('synthetic_pat_00000000', 7, proxy_id=None)
    assert settings.proxy() is None
    settings.save('synthetic_pat_00000000', 7, proxy_id=PROFILE['id'])
    settings.clear()
    assert settings.proxy_id is None
    cfg = store.get_config(True); cfg['outbound_proxies'] = []
    store.save_config(cfg, cfg['revision'])
    with pytest.raises(ValueError, match='unknown_proxy'):
        settings.save('synthetic_pat_00000000', 7, proxy_id=PROFILE['id'])
    assert settings.proxy_id is None and settings.secret() == (None, None)


@pytest.mark.asyncio
async def test_a6_relay_probe_uses_console_proxy_with_mock():
    seen = []
    event = {'choices': [{'index': 0, 'delta': {'content': '1, 2, 3'}, 'finish_reason': 'stop'}]}
    data = 'data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n'
    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={'content-type': 'text/event-stream', 'x-request-id': 'req-probe'}, content=data)
    async def logs(*args): return [{'request_id': 'req-probe', 'token_id': 1, 'actual_channel_id': 2, 'fallback_used': False}]
    client = SimpleNamespace(proxy=PROFILE, logs=logs)
    probe = TargetProbe(client, SimpleNamespace(), httpx.MockTransport(handler))
    _, evidence = await probe.challenge({'protocol': 'chat', 'model': MODEL, 'test_token_id': 1}, 2, 'synthetic_secret',
                                        {'prompt': 'test', 'id': 'test', 'expected_count': 3})
    assert evidence['actual_channel_id'] == 2 and len(seen) == 1
    assert seen[0].url.host == 'api.a6api.com'


@pytest.mark.asyncio
async def test_real_socks5_transport_uses_selected_proxy():
    observed = []; tasks = set()
    async def socks(reader, writer):
        try:
            version, count = await reader.readexactly(2)
            assert version == 5
            methods = await reader.readexactly(count)
            assert 2 in methods
            writer.write(b'\x05\x02'); await writer.drain()
            version, length = await reader.readexactly(2)
            username = await reader.readexactly(length)
            password = await reader.readexactly((await reader.readexactly(1))[0])
            observed.append((username, password))
            writer.write(b'\x01\x00'); await writer.drain()
            version, command, reserved, kind = await reader.readexactly(4)
            assert (version, command, reserved) == (5, 1, 0)
            if kind == 3:
                host = await reader.readexactly((await reader.readexactly(1))[0])
            else:
                assert kind == 1
                host = await reader.readexactly(4)
            port = int.from_bytes(await reader.readexactly(2), 'big')
            observed.append((host, port))
            writer.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50'); await writer.drain()
            observed.append(await reader.readuntil(b'\r\n\r\n'))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok'); await writer.drain()
        finally:
            writer.close(); await writer.wait_closed()
    def accepted(reader, writer):
        task = asyncio.create_task(socks(reader, writer)); tasks.add(task); task.add_done_callback(tasks.discard)
    server = await asyncio.start_server(accepted, '127.0.0.1', 0)
    try:
        profile = {**PROFILE, 'scheme': 'socks5', 'port': server.sockets[0].getsockname()[1]}
        async with async_client(profile) as client:
            assert (await client.get('http://provider.invalid:8080/models')).text == 'ok'
    finally:
        server.close(); await server.wait_closed()
        if tasks: await asyncio.gather(*tasks)
    assert observed[:2] == [(b'user', b'secret'), (b'provider.invalid', 8080)]
    assert b'Host: provider.invalid:8080' in observed[2]


@pytest.mark.asyncio
async def test_engine_generation_proxy_and_missing_reference_fail_closed(tmp_path):
    store = Store(tmp_path); configure(store)
    cfg = store.get_config(True)
    cfg['outbound_proxies'] = [PROFILE]
    cfg['providers'][0]['proxy_id'] = PROFILE['id']
    cfg['providers'] = cfg['providers'][:1]
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'okay'}, 'finish_reason': 'stop'}]})
    engine = Engine(store, httpx.MockTransport(handler))
    try:
        payload = {'model': MODEL, 'messages': []}
        assert (await engine.generate(payload, cfg, 'proxied', 'key'))['choices'][0]['message']['content'] == 'okay'
        cfg['outbound_proxies'] = []
        with pytest.raises(GatewayError, match='outbound_proxy_unavailable'):
            await engine.generate(payload, cfg, 'missing-proxy', 'key')
        assert len(seen) == 1
    finally: await engine.close()
