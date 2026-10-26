"""Fail-closed parking of owned test routes with verified readback recovery."""
import asyncio
import json
from .a6_client import A6Error


def parked_matches(route,draft,token_id,model):
    return (route.get('token_id')==token_id and route.get('model_name')==model
        and route.get('caps')==draft['caps'] and route.get('auto_pool_enabled') is False
        and route.get('fallback_enabled') is False
        and route.get('min_cache_hit_rate_bps') is None
        and sorted(c['channel_id'] for c in route.get('channels',[]) if not c.get('removed_at'))==sorted(c['channel_id'] for c in draft['channels']))


async def park_owned(manager,client,pool,token_id,route):
    """No deletes: deleting last channel can restore default routing on A6API."""
    draft=manager.route_draft(pool,[next(c['channel_id'] for c in route['channels'] if not c.get('removed_at'))])
    draft['caps']={k:1 if k!='per_request_price_micros' else 0 for k in draft['caps']}
    # Strict positive caps provide the stop condition (verified pilot). A6 may
    # reject an impossible cache100% route, so don't require that extra filter.
    draft['min_cache_hit_rate_bps']=None
    latest=await client.get_subscription(route['id'])
    if parked_matches(latest,draft,token_id,pool['model']):
        saved=latest
    else:
        if latest['config_version']!=route['config_version']:
            raise A6Error('test_route_changed_before_cleanup',409)
        try:
            saved=await client.save_subscription(token_id,pool['model'],draft,latest)
        except A6Error:
            # A server/network error may follow an accepted write. Only a
            # matching readback can clear uncertainty; never blind-retry PUT.
            saved=None
            for delay in (0,.5,1):
                if delay:await asyncio.sleep(delay)
                try:observed=await client.get_subscription(route['id'])
                except A6Error:continue
                if parked_matches(observed,draft,token_id,pool['model']):saved=observed;break
            if saved is None:raise
    await asyncio.to_thread(manager.operations.record,token_id,pool['model'],saved['id'],saved['config_version'],draft,'parked')
    return saved


async def reconcile_owned(manager,client,pool,token_id,owner,route):
    if owner.get('subscription_id')!=route['id']:return False
    if owner.get('version')!=route['config_version']:
        try:draft=json.loads(owner['draft']) if isinstance(owner.get('draft'),str) else owner.get('draft',{})
        except (ValueError,TypeError):return False
        caps=draft.get('caps',{})
        if owner.get('state')!='parked' or any(caps.get(k)!=1 for k in ('input_price_micros','output_price_micros','cache_read_price_micros','cache_write_price_micros')) or not parked_matches(route,draft,token_id,pool['model']):return False
        # A6 may advance config_version without changing the safely parked
        # configuration. Reconcile an exact known parked readback, never an
        # externally changed active whitelist/cap configuration.
        await asyncio.to_thread(manager.operations.record,token_id,pool['model'],route['id'],route['config_version'],draft,'parked')
        return True
    # Explicitly known previous route still exists at its exact owned version.
    # Parking only tightens its caps and disables fallback, no generation.
    await park_owned(manager,client,pool,token_id,route)
    return True
