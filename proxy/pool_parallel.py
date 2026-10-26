"""Four isolated test-key lanes, one shared budget and incremental results."""
import asyncio
from datetime import datetime, timezone

from .a6_client import A6Error
from .a6_probe import TargetProbe,ProbeError
from .pool_manager import PoolError,micros,iso
from .pool_cleanup import park_owned,reconcile_owned


def active_test_ids(pool):
    return pool.get('test_token_ids',[pool['test_token_id']])


async def verify_parallel(manager,pool,client,job):
    if not manager.route_caps_verified:raise PoolError('controlled_price_caps_pilot_required',409)
    job['phase']='discovery'
    import time
    catalogue=await manager.discover(pool,client,full=True)
    candidates=list(catalogue)
    targets=job.get('target_channel_ids')
    progressive=job.get('kind')!='auto_audit' and targets is None
    history={r['channel_id']:r for r in await asyncio.to_thread(manager.auto.list_records,pool['id'])}
    cached=[c for c in catalogue if c.get('verification_status')=='verified' and not manager.expired(c,pool)]
    cooling=[];eligible=[]
    for c in catalogue:
        if not c['eligible'] or (targets is not None and c['channel_id'] not in targets):continue
        h=history.get(c['channel_id'],{})
        from .pool_selection import cooling as is_cooling
        if progressive and is_cooling(h):
            cooling.append(c);continue
        if progressive and c in cached:continue
        eligible.append(c)
    eligible.sort(key=lambda c:(bool(history.get(c['channel_id'],{}).get('last_checked_at')),c['input_micros'] is None,c['input_micros'] or 0,c['channel_id']))
    tokens=active_test_ids(pool)
    worker_count=min(len(tokens),len(eligible))
    successful={c['channel_id'] for c in cached} if progressive else set()
    job.update(total=len(eligible),phase='preflight',workers=[],parallelism=worker_count)
    job['search']={'eligible_total':len(catalogue),'attempted':0,'remaining':len(eligible),'target_members':pool['max_members'],'verified':len(successful),'exhausted':False,'cooldown_count':len(cooling),'cached_verified':len(cached)}
    queue=asyncio.Queue()
    for c in eligible:queue.put_nowait(c)
    budget=asyncio.Lock();persist=asyncio.Lock()
    requests=0;reserved=0.0;budget_exhausted=False
    job.setdefault('id','poolrun_'+__import__('uuid').uuid4().hex)
    display_limit=min(100,max(pool['max_candidates'],pool['max_members']))
    attempted_ids=set();token_rows={};passed_this_run=set()

    async def persist_visible():
        current=await manager.get(pool['id'])
        member_ids={m['channel_id'] for m in current.get('members',[])}
        ordered=sorted(candidates,key=lambda c:(0 if c['channel_id'] in member_ids else 1 if c.get('verification_status')=='verified' else 2 if c['channel_id'] in attempted_ids else 3,c['input_micros'] or 0))
        await asyncio.to_thread(manager.db.update_runtime,pool['id'],pool['revision'],candidates=ordered[:display_limit])

    async def reserve_for(token_id,candidate,challenge):
        nonlocal requests,reserved,budget_exhausted
        input_bound=len(challenge['prompt'].encode('utf8'))+4096
        probe_prices=candidate.get('_probe_prices') or {}
        def price(policy_key,candidate_key):
            frozen=probe_prices.get(candidate_key)
            if frozen is not None:return frozen
            if pool.get(policy_key) is not None:return micros(pool[policy_key])
            value=candidate.get(candidate_key)
            if type(value) is not int or value<0:raise ProbeError('probe_price_unknown')
            return max(1,value)
        input_price=sum(price(k,c) for k,c in [('max_input_usd','input_micros'),('max_cache_read_usd','cache_read_micros'),('max_cache_write_usd','cache_write_micros')])
        amount=(input_bound*input_price+8192*price('max_output_usd','output_micros'))/1e12
        async with budget:
            current=await manager.get(pool['id'])
            if current['revision']!=pool['revision']:raise ProbeError('pool_configuration_changed')
            if requests>=pool['max_probe_requests']:
                budget_exhausted=True;raise ProbeError('probe_budget_exhausted')
            reservation=await asyncio.to_thread(manager.billing.reserve,pool['id'],job['id'],token_id,candidate['channel_id'],amount,pool['max_probe_usd'],pool['auto_daily_usd'] if job.get('automatic') else None)
            if reservation is None:
                budget_exhausted=True
                raise ProbeError('auto_daily_budget_exhausted' if job.get('automatic') else 'probe_budget_exhausted')
            if job.get('automatic') and not await asyncio.to_thread(manager.auto.reserve_request,pool['id'],pool['auto_daily_requests'],time.time()):
                await asyncio.to_thread(manager.billing.cancel_unsent,reservation)
                budget_exhausted=True;raise ProbeError('auto_daily_budget_exhausted')
            requests+=1;reserved+=amount
            job.update(probe_requests=requests,reserved_cost_usd=round(reserved,6))
            job['billing']=await asyncio.to_thread(manager.billing.stats,pool['id'],job_id=job['id'])
            return reservation

    async def result_for(candidate,result):
        record={**candidate,'reason':None,**result,'verified_at':iso(__import__('time').time()),'reference_version':manager.monitor.catalog.get('source_commit')}
        if result.get('verification_status')=='verified' and not result.get('technical_confirmation'):record.pop('technical_confirmation',None)
        async with persist:
            saved=await asyncio.to_thread(manager.db.save_verification,pool['id'],pool['revision'],record)
            candidate.update(saved)
            attempted_ids.add(candidate['channel_id'])
            if result.get('verification_status')=='verified':
                successful.add(candidate['channel_id']);passed_this_run.add(candidate['channel_id'])
            else:successful.discard(candidate['channel_id'])
            await persist_visible()
            job['progress']+=1
            job['search'].update(attempted=job['progress'],remaining=queue.qsize(),verified=len(successful))
        if hasattr(manager,'auto'):
            await asyncio.to_thread(manager.auto.outcome,pool['id'],candidate['channel_id'],result.get('verification_status'),result.get('reason'),__import__('time').time(),job.get('id'))
            callback=getattr(manager,'result_callbacks',{}).get(job.get('id'))
            if callback is not None:await callback(candidate,result)

    async def worker(token_id,index):
        nonlocal budget_exhausted
        lane={'token_id':token_id,'index':index,'phase':'preflight','status':'running','current_channel_id':None,'current_supplier':None,'error_kind':None}
        job['workers'].append(lane)
        lock=manager.test_locks.setdefault(token_id,asyncio.Lock())
        lane_client=manager.client()
        route=None;owned=False;lane_pool={**pool,'test_token_id':token_id}
        try:
            async with lock:
                pins=await lane_client.pins()
                if any(p['token_id']==token_id and p['model_name']==pool['model'] for p in pins):raise PoolError('test_token_has_conflicting_pin',409)
                routes=await lane_client.list_subscriptions(token_id,pool['model'])
                owner=await asyncio.to_thread(manager.operations.get,token_id,pool['model'])
                if len(routes)>1:raise PoolError('ambiguous_test_route',409)
                route=await lane_client.get_subscription(routes[0]['id']) if routes else None
                if route and (not owner or owner['subscription_id']!=route['id']):raise PoolError('test_token_has_unmanaged_route',409)
                if route and owner['version']!=route['config_version']:
                    if not await reconcile_owned(manager,lane_client,pool,token_id,owner,route):raise PoolError('test_token_has_unmanaged_route',409)
                    owner=await asyncio.to_thread(manager.operations.get,token_id,pool['model'])
                if owner and owner['state']=='uncertain':
                    if not route or not await reconcile_owned(manager,lane_client,pool,token_id,owner,route):raise PoolError('test_route_requires_reconciliation',409)
                    route=await lane_client.get_subscription(route['id'])
                owned=bool(route)
                secret=await lane_client.token_secret(token_id)
                probe=TargetProbe(lane_client,manager.monitor,transport=manager.transport)
                async def activity(stage,**fields):
                    now=iso(time.time());lane.update(last_activity_at=now,request_stage=stage,**fields)
                    job.update(last_activity_at=now,updated_at=now)
                async def reserve(candidate,challenge):
                    await activity('reserving',challenge_id=challenge.get('id'),request_id=None,http_status=None)
                    return await reserve_for(token_id,candidate,challenge)
                async def settle(request_id,billed_token):
                    await activity('billing')
                    from .pool_billing_sync import settle_request
                    reservation=getattr(probe,'billing_reservation',None)
                    if reservation is None:return
                    try:await settle_request(manager.billing,lane_client,reservation,request_id,billed_token)
                    except (ValueError,A6Error):pass
                    job['billing']=await asyncio.to_thread(manager.billing.stats,pool['id'],job_id=job['id'])
                async def bind(request_id):
                    reservation=getattr(probe,'billing_reservation',None)
                    if reservation is not None:
                        try:await asyncio.to_thread(manager.billing.bind,reservation,request_id)
                        except ValueError:pass
                probe.activity=activity
                probe.billing_bind=bind
                probe.billing_settler=settle
                try:
                    while not budget_exhausted:
                        if progressive and len(successful)>=pool['max_members']:break
                        try:candidate=queue.get_nowait()
                        except asyncio.QueueEmpty:break
                        lane.update(phase='preflight',current_channel_id=candidate['channel_id'],current_supplier=candidate.get('supplier'),last_activity_at=iso(time.time()),started_at=iso(time.time()))
                        # Legacy single-worker job fields remain available.
                        if worker_count==1:job.update(phase='preflight',current_channel_id=candidate['channel_id'],current_supplier=candidate.get('supplier'))
                        current=await manager.get(pool['id'])
                        if current['revision']!=pool['revision']:raise PoolError('pool_configuration_changed',409)
                        rows,at=token_rows.get(token_id,([],0))
                        if not rows or time.monotonic()-at>60:
                            refresh_error=None
                            for refresh_attempt in range(2):
                                try:
                                    await activity('catalogue')
                                    rows=await lane_client.search(token_id,pool['model'])
                                    token_rows[token_id]=(rows,time.monotonic());refresh_error=None;break
                                except A6Error as failed:
                                    if failed.kind not in ('a6_search_snapshot_changed','a6_incomplete_search','a6_connection_failed','a6_http_429','a6_http_503','a6_http_504'):raise
                                    refresh_error=failed.kind
                                    if refresh_attempt==0:await asyncio.sleep(1)
                            if refresh_error:
                                job.setdefault('warnings',[]).append(refresh_error)
                                await result_for(candidate,{'verification_status':'skipped','probability':None,'actual_channel_id':None,'reason':refresh_error})
                                continue
                        same=next((r for r in rows if r['channel_id']==candidate['channel_id'] and r['listing_id']==candidate['listing_id']),None)
                        if not same or not manager.eligibility(pool,same)[0]:
                            await result_for(candidate,{'verification_status':'skipped','probability':None,'actual_channel_id':None,'reason':'test_token_candidate_no_longer_eligible'})
                            continue
                        draft=manager.route_draft(pool,[candidate['channel_id']])
                        frozen={}
                        for setting,remote,local in [('max_input_usd','input_price_micros','input_micros'),('max_output_usd','output_price_micros','output_micros'),('max_cache_read_usd','cache_read_price_micros','cache_read_micros'),('max_cache_write_usd','cache_write_price_micros','cache_write_micros')]:
                            if pool.get(setting) is None:
                                # No user constraint on production routing, but
                                # paid tests freeze observed prices for budget
                                # safety. Use1 for free listings: A6zero meansnone.
                                draft['caps'][remote]=max(1,same[remote])
                            frozen[local]=draft['caps'][remote]
                        candidate['_probe_prices']=frozen
                        await asyncio.to_thread(manager.operations.record,token_id,pool['model'],route['id'] if route else None,route['config_version'] if route else None,draft,'uncertain')
                        route=await lane_client.save_subscription(token_id,pool['model'],draft,route);owned=True
                        await asyncio.to_thread(manager.operations.record,token_id,pool['model'],route['id'],route['config_version'],draft,'owned')
                        lane['phase']='testing'
                        if worker_count==1:job['phase']='testing'
                        async with manager.probe_gate:
                            result=await probe.run(lane_pool,candidate,secret,reserve)
                            if job.get('kind')=='auto_audit' and candidate['channel_id'] in set(job.get('target_channel_ids',[])):
                                from .pool_audit_confirmation import confirm_member
                                result=await confirm_member(probe,lane_pool,candidate,secret,reserve,result)
                        lane['phase']='saving'
                        if worker_count==1:job['phase']='saving'
                        await result_for(candidate,result)
                finally:
                    lane['phase']='cleanup'
                    if worker_count==1:job['phase']='cleanup'
                    if route and owned:
                        import anyio
                        cleanup_ok=False
                        with anyio.move_on_after(35,shield=True):
                            try:
                                await park_owned(manager,lane_client,pool,token_id,route)
                                cleanup_ok=True
                            except Exception:pass
                        if not cleanup_ok:
                            await asyncio.to_thread(manager.operations.record,token_id,pool['model'],route['id'],route['config_version'],{},'uncertain')
                            lane['error_kind']='test_route_cleanup_uncertain'
                lane['status']='error' if lane['error_kind'] else 'done'
        except asyncio.CancelledError:
            lane.update(status='error',error_kind='cancelled');raise
        except (PoolError,A6Error,ValueError,ProbeError) as e:
            kind=getattr(e,'kind','pool_worker_failed')
            lane.update(status='error',error_kind=kind)
            candidate=next((c for c in candidates if c['channel_id']==lane.get('current_channel_id')),None)
            if candidate is not None and candidate.get('verification_status')=='unverified':
                await result_for(candidate,{'verification_status':'unknown','probability':None,'actual_channel_id':None,'reason':kind})
        finally:
            lane['current_channel_id']=None
            await lane_client.close()

    tasks=[asyncio.create_task(worker(token,i+1)) for i,token in enumerate(tokens[:worker_count])]
    group=asyncio.gather(*tasks,return_exceptions=True)
    try:
        results=await asyncio.shield(group)
        for result in results:
            if isinstance(result,BaseException):raise result
    except BaseException:
        # Parent cancellation must reach children exactly once. Cancelling an
        # already-cancelled gather again can interrupt their parked-route cleanup.
        for t in tasks:
            if not t.done():t.cancel()
        await asyncio.shield(group)
        raise
    # Do not mark untouched sellers failed/skipped merely because a shared
    # budget ended. They remain first-class fresh candidates for the next batch.
    worker_failed=any(w.get('status')=='error' for w in job['workers'])
    stop='budget_exhausted' if budget_exhausted else 'target_reached' if progressive and len(successful)>=pool['max_members'] else 'worker_error' if worker_failed and not queue.empty() else 'cooldown_wait' if not eligible and cooling else 'catalogue_exhausted'
    job['search'].update(remaining=queue.qsize(),verified=len(successful),exhausted=queue.empty() and stop!='target_reached')
    errors=list(dict.fromkeys(list(job.get('warnings',[]))+[w['error_kind'] for w in job['workers'] if w['status']=='error' and w.get('error_kind')]))
    job['phase']='saving'
    try:final=await manager.discover(await manager.get(pool['id']),client,full=True)
    except A6Error as failed:
        errors.append(failed.kind);final=candidates
    counts={key:sum(c.get('verification_status')==key for c in final) for key in ('verified','suspect','unknown','skipped','unverified')}
    job['warnings']=errors
    job['result']={'candidates':len(final),'verified':counts['verified'],'suspect':counts['suspect'],'unknown':counts['unknown'],'skipped':counts['skipped'],'unprocessed':counts['unverified'],'partial':bool(errors or counts['unverified'] and stop!='target_reached'),'checked_this_run':len(attempted_ids),'passed_this_run':len(passed_this_run),'cached_verified':len(cached),'requires_publication':True,'search_exhausted':job['search']['exhausted'],'stop_reason':stop}
    if errors and not counts['verified']:raise PoolError(errors[0],409)
    manager.verification_snapshots[job['id']]=(time.monotonic(),final)
    return final
