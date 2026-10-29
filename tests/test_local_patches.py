"""LOCAL POLICY tests — behaviour this fork adds on top of the upstream relay.

Each test names the defect it prevents. They live in their own file on purpose: the
shared test modules belong to upstream and the next snapshot overwrites them, so a fix
whose only test sat inside a shared file would silently lose its guard.

Ported from the local fork on 2026-10-28; the upstream already carries the two fixes they
replace (provider_policy, context_preflight), so this file holds only what is new here.
"""
import json

import httpx
import pytest

from proxy.engine import Engine, GatewayError, Completion, parse_sse

MODEL = 'deepseek-v4.1-flash-1017'


# ---- helpers (independent of the shared test modules) ------------------------------

def frame(text=None, reason=None):
    delta = {}
    if text is not None:
        delta['content'] = text
    return {'id': 't', 'object': 'chat.completion.chunk', 'created': 1, 'model': MODEL,
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}


def wire(events, done=True):
    body = ''.join('data: ' + json.dumps(x) + '\n\n' for x in events)
    return (body + ('data: [DONE]\n\n' if done else '')).encode()


class Parts(httpx.AsyncByteStream):
    """Minimal httpx stream that yields the payload in chunks."""

    def __init__(self, parts):
        self._parts = list(parts)

    async def __aiter__(self):
        for part in self._parts:
            yield part


# ---- 1. a novel finish_reason is not a broken answer -------------------------------
# An upstream answered HTTP 200 with a complete body and a reason outside the old
# five-value whitelist; the relay graded the answer as broken and failed over.

@pytest.mark.asyncio
async def test_novel_finish_reason_with_content_is_accepted():
    data = wire([frame('Привет'), frame(reason='end_turn')])
    result = await parse_sse(httpx.Response(200, stream=Parts([data])), Completion(MODEL, 100000, 100))
    assert result.texts[0] == 'Привет'
    assert result.finished[0] == 'end_turn'


@pytest.mark.asyncio
async def test_unknown_finish_reason_without_content_is_still_fatal():
    data = wire([frame(reason='brand_new_label')])
    with pytest.raises(GatewayError, match='invalid_finish_reason'):
        await parse_sse(httpx.Response(200, stream=Parts([data])), Completion(MODEL, 100000, 100))


def test_novel_finish_reason_json_with_content_is_accepted():
    payload = {'model': 'upstream', 'choices': [{'index': 0,
               'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'end_turn'}]}
    Engine.validate_json(payload, MODEL)


def test_unknown_finish_reason_json_without_content_is_still_fatal():
    payload = {'model': 'upstream', 'choices': [{'index': 0,
               'message': {'role': 'assistant', 'content': ''}, 'finish_reason': 'brand_new_label'}]}
    with pytest.raises(GatewayError, match='incomplete_upstream_response'):
        Engine.validate_json(payload, MODEL)


# ---- 2. a structured-output refusal is named, not guessed --------------------------
# A provider that does not support response_format said so in its own words and the
# client only saw a generic upstream_http_400 ("check the model's supported parameters").

@pytest.mark.parametrize('status,message', [
    (400, 'response_format is not supported for this model'),
    (400, 'unsupported value for json_schema'),
])
def test_response_format_refusal_is_named(status, message):
    from proxy.diagnostics import classify
    payload = {'error': {'message': message}}
    kind, public, retryable, detail = classify(payload, status, response_format='json_schema')
    assert kind == 'response_format_unsupported'
    assert (public, retryable) == (400, False)
    assert 'response_format' in detail


def test_response_format_refusal_is_not_named_when_the_client_did_not_ask():
    """The same wording must NOT be relabelled for a client that sent no response_format."""
    from proxy.diagnostics import classify
    kind, *_ = classify({'error': {'message': 'response_format is not supported'}}, 400)
    assert kind != 'response_format_unsupported'


# ---- 3. an attempt carries the diagnostic text, not just the kind ------------------

@pytest.mark.asyncio
async def test_a_failed_attempt_records_error_detail(tmp_path):
    from proxy.store import Store

    store = Store(tmp_path)
    c = store.get_config()
    c['settings']['trust_private_upstreams'] = True
    c['providers'] = [{'id': '1', 'name': 'P1', 'priority': 1, 'enabled': True,
                       'base_url': 'http://p1.invalid/v1', 'api_key': 'secret'}]
    c['models'] = [{'id': 'model', 'name': MODEL, 'enabled': True,
                    'routes': [{'provider_id': '1', 'upstream_model': MODEL, 'enabled': True}]}]
    store.save_config(c, c['revision'])

    def handler(request):
        return httpx.Response(503)

    engine = Engine(store, httpx.MockTransport(handler))
    try:
        with pytest.raises(GatewayError):
            await engine.generate({'model': MODEL, 'messages': [], 'stream': True},
                                  store.get_config(True), 'detail-test', 'key')
        rows = store.stats('hour')['recent_errors']
        assert rows and rows[0]['kind'].startswith('upstream_http_')
        assert rows[0].get('detail'), 'an attempt without error_detail cannot be diagnosed'
    finally:
        await engine.close()

@pytest.mark.parametrize("status", [401, 402, 403, 429, 500, 503])
def test_an_account_refusal_fails_over_like_any_other(status):
    """402 is the same class as 401/403: the refusal belongs to one account.

    Observed live: a spent balance answered 402 on every request and the relay returned
    137 errors in a row with zero failovers, while two providers with their own keys sat
    idle. A payment error is not a client error, so it must reach the next provider.
    """
    from proxy.diagnostics import classify
    kind, public, retryable, detail = classify({"error": {"message": "refused"}}, status)
    assert kind == f"upstream_http_{status}"
    assert retryable is True, f"{status} must fail over to a provider with another account"
