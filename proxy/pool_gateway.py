"""Local fail-closed gate for token/model routes managed by this gateway."""
import asyncio
import hashlib
import hmac

from .pool_manager import PoolError


async def guarded_config(manager,config,model_name):
    policies=await asyncio.to_thread(manager.db.list)
    credentials={}
    for p in config['providers']:
        if p.get('base_url','').rstrip('/')!='https://api.a6api.com/v1' or not p.get('api_key'):continue
        credentials[p['id']]=p['api_key'].removeprefix('sk-')
    if not credentials:return config
    if hasattr(manager,'deleted'):
        for alias in config['models']:
            if alias['name']!=model_name:continue
            for route in alias['routes']:
                secret=credentials.get(route['provider_id'])
                if secret and await asyncio.to_thread(manager.deleted.blocked,route['upstream_model'],secret):raise PoolError('managed_pool_deleted',503)
    if not policies:return config
    # A6 token list doesn't expose raw key. Resolve server-side only when first
    # used; only store SHA256 fingerprints, never plaintext outside encrypted PAT.
    client=manager.client()
    try:
        for pool in policies:
            manager.mark_reference_stale(pool)
            aliases={m['name'] for m in config['models'] for r in m['routes'] if r['upstream_model']==pool['model'] and r['provider_id'] in credentials}
            if model_name not in aliases:continue
            secret=await client.token_secret(pool['token_id'])
            matching={id for id,key in credentials.items() if hmac.compare_digest(key,secret.removeprefix('sk-'))}
            if not matching:continue
            if pool.get('auto_publish') and hasattr(manager,'auto'):
                owner=await asyncio.to_thread(manager.auto.remote,pool['id'])
                if owner and owner['state']=='uncertain':raise PoolError('managed_pool_remote_sync_uncertain',503)
            if pool.get('status') in ('syncing','blocked') or pool.get('last_error')=='auto_route_sync_pending' or not pool['enabled'] or not pool.get('last_sync_at') or not pool.get('members'):
                raise PoolError('managed_pool_not_ready',503)
            candidates={c['channel_id']:c for c in pool.get('candidates',[]) if c.get('eligible') and c.get('verification_status')=='verified' and not manager.expired(c,pool)}
            if any(m['channel_id'] not in candidates for m in pool['members']):
                raise PoolError('managed_pool_has_no_safe_route',503)
            owner=await asyncio.to_thread(manager.auto.remote,pool['id'])
            if not owner or owner.get('state')!='owned':raise PoolError('managed_pool_remote_sync_uncertain',503)
            from .pool_automation import exact_owned_config
            actual=await client.get_subscription(owner['subscription_id'])
            pins=await client.pins()
            if any(p['token_id']==pool['token_id'] and p['model_name']==pool['model'] for p in pins) or not exact_owned_config(actual,owner,pool):raise PoolError('managed_pool_remote_changed',503)
        return config
    finally:await client.close()
