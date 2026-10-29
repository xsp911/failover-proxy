"""A6API Marketplace queries: tokens and priced seller listings.

A6 exposes only cache *prices* (cache_read_price_micros), not a cache-hit rate.
The cache filter therefore uses the cache discount: how much cheaper a cached
input token is than a normal one (100 - cache_read/input*100).
"""
import httpx

from .proxy_transport import async_client

from .a6_origin import CONSOLE_BASE as BASE
MAX_LISTINGS = 10000


class MarketplaceError(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def _headers(pat, user_id):
    return {'Authorization': 'Bearer ' + pat, 'New-Api-User': str(user_id)}


async def tokens(pat, user_id, transport=None, proxy=None):
    async with async_client(proxy, transport, base_url=BASE, timeout=httpx.Timeout(20, connect=10)) as client:
        response = await client.get('/api/token/?p=1&size=100', headers=_headers(pat, user_id))
    if response.status_code == 401:
        raise MarketplaceError('a6_auth_rejected')
    if response.status_code != 200:
        raise MarketplaceError('a6_unexpected_status')
    try:
        data = response.json().get('data') or {}
    except ValueError:
        raise MarketplaceError('a6_invalid_response') from None
    items = data.get('items')
    if not isinstance(items, list):
        raise MarketplaceError('a6_invalid_response')
    output = []
    for item in items[:200]:
        if not isinstance(item, dict):
            continue
        name = item.get('name') if isinstance(item.get('name'), str) else ''
        output.append({'id': item.get('id') if isinstance(item.get('id'), int) else None,
                       'name': name[:80], 'status': item.get('status') if isinstance(item.get('status'), int) else None,
                       'remain_quota': item.get('remain_quota') if isinstance(item.get('remain_quota'), int) else None})
    return {'total': data.get('total') if isinstance(data.get('total'), int) else len(output),
            'items': [t for t in output if t['id'] is not None]}


def _price(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _cache_share(cache_read, input_price):
    if not cache_read or not input_price:
        return None
    return round(100 * cache_read / input_price, 2)


async def sellers(pat, user_id, model, min_success_percent=None, max_cache_usd=None, limit=50, transport=None, proxy=None):
    model = model.strip() if isinstance(model, str) else ''
    if not 1 <= len(model) <= 200 or any(ord(c) < 32 for c in model) or any(c.isspace() for c in model):
        raise MarketplaceError('invalid_model')
    if min_success_percent is not None and (isinstance(min_success_percent, bool) or not isinstance(min_success_percent, (int, float)) or not 0 <= min_success_percent <= 100):
        raise MarketplaceError('invalid_success_percent')
    if max_cache_usd is not None and (isinstance(max_cache_usd, bool) or not isinstance(max_cache_usd, (int, float)) or not 0 <= max_cache_usd <= 1000):
        raise MarketplaceError('invalid_cache_price')
    async with async_client(proxy, transport, base_url=BASE, timeout=httpx.Timeout(60, connect=10)) as client:
        response = await client.get('/api/marketplace/channels', headers=_headers(pat, user_id))
    if response.status_code == 401:
        raise MarketplaceError('a6_auth_rejected')
    if response.status_code != 200:
        raise MarketplaceError('a6_unexpected_status')
    try:
        channels = response.json().get('data')
    except ValueError:
        raise MarketplaceError('a6_invalid_response') from None
    if not isinstance(channels, list):
        raise MarketplaceError('a6_invalid_response')

    official = None
    rows, total_matches, excluded_cache, excluded_quality = [], 0, 0, 0
    for channel in channels[:MAX_LISTINGS]:
        if not isinstance(channel, dict):
            continue
        listings = channel.get('listings') or []
        for listing in listings:
            if not isinstance(listing, dict) or listing.get('model_name') != model:
                continue
            total_matches += 1
            price_in, price_out = _price(listing.get('input_price_micros')), _price(listing.get('output_price_micros'))
            cache_read, cache_write = _price(listing.get('cache_read_price_micros')), _price(listing.get('cache_write_price_micros'))
            share = _cache_share(cache_read, price_in)
            success = listing.get('recent_success_rate')
            success_percent = round(success / 100, 2) if isinstance(success, int) and success >= 0 else None
            if min_success_percent is not None and (success_percent is None or success_percent < min_success_percent):
                excluded_quality += 1
                continue
            if max_cache_usd is not None and (cache_read is None or cache_read / 1e6 > max_cache_usd):
                excluded_cache += 1
                continue
            snapshot = listing.get('official_price')
            if official is None and isinstance(snapshot, dict):
                official = {'input_micros': _price(snapshot.get('input_price_micros')),
                            'output_micros': _price(snapshot.get('output_price_micros'))}
            rows.append({
                'channel_id': channel.get('channel_id'),
                'supplier': str(channel.get('supplier_name') or channel.get('nickname') or '')[:80],
                'supplier_slug': str(channel.get('supplier_slug') or '')[:80],
                'description': str(listing.get('description') or '')[:160],
                'input_micros': price_in, 'output_micros': price_out,
                'cache_read_micros': cache_read, 'cache_write_micros': cache_write,
                'cache_share_percent': share,
                'cache_discount_percent': round(100 - share, 2) if share is not None else None,
                'success_percent': success_percent,
                'samples': listing.get('sample_count') if isinstance(listing.get('sample_count'), int) else None,
                'p50_ms': listing.get('recent_p50_ms') if isinstance(listing.get('recent_p50_ms'), int) else None,
                'authenticity_guaranteed': listing.get('authenticity_guaranteed') is True,
            })
    rows.sort(key=lambda r: (r['input_micros'] is None, r['input_micros'] or 0, r['output_micros'] or 0))
    return {'model': model, 'min_success_percent': min_success_percent, 'max_cache_usd': max_cache_usd,
            'official': official, 'matches': total_matches,
            'excluded_by_cache': excluded_cache, 'excluded_by_quality': excluded_quality,
            'returned': min(len(rows), limit), 'sellers': rows[:limit]}
