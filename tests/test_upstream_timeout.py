import json
import time

import httpx
import pytest

from proxy import diagnostics as d
from proxy.engine import Completion, Engine, GatewayError, parse_sse
from proxy.store import Store
from test_engine import MODEL, config, frame, wire


SECRET = 'TOPSECRET sk-private https://secret.invalid/request-id'
TIMEOUT_ERROR = {
    'error': {
        'message': '等待上游响应超时 ' + SECRET,
        'type': 'upstream_timeout',
        'code': 'upstream_timeout',
        'reason': SECRET,
        'request_id': SECRET,
    }
}


@pytest.mark.parametrize('status', [500, 200])
@pytest.mark.parametrize('field', ['code', 'type'])
@pytest.mark.parametrize('nested', [True, False])
def test_exact_timeout_code_or_type(status, field, nested):
    fields = {field: 'upstream_timeout', 'message': SECRET}
    payload = {'error': fields} if nested else fields
    assert d.classify(payload, status) == (
        'upstream_timeout', 504, True, d.DETAILS['upstream_timeout']
    )
    assert d.diagnostic('upstream_timeout') == d.DETAILS['upstream_timeout']
    assert SECRET not in d.classify(payload, status)[3]


@pytest.mark.parametrize('status', [500, 200])
def test_concrete_provider_timeout_keeps_only_controlled_detail(status):
    kind, public_status, retryable, detail = d.classify(TIMEOUT_ERROR, status)
    assert (kind, public_status, retryable) == ('upstream_timeout', 504, True)
    assert detail == 'The upstream request timed out. Other configured routes may be tried.'
    assert '等待' not in detail and SECRET not in detail


@pytest.mark.parametrize('message', [
    'Upstream request timed out.', 'upstream timed out', 'Upstream timeout.',
])
@pytest.mark.parametrize('status', [500, 200])
def test_allowlisted_full_timeout_messages(message, status):
    assert d.classify({'error': {'message': message}}, status)[0] == 'upstream_timeout'


@pytest.mark.parametrize('payload', [
    {'error': {'message': 'Explain timeout in this prompt: ' + SECRET}},
    {'error': {'message': 'timeout'}},
    {'error': {'message': 'request timeout'}},
    {'error': {'message': 'prefix upstream timed out suffix'}},
    {'error': {'message': 'upstream timeout. ' + SECRET}},
    {'error': {'message': 'The prompt mentions upstream_timeout'}},
    {'error': {'code': 'timeout'}},
    {'error': {'type': 'request_timeout'}},
    {'error': {'code': 'UPSTREAM_TIMEOUT'}},
    {'error': {'code': ' upstream_timeout'}},
    {'error': {'code': 'upstream_timeout_suffix'}},
    {'error': {'code': ['upstream_timeout'], 'type': {'code': 'upstream_timeout'}}},
    {'error': {'reason': 'upstream_timeout', 'request_id': 'upstream_timeout'}},
    {'prompt': 'upstream_timeout', 'message': 'Describe timeout handling'},
    {'choices': [{'message': {'content': 'upstream timeout'}}]},
    None,
])
@pytest.mark.parametrize('status', [500, 200])
def test_arbitrary_timeout_text_and_nonexact_codes_stay_generic(payload, status):
    kind, public_status, retryable, detail = d.classify(payload, status)
    assert kind == ('upstream_http_500' if status == 500 else 'upstream_sse_error')
    assert public_status == 502 and retryable
    assert SECRET not in detail


@pytest.mark.parametrize('status', [400, 401, 403, 408, 429, 502, 503, 504])
def test_timeout_body_does_not_override_other_http_statuses(status):
    assert d.classify(TIMEOUT_ERROR, status) == d.classify(None, status)
    if status == 401:
        assert 'credential' in d.classify(TIMEOUT_ERROR, status)[3]


@pytest.mark.parametrize('status', [400, 200])
def test_context_classification_preserved(status):
    result = d.classify({'error': {'code': 'context_length_exceeded', 'message': SECRET}}, status)
    assert result == ('context_length_exceeded', 400, False, d.DETAILS['context_length_exceeded'])


@pytest.mark.parametrize('status', [500, 200])
def test_exact_timeout_code_outranks_echoed_context_message(status):
    payload = {'error': {'type': 'upstream_timeout', 'message': 'Maximum context length exceeded ' + SECRET}}
    assert d.classify(payload, status) == ('upstream_timeout', 504, True, d.DETAILS['upstream_timeout'])


@pytest.mark.asyncio
async def test_sse_timeout_is_retryable_without_secret_detail():
    response = httpx.Response(200, content=wire([frame('partial-secret'), TIMEOUT_ERROR], False))
    with pytest.raises(GatewayError) as error:
        await parse_sse(response, Completion(MODEL, 100000, 100))
    assert (error.value.kind, error.value.status, error.value.retryable) == ('upstream_timeout', 504, True)
    assert error.value.detail == d.DETAILS['upstream_timeout']
    assert SECRET not in str(error.value) + error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_timeout_engine_fails_over_and_records_safe_kind(tmp_path, stream):
    store = Store(tmp_path)
    config(store)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == 'p1.invalid':
            if stream:
                return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                      content=wire([frame('partial-secret'), TIMEOUT_ERROR], False))
            return httpx.Response(500, json=TIMEOUT_ERROR)
        if stream:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=wire([frame('success'), frame(reason='stop')]))
        return httpx.Response(200, json={'choices': [
            {'index': 0, 'message': {'role': 'assistant', 'content': 'success'}, 'finish_reason': 'stop'}
        ]})

    engine = Engine(store, httpx.MockTransport(handler))
    try:
        result = await engine.generate({'model': MODEL, 'messages': [], 'stream': stream},
                                       store.get_config(True), 'timeout-test', 'key')
        output = b''.join(result.wire()).decode() if stream else json.dumps(result)
        assert calls == ['p1.invalid', 'p2.invalid']
        assert 'success' in output and 'partial-secret' not in output and SECRET not in output
        assert engine.cooldowns['1'] > time.time()
        stats = store.stats('hour')
        assert stats['summary']['errors'] == 1 and stats['summary']['success'] == 1
        assert stats['summary']['switches'] == 1
        assert stats['recent_errors'][0]['kind'] == 'upstream_timeout'
        assert stats['recent_errors'][0]['status'] == (200 if stream else 500)
        assert SECRET not in json.dumps(stats)
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_exhausted_timeout_routes_return_504_without_raw_body(tmp_path, stream):
    store = Store(tmp_path)
    config(store)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if stream:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=wire([TIMEOUT_ERROR], False))
        return httpx.Response(500, json=TIMEOUT_ERROR)

    engine = Engine(store, httpx.MockTransport(handler))
    try:
        with pytest.raises(GatewayError) as error:
            await engine.generate({'model': MODEL, 'messages': [], 'stream': stream},
                                  store.get_config(True), 'timeout-test', 'key')
        assert calls == ['p1.invalid', 'p2.invalid', 'p3.invalid']
        assert (error.value.kind, error.value.status, error.value.retryable) == ('upstream_timeout', 504, True)
        assert error.value.detail == d.DETAILS['upstream_timeout']
        assert SECRET not in str(error.value) + error.value.detail + json.dumps(store.stats('hour'))
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_timeout_reader_does_not_extract_from_oversized_or_encoded_body():
    oversized = {'padding': 'x' * d.LIMIT, **TIMEOUT_ERROR}
    assert await d.read_error(httpx.Response(500, json=oversized)) is None
    encoded = httpx.Response(500, content=b'not decompressed')
    encoded.headers['content-encoding'] = 'gzip'
    assert await d.read_error(encoded) is None
    assert await d.read_error(httpx.Response(500, json=TIMEOUT_ERROR)) == TIMEOUT_ERROR
    assert d.LIMIT == 8192 and d.READ_TIMEOUT == 2.0
