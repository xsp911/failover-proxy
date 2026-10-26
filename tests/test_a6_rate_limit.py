"""Offline A6 account throttling, safe diagnostics, token cache isolation."""
import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from proxy import a6_client
from proxy.a6_client import A6Client, A6Error

PAT = 'private-pat-never-emit'


def ok(data=None):
    return httpx.Response(200, json={'success': True, 'data': {} if data is None else data})


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [429, 503])
async def test_readonly_get_retries_rate_limit_with_shared_metrics(monkeypatch, status):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', .001)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Retry-After': '0'}) if len(calls) < 3 else ok({'done': True})

    transport = httpx.MockTransport(handler)
    first = A6Client(PAT, 23, transport=transport)
    second = A6Client(PAT, 23, transport=transport)
    try:
        assert await first.request('GET', '/api/status') == {'done': True}
        stats = second.diagnostics()
        assert stats['requests'] == 3
        assert stats['rate_limited'] == 2
        assert stats['last_status'] == 200
        assert stats['last_error_at'] is not None
        assert stats['inflight'] == 0
        assert stats['max_inflight'] == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_get_retries_at_most_twice(monkeypatch):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', .001)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '0'})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error, match='a6_http_429'):
            await client.request('GET', '/api/status')
        assert len(calls) == 3
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('method,write,status', [('POST', False, 429), ('PUT', True, 503), ('GET', True, 429)])
async def test_non_readonly_requests_are_never_retried(method, write, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Retry-After': '0'})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error):
            await client.request(method, '/api/subscriptions', write=write)
        assert len(calls) == 1
        assert client.diagnostics()['rate_limited'] == 1
    finally:
        await client.close()


def test_retry_after_is_bounded_and_supports_http_date():
    for value in ['99999999999', 'inf', '1e9999']:
        assert a6_client._retry_after(value) == 30
    assert a6_client._retry_after('-5') == 0
    assert a6_client._retry_after('0') == 0
    assert a6_client._retry_after(None) == 1
    assert a6_client._retry_after(PAT) == 1
    assert a6_client._retry_after('nan') == 1
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120))
    assert a6_client._retry_after(future) == 30


@pytest.mark.asyncio
async def test_cooldown_shared_across_clients_and_total_budget(monkeypatch):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', .001)
    monkeypatch.setattr(a6_client, '_REQUEST_TIMEOUT', .04)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={'Retry-After': '99999999'})

    transport = httpx.MockTransport(handler)
    first = A6Client(PAT, 23, transport=transport)
    second = A6Client(PAT, 23, transport=transport)
    try:
        with pytest.raises(A6Error, match='a6_route_write_uncertain'):
            await first.request('PUT', '/api/subscriptions/1/config', write=True)
        assert 29 <= first.diagnostics()['cooldown_until'] - time.time() <= 30
        started = time.monotonic()
        with pytest.raises(A6Error, match='a6_connection_failed'):
            await second.request('GET', '/api/status')
        assert time.monotonic() - started < .2
        assert len(calls) == 1
        assert second.diagnostics()['inflight'] == 0
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_shared_account_concurrency_two_and_minimum_spacing():
    starts = []
    inflight = 0
    maximum = 0

    async def handler(request):
        nonlocal inflight, maximum
        starts.append(time.monotonic())
        inflight += 1
        maximum = max(maximum, inflight)
        try:
            await asyncio.sleep(.22)
            return ok()
        finally:
            inflight -= 1

    transport = httpx.MockTransport(handler)
    clients = [A6Client(PAT, 23, transport=transport) for _ in range(4)]
    try:
        await asyncio.gather(*(client.request('GET', '/api/status') for client in clients))
        assert maximum == 2
        assert all(second - first >= .145 for first, second in zip(starts, starts[1:]))
        assert clients[0].diagnostics()['max_inflight'] == 2
        assert clients[0].diagnostics()['requests'] == 4
    finally:
        for client in clients:
            await client.close()


def production_mock(monkeypatch, handler):
    """Exercise transport=None cache without making any network connection."""
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs['transport'] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(a6_client.httpx, 'AsyncClient', factory)


@pytest.mark.asyncio
async def test_production_tokens_cached_across_clients_deepcopied_and_expire(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return ok({'total': 1, 'items': [{'id': 7, 'name': 'original', 'model_limits': ['safe'], 'key': PAT}]})

    production_mock(monkeypatch, handler)
    first = A6Client(PAT, 23)
    second = A6Client(PAT, 23)
    try:
        one, two = await asyncio.gather(first.tokens(), second.tokens())
        assert len(calls) == 1
        one[0]['model_limits'].append('mutated')
        one[0]['name'] = 'mutated'
        assert two[0]['model_limits'] == ['safe']
        assert (await second.tokens())[0]['name'] == 'original'
        assert PAT not in json.dumps(two)
        cache = a6_client._TOKEN_CACHES[asyncio.get_running_loop()][first._account_key]
        assert 29 <= cache.expires - asyncio.get_running_loop().time() <= 30
        cache.expires = 0
        await first.tokens()
        assert len(calls) == 2
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_cache_never_applies_to_mock_transport_or_pins_or_readback(monkeypatch):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', .001)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == '/api/token/':
            return ok({'total': 0, 'items': []})
        if request.url.path == '/api/marketplace/pins':
            return ok([])
        return ok({'id': 1, 'token_id': 7})

    # Cache bypass even when mock clients share the same account/transport.
    transport = httpx.MockTransport(handler)
    client = A6Client(PAT, 23, transport=transport)
    try:
        await client.tokens()
        await client.tokens()
        assert calls == ['/api/token/', '/api/token/']
    finally:
        await client.close()
    production_mock(monkeypatch, handler)
    client = A6Client(PAT, 23)
    try:
        await client.pins()
        await client.pins()
        await client.get_subscription(1)
        await client.get_subscription(1)
        assert calls.count('/api/marketplace/pins') == 2
        assert calls.count('/api/subscriptions/1') == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_diagnostics_sanitized_copied_and_transport_account_isolated(monkeypatch):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', .001)
    transport = httpx.MockTransport(lambda request: httpx.Response(401, headers={'secret': PAT}, text=PAT))
    clients = [A6Client(PAT, 23, transport=transport),
               A6Client(PAT, 24, transport=transport),
               A6Client(PAT, 23, transport=httpx.MockTransport(lambda request: ok()))]
    try:
        with pytest.raises(A6Error, match='a6_auth_rejected') as caught:
            await clients[0].request('GET', '/api/status')
        stats = clients[0].diagnostics()
        assert set(stats) == {'requests', 'rate_limited', 'last_status', 'last_error_at', 'cooldown_until', 'inflight', 'max_inflight','endpoints'}
        encoded = json.dumps(stats) + str(caught.value)
        assert PAT not in encoded
        assert hashlib.sha256(PAT.encode()).hexdigest() not in encoded
        assert 'authorization' not in encoded.lower()
        assert 'secret' not in encoded.lower()
        stats['requests'] = 999
        assert clients[0].diagnostics()['requests'] == 1
        assert all(client.diagnostics()['requests'] == 0 for client in clients[1:])
    finally:
        for client in clients:
            await client.close()


def test_production_cache_and_locks_are_event_loop_isolated(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return ok({'total': 0, 'items': []})

    production_mock(monkeypatch, handler)

    async def run():
        client = A6Client(PAT, 23)
        try:
            await client.tokens()
            await client.tokens()
            assert client.diagnostics()['requests'] == 1
        finally:
            await client.close()

    asyncio.run(run())
    asyncio.run(run())
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_production_token_cache_is_account_scoped_and_errors_not_cached(monkeypatch):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', .001)
    calls = []

    def handler(request):
        calls.append(request.headers['new-api-user'])
        if len(calls) == 1:
            return httpx.Response(401)
        return ok({'total': 1, 'items': [{'id': 7, 'name': request.headers['new-api-user']}]})

    production_mock(monkeypatch, handler)
    first = A6Client(PAT, 23)
    second = A6Client(PAT, 24)
    try:
        with pytest.raises(A6Error, match='a6_auth_rejected'):
            await first.tokens()
        assert (await first.tokens())[0]['name'] == '23'
        assert (await second.tokens())[0]['name'] == '24'
        assert (await first.tokens())[0]['name'] == '23'
        assert calls == ['23', '23', '24']
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_cancelled_inflight_request_releases_slot():
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(client.request('GET', '/api/status'))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.diagnostics()['inflight'] == 0
        assert client._request_state().slots._value == 2
    finally:
        await client.close()
