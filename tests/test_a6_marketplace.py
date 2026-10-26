import httpx
import pytest

from proxy import a6_marketplace as m

PAT = 'SYNTHETIC_TEST_PAT_0000000000000'
TOKENS = {'data': {'total': 2, 'items': [
    {'id': 76809, 'name': 'image', 'status': 1, 'remain_quota': 346400},
    {'id': 60884, 'name': 'glm-5.3', 'status': 1, 'remain_quota': 0},
    {'id': 'bad', 'name': 'x'}]}}


def channel(channel_id, supplier, listings):
    return {'channel_id': channel_id, 'supplier_name': supplier, 'supplier_slug': 'supplier-' + str(channel_id),
            'listings': listings}


def listing(model='gpt-6.1-sol', inp=100000, out=500000, cache=5000, success=9900, samples=100, desc='d'):
    return {'model_name': model, 'input_price_micros': inp, 'output_price_micros': out,
            'cache_read_price_micros': cache, 'cache_write_price_micros': 0,
            'recent_success_rate': success, 'sample_count': samples, 'recent_p50_ms': 9000,
            'description': desc, 'authenticity_guaranteed': True,
            'official_price': {'input_price_micros': 2000000, 'output_price_micros': 10000000}}


@pytest.mark.asyncio
async def test_tokens_sanitized():
    def handler(request):
        assert request.headers['New-Api-User'] == '23984'
        return httpx.Response(200, json=TOKENS)
    result = await m.tokens(PAT, 23984, transport=httpx.MockTransport(handler))
    assert result['total'] == 2
    assert [t['id'] for t in result['items']] == [76809, 60884]
    assert result['items'][0]['name'] == 'image'


@pytest.mark.asyncio
async def test_tokens_auth_and_malformed():
    def rejected(request):
        return httpx.Response(401, json={})
    with pytest.raises(m.MarketplaceError) as error:
        await m.tokens(PAT, 1, transport=httpx.MockTransport(rejected))
    assert error.value.kind == 'a6_auth_rejected'

    def bad(request):
        return httpx.Response(200, content=b'not json')
    with pytest.raises(m.MarketplaceError) as error:
        await m.tokens(PAT, 1, transport=httpx.MockTransport(bad))
    assert error.value.kind == 'a6_invalid_response'


@pytest.mark.asyncio
async def test_sellers_filter_model_and_sort_by_price():
    def handler(request):
        return httpx.Response(200, json={'data': [
            channel(1, 'expensive', [listing(inp=500000)]),
            channel(2, 'cheap', [listing(inp=10000, cache=500), listing(model='other-model')]),
            channel(3, 'mid', [listing(inp=50000, cache=20000)]),
        ]})
    result = await m.sellers(PAT, 1, 'gpt-6.1-sol', None, None, 50, transport=httpx.MockTransport(handler))
    assert [r['supplier'] for r in result['sellers']] == ['cheap', 'mid', 'expensive']
    assert result['matches'] == 3
    assert result['official'] == {'input_micros': 2000000, 'output_micros': 10000000}
    assert result['sellers'][0]['cache_discount_percent'] == 95.0


@pytest.mark.asyncio
async def test_cache_filter_excludes_expensive_cache():
    def handler(request):
        return httpx.Response(200, json={'data': [
            channel(1, 'cheap-normal', [listing(inp=10000, cache=500)]),
            channel(2, 'cache-expensive', [listing(inp=100000, cache=8000)]),
        ]})
    result = await m.sellers(PAT, 1, 'gpt-6.1-sol', None, 0.005, 50, transport=httpx.MockTransport(handler))
    assert [r['supplier'] for r in result['sellers']] == ['cheap-normal']
    assert result['excluded_by_cache'] == 1 and result['matches'] == 2


@pytest.mark.asyncio
async def test_sellers_rejects_bad_input_and_handles_empty():
    with pytest.raises(m.MarketplaceError):
        await m.sellers(PAT, 1, '   ', transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': []})))
    with pytest.raises(m.MarketplaceError):
        await m.sellers(PAT, 1, 'm', 101, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': []})))
    def empty(request):
        return httpx.Response(200, json={'data': []})
    result = await m.sellers(PAT, 1, 'missing-model', transport=httpx.MockTransport(empty))
    assert result['sellers'] == [] and result['matches'] == 0


@pytest.mark.asyncio
async def test_sellers_survives_malformed_rows():
    def handler(request):
        return httpx.Response(200, json={'data': [
            'not-a-channel',
            {'channel_id': 9, 'listings': 'not-a-list'},
            channel(10, 'ok', [{'model_name': 'gpt-6.1-sol', 'input_price_micros': 'x'}, listing(inp=1000, cache=50)]),
        ]})
    result = await m.sellers(PAT, 1, 'gpt-6.1-sol', transport=httpx.MockTransport(handler))
    assert len(result['sellers']) == 2
    assert result['sellers'][0]['input_micros'] == 1000
    assert result['sellers'][-1]['input_micros'] is None
