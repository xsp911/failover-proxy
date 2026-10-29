"""A6 authenticated client contract tests. No live A6 requests."""
import json

import httpx
import pytest

from proxy.a6_client import A6Client, A6Error

PAT = 'test-private-pat-do-not-leak'
SECRET = 'test-private-relay-do-not-leak'
MODEL = 'gpt-6.1-sol'


def response(data):
    return httpx.Response(200, json={'success': True, 'data': data})


def row(listing_id, **extra):
    return {'listing_id': listing_id, 'channel_id': listing_id + 100,
            'model_name': MODEL, 'supplier_name': 'seller',
            'input_price_micros': 20, 'output_price_micros': 100,
            'cache_read_price_micros': 0, 'cache_write_price_micros': 2,
            'per_request_price_micros': 0, 'pricing_currency': 'USD',
            'pricing_unit': 'per_1m_tokens', 'cache_hit_rate_24h': 8935,
            'cache_hit_state': 'rate', 'recent_success_rate': 9900,
            'sample_count': 100, 'test_endpoint': 'responses',
            'model_detection_supported': False, **extra}


def page(items, total=None, token_id=7, snapshot='snapshot'):
    return {'items': items, 'total': len(items) if total is None else total,
            'query': {'token_id': token_id}, 'snapshot_generated_at': snapshot}


@pytest.mark.asyncio
async def test_auth_fixed_origin_token_allowlist_and_secret_helper():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.scheme == 'https'
        assert request.url.host == 'api.a6api.com'
        assert request.headers['authorization'] == 'Bearer ' + PAT
        assert request.headers['new-api-user'] == '23'
        if request.url.path.endswith('/key'):
            assert request.method == 'POST'
            assert json.loads(request.content) == {}
            return response({'key': SECRET})
        return response({'total': 1, 'items': [
            {'id': 7, 'name': 'test', 'status': 1, 'key': SECRET,
             'model_limits_enabled': True, 'model_limits': MODEL,
             'unlimited_quota': False, 'email': 'private@example.org'}]})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        tokens = await client.tokens()
        assert tokens == [{'id': 7, 'name': 'test', 'status': 1,
                           'model_limits_enabled': True, 'model_limits': MODEL,
                           'unlimited_quota': False}]
        assert await client.token_secret(7) == SECRET
    finally:
        await client.close()
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(401, {'message': PAT}),
                                         (500, {'error': SECRET}),
                                         (302, {'location': 'https://evil.test'})])
async def test_errors_never_include_upstream_body_or_credentials(status, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body, headers={'location': 'https://evil.test'})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error) as caught:
            await client.tokens()
        assert PAT not in str(caught.value)
        assert SECRET not in str(caught.value)
        assert PAT not in repr(caught.value)
        assert isinstance(caught.value.status, int)
    finally:
        await client.close()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_network_error_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout(PAT + SECRET, request=request)

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error) as caught:
            await client.tokens()
        assert PAT not in str(caught.value)
        assert SECRET not in str(caught.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_search_full_pagination_exact_model_and_sanitization():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == '/api/marketplace/channels/search'
        query = request.url.params
        assert query['token_id'] == '7'
        assert query['model'] == MODEL
        assert query['sort'] == 'price_asc'
        assert query['page_size'] == '100'
        n = int(query['page'])
        items = [row(i, key=SECRET) for i in range(1, 101)] if n == 1 else [row(101)]
        return response(page(items, total=101))

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        rows = await client.search(7, MODEL)
        assert len(rows) == 101
        assert len(calls) == 3
        assert {r['model_name'] for r in rows} == {MODEL}
        assert rows[0]['cache_read_price_micros'] == 0
        assert rows[0]['cache_hit_rate_24h'] == 8935
        assert SECRET not in json.dumps(rows)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_search_filters_foreign_model():
    client = A6Client(PAT, 23, transport=httpx.MockTransport(
        lambda request: response(page([row(1), row(2, model_name='wrong-model')]))))
    try:
        rows = await client.search(7, MODEL)
        assert [r['listing_id'] for r in rows] == [1]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_token', [8, '7', True])
async def test_search_rejects_mismatching_query_token(bad_token):
    client = A6Client(PAT, 23, transport=httpx.MockTransport(
        lambda request: response(page([row(1)], token_id=bad_token))))
    try:
        with pytest.raises(A6Error):
            await client.search(7, MODEL)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_search_rejects_changed_total():
    def handler(request):
        n = int(request.url.params['page'])
        return response(page([row(i) for i in range(1, 101)] if n == 1 else [row(101)],
                             total=101 if n == 1 else 102))

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error):
            await client.search(7, MODEL)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_invalid_token_id_never_enters_url_path():
    calls = []

    def handler(request):
        calls.append(request)
        return response({'key': SECRET})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        for token_id in ('../user/self', True, 0, -1, 'https://evil.test'):
            with pytest.raises(A6Error):
                await client.token_secret(token_id)
        assert not calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_oversize_response_rejected():
    client = A6Client(PAT, 23, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b' ' * (8 * 1024 * 1024 + 1))))
    try:
        with pytest.raises(A6Error):
            await client.tokens()
    finally:
        await client.close()


CAPS = {'input_price_micros': 20, 'output_price_micros': 100,
        'cache_read_price_micros': 0, 'cache_write_price_micros': 2,
        'per_request_price_micros': 0}
DRAFT = {'channels': [{'channel_id': 101, 'priority': None}], 'caps': CAPS,
         'sort_strategy': 'price_asc', 'user_priorities': {},
         'auto_pool_enabled': False, 'fallback_enabled': False,
         'min_ratio': '', 'max_ratio': '', 'min_cache_hit_rate_bps': 4001}


@pytest.mark.asyncio
async def test_save_explicit_creation_readback_preserves_zero_caps():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == 'POST':
            assert request.url.path == '/api/subscriptions'
            body = json.loads(request.content)
            assert body['token_id'] == 7
            assert body['model_name'] == MODEL
            assert body['caps'] == CAPS
            assert body['auto_pool_enabled'] is False
            assert body['fallback_enabled'] is False
            return response({'id': 91, 'config_version': 1})
        assert request.method == 'GET'
        assert request.url.path == '/api/subscriptions/91'
        return response({'id': 91, 'token_id': 7, 'model_name': MODEL,
                         'config_version': 1, **DRAFT})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        assert calls == []
        saved = await client.save_subscription(7, MODEL, DRAFT)
        assert saved['id'] == 91
        assert saved['caps']['cache_read_price_micros'] == 0
        assert [r.method for r in calls] == ['POST', 'GET']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_save_update_uses_expected_version_fixed_config_path():
    calls = []
    existing = {'id': 91, 'token_id': 7, 'model_name': MODEL,
                'config_version': 4, **DRAFT}

    def handler(request):
        calls.append(request)
        if request.method == 'PUT':
            assert request.url.path == '/api/subscriptions/91/config'
            assert json.loads(request.content)['expected_config_version'] == 4
            return response({'id': 91, 'config_version': 5})
        return response({**existing, 'config_version': 5 if any(r.method == 'PUT' for r in calls) else 4})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        saved = await client.save_subscription(7, MODEL, DRAFT, existing=existing)
        assert saved['config_version'] == 5
        assert [r.method for r in calls] == ['GET', 'PUT', 'GET']
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_cap', [True, -1, 1.2, '20', None])
async def test_save_rejects_invalid_integer_caps_before_network(bad_cap):
    calls = []

    def handler(request):
        calls.append(request)
        return response({})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        draft = {**DRAFT, 'caps': {**CAPS, 'input_price_micros': bad_cap}}
        with pytest.raises(A6Error):
            await client.save_subscription(7, MODEL, draft)
        assert calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'readback_mismatch'])
async def test_write_uncertainty_never_blind_retries(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == 'POST':
            if failure == 'timeout':
                raise httpx.ReadTimeout(PAT + SECRET, request=request)
            return response({'id': 91, 'config_version': 1})
        return response({'id': 91, 'token_id': 7, 'model_name': MODEL,
                         'config_version': 1, **DRAFT, 'fallback_enabled': True})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error) as caught:
            await client.save_subscription(7, MODEL, DRAFT)
        assert caught.value.kind == 'a6_route_write_uncertain'
        assert PAT not in str(caught.value)
        assert SECRET not in str(caught.value)
        assert len([r for r in calls if r.method == 'POST']) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_logs_exact_attribution_filters_non_usage_rows_and_secrets():
    def handler(request):
        assert request.url.path == '/api/log/self'
        assert request.url.params['request_id'] == 'req-1'
        common = {'request_id': 'req-1', 'token_id': 7, 'type': 2,
                  'channel': 999, 'quota': 12, 'prompt_tokens': 10,
                  'completion_tokens': 3, 'content': SECRET,
                  'other': json.dumps({'actual_channel_id': 101,
                      'smart_route_fallback_used': False,
                      'marketplace_price_input_micros': 20,
                      'marketplace_price_output_micros': 100,
                      'cache_tokens': 8, 'secret': SECRET})}
        return response({'total': 4, 'items': [common,
                         {**common, 'request_id': 'req-10'},
                         {**common, 'token_id': 8},
                         {**common, 'type': 1}]})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        rows = await client.logs('req-1', 7)
        assert len(rows) == 1
        assert rows[0]['actual_channel_id'] == 101
        assert rows[0]['fallback_used'] is False
        assert rows[0]['input_price_micros'] == 20
        assert SECRET not in json.dumps(rows)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_search_page_timestamp_change_with_stable_listing_ids_is_accepted():
    def handler(request):
        n = int(request.url.params['page'])
        items = [row(i) for i in range(1, 101)] if n == 1 else [row(101)]
        return response(page(items, total=101, snapshot='snapshot-' + str(n)))

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        rows = await client.search(7, MODEL)
        assert len(rows) == 101
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_readback_version_must_advance_after_update():
    existing = {'id': 91, 'token_id': 7, 'model_name': MODEL,
                'config_version': 4, **DRAFT}

    def handler(request):
        return response(existing)

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(A6Error) as caught:
            await client.save_subscription(7, MODEL, DRAFT, existing=existing)
        assert caught.value.kind == 'a6_route_write_uncertain'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_request_helper_rejects_foreign_origin_before_auth_can_leave():
    calls = []

    def handler(request):
        calls.append(request)
        return response({})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        for path in ('https://evil.test/api/token/', '//evil.test/api/token/',
                     '/api/../admin', '/api/token/?secret=bad'):
            with pytest.raises(A6Error):
                await client.request('GET', path)
        assert calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_subscriptions_filter_identity_and_safe_details():
    def handler(request):
        if request.url.path == '/api/subscriptions':
            assert request.url.params['token_id'] == '7'
            assert request.url.params['model_names'] == MODEL
            base = {'id': 91, 'token_id': 7, 'model_name': MODEL,
                    'config_version': 1, **DRAFT, 'secret': SECRET}
            return response({'total': 3, 'items': [base,
                             {**base, 'id': 92, 'token_id': 8},
                             {**base, 'id': 93, 'model_name': 'wrong-model'}]})
        assert request.url.path == '/api/subscriptions/91'
        return response({'id': 91, 'token_id': 7, 'model_name': MODEL,
                         'config_version': 1, **DRAFT, 'key': SECRET})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        rows = await client.list_subscriptions(7, MODEL)
        assert [r['id'] for r in rows] == [91]
        detail = await client.get_subscription(91)
        assert detail['caps'] == CAPS
        assert detail['channels'][0]['channel_id'] == 101
        assert SECRET not in json.dumps(rows)
        assert SECRET not in json.dumps(detail)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pins_allowlist_omits_secret():
    client = A6Client(PAT, 23, transport=httpx.MockTransport(
        lambda request: response([{'token_id': 7, 'channel_id': 101,
            'model_name': MODEL, 'fallback_to_smart_routing': True,
            'route_effective': True, 'status': 1, 'key': SECRET} ])))
    try:
        rows = await client.pins()
        assert len(rows) == 1
        assert rows[0]['token_id'] == 7
        assert rows[0]['fallback_to_smart_routing'] is True
        assert SECRET not in json.dumps(rows)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_payload_does_not_forward_arbitrary_nested_fields():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == 'POST':
            body = json.loads(request.content)
            assert 'secret' not in body
            assert 'secret' not in body['caps']
            assert 'secret' not in body['channels'][0]
            assert 'path' not in body
            return response({'id': 91, 'config_version': 1})
        return response({'id': 91, 'token_id': 7, 'model_name': MODEL,
                         'config_version': 1, **DRAFT})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        draft = {**DRAFT, 'secret': SECRET, 'path': 'https://evil.test',
                 'channels': [{'channel_id': 101, 'priority': None, 'secret': SECRET}],
                 'caps': {**CAPS, 'secret': SECRET}}
        try:
            await client.save_subscription(7, MODEL, draft)
        except A6Error:
            # Rejecting unknown fields before writing is equally safe.
            assert not any(r.method in ('POST', 'PUT') for r in calls)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_logs_read_all_pages_before_exact_filtering():
    calls = []

    def handler(request):
        calls.append(request)
        n = int(request.url.params['p'])
        assert request.url.params['page_size'] == '100'
        unrelated = {'request_id': 'other-request', 'token_id': 7,
                     'type': 2, 'channel': 999, 'other': {}}
        match = {'request_id': 'req-1', 'token_id': 7, 'type': 5,
                 'channel': 101, 'other': {'actual_channel_id': 101}}
        return response({'total': 101, 'items': [unrelated] * 100 if n == 1 else [match]})

    client = A6Client(PAT, 23, transport=httpx.MockTransport(handler))
    try:
        rows = await client.logs('req-1', 7)
        assert len(calls) == 2
        assert len(rows) == 1
        assert rows[0]['actual_channel_id'] == 101
        assert rows[0]['type'] == 5
    finally:
        await client.close()


def test_safe_error_default_status():
    error = A6Error('a6_invalid_response')
    assert error.kind == 'a6_invalid_response'
    assert error.status == 502
    assert str(error) == 'a6_invalid_response'
