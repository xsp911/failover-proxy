"""Offline, bounded endpoint diagnostics never retain request identities."""
import json

import httpx
import pytest

from proxy import a6_client
from proxy.a6_client import A6Client, A6Error


PAT = 'private-endpoint-pat-never-emit'
REQUEST_ID = 'private-request-identity-never-emit'
BUCKETS = {'tokens', 'marketplace', 'subscriptions', 'pins', 'logs', 'status', 'other'}
FLAT_FIELDS = {'requests', 'rate_limited', 'last_status', 'last_error_at',
               'cooldown_until', 'inflight', 'max_inflight'}


def ok():
    return httpx.Response(200, json={'success': True, 'data': {}})


def assert_timings(metrics):
    assert metrics['total_duration_s'] >= 0
    assert metrics['max_duration_s'] >= 0
    assert metrics['max_duration_s'] <= metrics['total_duration_s']


@pytest.mark.asyncio
async def test_endpoint_metrics_count_actual_get_retry_attempts(monkeypatch):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', 0)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) <= 2:
            return httpx.Response((429, 503)[len(calls) - 1], headers={'Retry-After': '0'})
        return ok()

    transport = httpx.MockTransport(handler)
    first = A6Client(PAT, 23, transport=transport)
    second = A6Client(PAT, 23, transport=transport)
    try:
        await first.request('GET', '/api/token/918273645/key')
        diagnostics = second.diagnostics()
        assert FLAT_FIELDS <= diagnostics.keys()
        assert diagnostics['requests'] == len(calls) == 3
        assert diagnostics['rate_limited'] == 2
        assert diagnostics['inflight'] == 0
        metrics = diagnostics['endpoints']['tokens']
        assert metrics['requests'] == 3
        assert metrics['errors'] == 2
        assert metrics['http_429'] == 1
        assert metrics['http_5xx'] == 1
        assert metrics['last_status'] == 200
        assert_timings(metrics)
        # Nested snapshots must not allow callers to corrupt shared counters.
        metrics['requests'] = -1
        assert first.diagnostics()['endpoints']['tokens']['requests'] == 3
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_endpoint_metrics_fixed_buckets_and_no_request_identity(monkeypatch):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', 0)
    paths = [
        ('/api/token/918273645/key', 'tokens'),
        ('/api/marketplace/channels/search', 'marketplace'),
        ('/api/subscriptions/918273645/config', 'subscriptions'),
        ('/api/marketplace/pins', 'pins'),
        ('/api/log/self', 'logs'),
        ('/api/status', 'status'),
        ('/api/private-918273645/secret-value', 'other'),
    ]
    client = A6Client(PAT, 23, transport=httpx.MockTransport(lambda request: ok()))
    try:
        for path, bucket in paths:
            await client.request('GET', path, params={'request_id': REQUEST_ID, 'token_id': 918273645},
                                 body={'secret': PAT})
            assert client.diagnostics()['endpoints'][bucket]['requests'] == 1
        diagnostics = client.diagnostics()
        assert set(diagnostics['endpoints']) == BUCKETS
        assert diagnostics['requests'] == len(paths)
        for metrics in diagnostics['endpoints'].values():
            assert metrics['requests'] == 1
            assert metrics['errors'] == metrics['http_429'] == metrics['http_5xx'] == 0
            assert metrics['last_status'] == 200
            assert_timings(metrics)
        serialized = json.dumps(diagnostics)
        for secret in (PAT, REQUEST_ID, '918273645', 'secret-value', 'request_id', '/api/'):
            assert secret not in serialized
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_endpoint_metrics_transport_errors_finish_attempt(monkeypatch):
    monkeypatch.setattr(a6_client, '_REQUEST_SPACING', 0)

    def handler(request):
        raise httpx.ConnectError(PAT + REQUEST_ID, request=request)

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error, match='a6_connection_failed'):
            await client.request('GET', '/api/status')
        diagnostics = client.diagnostics()
        metrics = diagnostics['endpoints']['status']
        assert diagnostics['requests'] == metrics['requests'] == 1
        assert diagnostics['inflight'] == 0
        assert metrics['errors'] == 1
        assert metrics['http_429'] == metrics['http_5xx'] == 0
        assert metrics['last_status'] is None
        assert_timings(metrics)
        assert PAT not in json.dumps(diagnostics)
        assert REQUEST_ID not in json.dumps(diagnostics)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_endpoint_metrics_rejected_paths_do_not_count_attempts():
    client = A6Client(PAT, 23, transport=httpx.MockTransport(lambda request: ok()))
    try:
        with pytest.raises(A6Error, match='invalid_a6_path'):
            await client.request('GET', '/api/status?secret=' + PAT)
        diagnostics = client.diagnostics()
        assert diagnostics['requests'] == 0
        assert all(metrics['requests'] == 0 for metrics in diagnostics['endpoints'].values())
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 429, 500])
async def test_endpoint_metrics_write_errors_never_add_retry_attempts(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Retry-After': '0'})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error):
            await client.request('PUT', '/api/subscriptions/918273645/config', write=True)
        metrics = client.diagnostics()['endpoints']['subscriptions']
        assert metrics['requests'] == len(calls) == 1
        assert metrics['errors'] == 1
        assert metrics['http_429'] == int(status == 429)
        assert metrics['http_5xx'] == int(status >= 500)
        assert metrics['last_status'] == status
        assert_timings(metrics)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_endpoint_metrics_failed_json_is_error_with_http_200():
    client = A6Client(PAT, 23, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b'invalid json')))
    try:
        with pytest.raises(A6Error, match='a6_invalid_response'):
            await client.request('GET', '/api/status')
        metrics = client.diagnostics()['endpoints']['status']
        assert metrics['requests'] == metrics['errors'] == 1
        assert metrics['last_status'] == 200
        assert metrics['http_429'] == metrics['http_5xx'] == 0
        assert_timings(metrics)
    finally:
        await client.close()
