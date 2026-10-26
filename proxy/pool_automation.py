"""Opt-in paid pool automation. No pins, hidden fallback or unmanaged writes."""
import asyncio
import time

from .a6_client import A6Error
from .pool_manager import PoolError,iso


def channels(route):
    return sorted(c['channel_id'] for c in route.get('channels',[]) if not c.get('removed_at'))


def same_remote(remote,owner):
    return remote['id']==owner['subscription_id'] and remote['config_version']==owner['config_version']


def exact_owned_config(remote,owner,pool):
    draft=owner.get('draft',{})
    return (remote.get('id')==owner.get('subscription_id') and remote.get('token_id')==pool['token_id'] and remote.get('model_name')==pool['model']
        and channels(remote)==sorted(c['channel_id'] for c in draft.get('channels',[]))
        and all(remote.get(k)==draft.get(k) for k in ('caps','sort_strategy','user_priorities','min_ratio','max_ratio','min_cache_hit_rate_bps'))
        and remote.get('auto_pool_enabled') is False and remote.get('fallback_enabled') is False
        and draft.get('auto_pool_enabled') is False and draft.get('fallback_enabled') is False)


async def remote_before(manager,pool,client):
    pins=await client.pins()
    if any(p['token_id']==pool['token_id'] and p['model_name']==pool['model'] for p in pins):raise PoolError('existing_pin_conflicts_with_pool',409)
    listed=await client.list_subscriptions(pool['token_id'],pool['model'])
    owner=await asyncio.to_thread(manager.auto.remote,pool['id'])
    if len(listed)>1:raise PoolError('ambiguous_existing_routes',409)
    remote=await client.get_subscription(listed[0]['id']) if listed else None
    if remote:
        if owner and owner.get('state') in ('owned','parked','uncertain') and not same_remote(remote,owner) and exact_owned_config(remote,owner,pool):
            state=owner['state'] if owner['state']!='uncertain' else 'parked' if not pool.get('members') else 'owned'
            owner=await asyncio.to_thread(manager.auto.save_remote,pool['id'],remote['id'],remote['config_version'],owner['draft'],state)
        if owner and owner['state']=='uncertain':
            draft=owner.get('draft',{})
            if exact_owned_config(remote,owner,pool):
                state='parked' if not pool.get('members') else 'owned'
                owner=await asyncio.to_thread(manager.auto.save_remote,pool['id'],remote['id'],remote['config_version'],draft,state)
        if not owner or owner['state'] not in ('owned','parked') or not same_remote(remote,owner):raise PoolError('auto_remote_route_conflict',409)
    elif owner:raise PoolError('auto_remote_route_missing',409)
    return remote


async def apply_members(manager,pool,client,members):
    current=await manager.get(pool['id'])
    if current['revision']!=pool['revision'] or not current['enabled'] or not current.get('auto_publish'):raise PoolError('auto_policy_changed',409)
    before=await remote_before(manager,pool,client)
    if members:
        draft=manager.route_draft(pool,[m['channel_id'] for m in members]);state='owned'
    elif before:
        # Never remove last channel: A6 may restore its default router. Keep the
        # owned route parked behind pilot-proven strict positive caps instead.
        remaining=channels(before)
        if not remaining:
            await asyncio.to_thread(manager.db.update_runtime,pool['id'],pool['revision'],members=[],status='blocked',last_error='no_verified_eligible_members')
            return None
        draft=manager.route_draft(pool,[remaining[0]])
        draft['caps']={k:1 if k!='per_request_price_micros' else 0 for k in draft['caps']}
        draft['min_cache_hit_rate_bps']=None;state='parked'
    else:
        await asyncio.to_thread(manager.db.update_runtime,pool['id'],pool['revision'],members=[],status='blocked',last_error='no_verified_eligible_members')
        return None
    if before and exact_owned_config(before,{'subscription_id':before['id'],'draft':draft},pool):
        # Ownership was read and validated above. Do not PUT the same route:
        # publish fresh local evidence without manufacturing a remote version.
        await asyncio.to_thread(manager.db.update_runtime,pool['id'],pool['revision'],members=members,status='published' if members else 'blocked',last_sync_at=iso(time.time()),last_error=None if members else 'no_verified_eligible_members')
        job=manager.current_jobs.get(pool['id'],{})
        await asyncio.to_thread(manager.membership_history.record,pool['id'],before=[m['channel_id'] for m in current.get('members',[])],after=[m['channel_id'] for m in members],reason='verified_pool_sync',source=job.get('kind') if job.get('kind') in ('auto_refresh','auto_audit') else 'system',job_id=job.get('id'),subscription_id=before['id'],config_version=before['config_version'])
        return before
    # Durable local close before remote I/O: errors must never silently reenable
    # the gateway using a stale whitelist. Direct A6 clients remain outside it.
    await asyncio.to_thread(manager.db.update_runtime,pool['id'],pool['revision'],members=members,status='syncing',last_error='auto_route_sync_pending')
    await asyncio.to_thread(manager.auto.save_remote,pool['id'],before['id'] if before else None,before['config_version'] if before else None,draft,'uncertain')
    saved=await client.save_subscription(pool['token_id'],pool['model'],draft,before)
    await asyncio.to_thread(manager.auto.save_remote,pool['id'],saved['id'],saved['config_version'],draft,state)
    if members and hasattr(manager,'deleted'):
        secret=await client.token_secret(pool['token_id'])
        await asyncio.to_thread(manager.deleted.clear,pool['model'],secret)
    await asyncio.to_thread(manager.db.update_runtime,pool['id'],pool['revision'],members=members,status='published' if members else 'blocked',last_sync_at=iso(time.time()),last_error=None if members else 'no_verified_eligible_members')
    job=manager.current_jobs.get(pool['id'],{})
    reason='confirmed_member_removal' if job.get('phase')!='saving' and len(members)<len(current.get('members',[])) else 'verified_pool_sync'
    await asyncio.to_thread(manager.membership_history.record,pool['id'],before=[m['channel_id'] for m in current.get('members',[])],after=[m['channel_id'] for m in members],reason=reason,source=job.get('kind') if job.get('kind') in ('auto_refresh','auto_audit') else 'system',job_id=job.get('id'),subscription_id=saved['id'],config_version=saved['config_version'])
    return saved


async def schedule_refill(manager,pool,fresh,safe,job):
    from .pool_selection import cooling,next_check_epoch
    histories={r['channel_id']:r for r in await asyncio.to_thread(manager.auto.list_records,pool['id'])}
    member_ids={m['channel_id'] for m in safe}
    replacements=[c for c in fresh if c['channel_id'] not in member_ids and c['eligible'] and not await asyncio.to_thread(manager.auto.is_banned,pool['id'],c['channel_id'])]
    if not replacements:return
    available=[c for c in replacements if not cooling(histories.get(c['channel_id'],{}))]
    earliest=None if available else min(next_check_epoch(histories[c['channel_id']]) for c in replacements)
    waiting_reason='fresh_candidates' if available else 'seller_cooldown'
    reasons=set(job.get('warnings',[]))
    if job.get('result',{}).get('stop_reason')=='budget_exhausted':
        # A daily ceiling cannot be fixed by minute-by-minute retries.
        from datetime import datetime,timezone,timedelta
        now=datetime.now(timezone.utc)
        waiting_reason='daily_budget'
        earliest=max(earliest or 0,(now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp())
    elif reasons and available:
        earliest=max(earliest or 0,time.time()+300);waiting_reason='upstream_retry'
    await asyncio.to_thread(manager.auto.request_refill,pool['id'],time.time(),earliest,waiting_reason)
    job.setdefault('result',{})['refill_scheduled']=True


async def automate(manager,pool,client,job):
    if not pool['enabled'] or not pool.get('auto_publish'):raise PoolError('auto_mode_disabled',409)
    mode=job['kind']
    initial=list(pool.get('members',[]))
    members_lock=asyncio.Lock()
    audited=set()
    async def after_candidate(candidate,result):
        audited.add(candidate['channel_id'])
        if mode!='auto_audit':return
        record=await asyncio.to_thread(manager.auto.is_banned,pool['id'],candidate['channel_id'])
        if result.get('verification_status')!='suspect' and not record:return
        async with members_lock:
            current=await manager.get(pool['id'])
            kept=[m for m in current.get('members',[]) if m['channel_id']!=candidate['channel_id']]
            if len(kept)==len(current.get('members',[])):return
            # Result suspect requires two complete low-scoring rounds with exact
            # attribution. No request/network error is identity-substitution proof.
            await apply_members(manager,pool,client,kept)
            job.setdefault('removed_channels',[]).append(candidate['channel_id'])
    manager.result_callbacks[job['id']]=after_candidate
    try:
        if mode=='auto_audit':
            job['target_channel_ids']=[m['channel_id'] for m in initial]
            if not initial:
                job['result']={'candidates':0,'verified':0,'requires_publication':False}
                fresh=await manager.discover(pool,client,full=True)
                await schedule_refill(manager,pool,fresh,[],job)
                return []
        # Check remote ownership before spending money or touching a work route.
        await remote_before(manager,pool,client)
        results=await manager.verify(pool,client,job)
        snapshot=manager.verification_snapshots.pop(job['id'],None)
        fresh=snapshot[1] if snapshot and time.monotonic()-snapshot[0]<10 else await manager.discover(await manager.get(pool['id']),client,full=True)
        safe=[c for c in fresh if c['eligible'] and c.get('verification_status')=='verified' and not manager.expired(c,pool) and not await asyncio.to_thread(manager.auto.is_banned,pool['id'],c['channel_id'])]
        if mode=='auto_audit':
            original_ids={m['channel_id'] for m in initial}
            audited_members=[c for c in safe if c['channel_id'] in original_ids and c['channel_id'] in audited]
            # Admit only fresh independently verified reserve sellers that were
            # not this audit's failed/unaudited originals. Final discovery above
            # rechecks exact policy/TTL/price/cache/ban before one publication.
            reserve=[c for c in safe if c['channel_id'] not in original_ids]
            safe=audited_members+reserve
            job.setdefault('result',{})['cached_replacements']=[c['channel_id'] for c in reserve[:max(0,pool['max_members']-len(audited_members))]]
        safe=safe[:pool['max_members']]
        await apply_members(manager,pool,client,safe)
        job.setdefault('result',{})['requires_publication']=False
        job['result']['published']=bool(safe)
        job['result']['published_members']=len(safe)
        job['result']['removed_members']=sorted({m['channel_id'] for m in initial}-{m['channel_id'] for m in safe})
        if len(safe)<pool['max_members']:
            await schedule_refill(manager,pool,fresh,safe,job)
        return results
    finally:manager.result_callbacks.pop(job['id'],None)


async def tick(manager,now=None):
    now=time.time() if now is None else now
    for pool in await asyncio.to_thread(manager.db.list):
        automatic=bool(pool['enabled'] and pool.get('auto_publish'))
        await asyncio.to_thread(manager.auto.configure,pool['id'],automatic,now)
        if not automatic or pool['id'] in getattr(manager,'restarting',set()):continue
        if any(j['pool_id']==pool['id'] and j['status'] in ('queued','running') for j in manager.jobs.values()):continue
        for kind in ('audit','refresh'):
            if await asyncio.to_thread(manager.auto.claim_due,pool['id'],kind,now):
                try:await manager.enqueue(pool['id'],'auto_'+kind,True,scheduled=True)
                except Exception:
                    await asyncio.to_thread(manager.auto.finish,pool['id'],kind,'auto_enqueue_failed')
                break
