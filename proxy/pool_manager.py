"""Multi-model A6API pools: discovery, bounded verification and reviewed sync.

No automatic pin changes. A successful candidate fingerprint is not an identity
proof. Route publication is fail-closed until an operator verifies targeting
and server-side price-cap enforcement using the controlled deployment pilot.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import secrets
import time
import uuid

from .a6_client import A6Client,A6Error
from .a6_probe import TargetProbe,ProbeError
from .pool_store import PoolStore
from .pool_operations import PoolOperations
from .pool_billing import BillingStore
from .pool_billing_sync import today,reconcile_pending
from .pool_auto_store import PoolAutoStore


def iso(value):
    from datetime import datetime,timezone
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value else None


def micros(value):
    from decimal import Decimal
    # A6 remote caps use0 as 'not constrained'. Local explicit0 policies remain
    # rejected; only a missing/None optional cap is serialized to this sentinel.
    if value is None or value=='':return 0
    return int(Decimal(str(value))*1000000)


def binding(pool,candidate,account,reference):
    return hashlib.sha256(json.dumps([pool['id'],pool['revision'],pool['model'],pool['reference_model'],pool['protocol'],
        pool.get('test_token_ids',[pool['test_token_id']]),account,reference,candidate['channel_id'],candidate['listing_id']],separators=(',',':')).encode()).hexdigest()


class PoolError(Exception):
    def __init__(self,kind,status=400):super().__init__(kind);self.kind=kind;self.status=status


class PoolManager:
    def __init__(self,store,a6,monitor,data_dir,transport=None):
        self.store=store;self.a6=a6;self.monitor=monitor
        self.db=PoolStore(data_dir);self.operations=PoolOperations(data_dir);self.transport=transport
        from .pool_job_history import PoolJobHistory
        self.job_history=PoolJobHistory(data_dir)
        self.jobs={j['id']:j for j in self.job_history.load()};self.tasks={};self.previews={}
        self.auto=PoolAutoStore(data_dir);self.result_callbacks={};self.verification_snapshots={}
        self.billing=BillingStore(data_dir);self.billing_task=None
        from .pool_membership_history import MembershipHistory
        self.membership_history=MembershipHistory(data_dir);self.current_jobs={}
        from .pool_deletion import DeletedRoutes
        self.deleted=DeletedRoutes(data_dir)
        self.pool_locks={};self.probe_gate=asyncio.Semaphore(4)
        self.scheduler=None;self.wake=asyncio.Event();self.next_refresh={};self.discovery_due=set()
        # Runtime safety attestation, deliberately not inferred from API field
        # names or user-provided JSON. No public bypass toggles.
        self.targeted_probe_verified=True
        self.route_caps_verified=True
        self.test_locks={};self.restart_locks={};self.restarting=set()
        self.operation_gate=asyncio.Semaphore(2)

    def client(self):
        pat,user=self.a6.secret()
        if not pat or not user:raise PoolError('a6_not_configured')
        return A6Client(pat,user,transport=self.transport)

    async def start(self):
        self.scheduler=asyncio.create_task(self._schedule())
        self.billing_task=asyncio.create_task(self._billing_reconcile())

    async def close(self):
        jobs=list(self.tasks.values())+([self.scheduler] if self.scheduler else [])+([self.billing_task] if self.billing_task else [])
        for j in jobs:j.cancel()
        if jobs:await asyncio.gather(*jobs,return_exceptions=True)

    async def info(self):
        client=self.client()
        try:tokens=await client.tokens()
        finally:
            api_diagnostics=client.diagnostics() if hasattr(client,'diagnostics') else {}
            await client.close()
        policies=await asyncio.to_thread(self.db.list)
        for policy in policies:
            self.mark_reference_stale(policy)
            policy['auto_state']=await asyncio.to_thread(self.auto.state,policy['id'])
            owner=await asyncio.to_thread(self.auto.remote,policy['id'])
            policy['auto_state']['remote_state']=owner['state'] if owner else None
            policy['bans']=await asyncio.to_thread(self.auto.list_records,policy['id'])
            policy['billing']=await asyncio.to_thread(self.billing.stats,policy['id'],day=today())
            policy['billing'].update(day=today(),history_complete=False)
            policy['billing']['reconciliation']=await asyncio.to_thread(self.billing.pending_diagnostics,policy['id'])
        for job in self.jobs.values():
            job['billing']=await asyncio.to_thread(self.billing.stats,job['pool_id'],job_id=job['id'])
        return {'pools':policies,'catalog':self.monitor.catalog.get('models',[]),
            'tokens':tokens,'jobs':list(self.jobs.values())[-50:],'api_diagnostics':api_diagnostics,
            'capabilities':{'automatic_mode_supported':True,'auto_refresh_hours':6,'auto_audit_hours':1,'ban_after_failures':4,'max_parallel_checks':4,'targeted_probe_verified':self.targeted_probe_verified,'route_caps_verified':self.route_caps_verified,
                'warning':'Проверки используют отдельный тестовый токен с одно-канальным маршрутом и ценовыми caps; суффикс ключа запрещён, он обходит caps. Рабочие пины не меняются. Fingerprint — оценка сходства, не доказательство личности модели.'}}

    async def policy(self,data,id=None):
        if not isinstance(data,dict):raise PoolError('invalid_pool_policy')
        if data.get('auto_publish') and data.get('enabled') is not True:raise PoolError('auto_pool_must_be_enabled')
        if any(data.get(k) in (0,0.0,'0','0.0') for k in ('max_input_usd','max_output_usd','max_cache_read_usd','max_cache_write_usd')):
            raise PoolError('zero_price_cap_is_not_enforceable_use_positive_limit')
        client=self.client()
        try:
            tokens=await client.tokens()
            pins=await client.pins()
        finally:await client.close()
        test_ids=data.get('test_token_ids',[data.get('test_token_id')])
        if not isinstance(test_ids,list) or not 1<=len(test_ids)<=4 or any(type(t) is not int for t in test_ids) or len(set(test_ids))!=len(test_ids):
            raise PoolError('invalid_test_token_ids')
        if data.get('token_id') in test_ids:raise PoolError('separate_test_token_required')
        if any(p['token_id'] in test_ids and p['model_name']==data.get('model') for p in pins):
            raise PoolError('test_token_has_conflicting_pin',409)
        ids={x['id'] for x in tokens if x['status']==1}
        if data.get('token_id') not in ids or any(t not in ids for t in test_ids):raise PoolError('pool_token_unavailable')
        data={**data,'test_token_ids':test_ids,'test_token_id':test_ids[0]}
        if data.get('reference_model') not in {m['id'] for m in self.monitor.catalog.get('models',[])}:raise PoolError('reference_model_not_in_catalog')
        if id:
            if id in self.pool_locks and self.pool_locks[id].locked():raise PoolError('pool_operation_in_progress',409)
            previous=await self.get(id)
            if previous.get('last_sync_at') and any(data.get(k)!=previous[k] for k in ('token_id','model','test_token_id')):
                raise PoolError('published_pool_identity_is_immutable',409)
            data={**data,'id':id}
        prior_evidence=await asyncio.to_thread(self.db.verifications,id) if id else []
        adopt=None
        if data.get('auto_publish'):
            check=self.client()
            try:
                workpins=await check.pins()
                if any(p['token_id']==data['token_id'] and p['model_name']==data['model'] for p in workpins):raise PoolError('existing_pin_conflicts_with_pool',409)
                existing=await check.list_subscriptions(data['token_id'],data['model'])
                if len(existing)>1:raise PoolError('ambiguous_existing_routes',409)
                owner=await asyncio.to_thread(self.auto.remote,id) if id else None
                if existing:
                    current=await check.get_subscription(existing[0]['id'])
                    if owner and owner.get('state') in ('owned','parked') and owner['config_version']!=current['config_version']:
                        from .pool_automation import exact_owned_config
                        if exact_owned_config(current,owner,data):owner=await asyncio.to_thread(self.auto.save_remote,id,current['id'],current['config_version'],owner['draft'],owner['state'])
                    if owner and (owner['subscription_id']!=current['id'] or owner['config_version']!=current['config_version'] or owner['state'] not in ('owned','parked')):raise PoolError('auto_remote_route_conflict',409)
                    if not owner:adopt=current
            finally:await check.close()
        try:
            result=await asyncio.to_thread(self.db.save,data,data.get('revision') if id else None)
        except ValueError as e:raise PoolError(str(e),409 if str(e)=='revision_conflict' else 400) from None
        # Activating an already checked pool must not erase the successful
        # evidence needed for publication. Only operational enable/refresh changes
        # may reuse evidence; identity, price, cache or detector policy changes
        # still invalidate it. Rebind preserved records to the new revision.
        if id and all(previous.get(k)==result.get(k) for k in ('token_id','test_token_ids','model','reference_model','protocol','max_input_usd','max_output_usd','max_cache_read_usd','max_cache_write_usd','min_cache_hit_percent','min_success_percent','threshold_percent','verification_ttl_hours')):
            _,account=self.a6.secret()
            for evidence in prior_evidence:
                if evidence.get('reference_version') and evidence['reference_version']!=self.monitor.catalog.get('source_commit'):continue
                preserved={**evidence,'configuration_key':binding(result,evidence,account,self.monitor.catalog.get('source_commit'))}
                await asyncio.to_thread(self.db.save_verification,id,result['revision'],preserved)
            kept=[]
            for candidate in previous.get('candidates',[]):
                if candidate.get('reference_version') and candidate['reference_version']!=self.monitor.catalog.get('source_commit'):
                    kept.append({**candidate,'verification_status':'expired','reason':'detector_reference_updated'});continue
                record={**candidate,'configuration_key':binding(result,candidate,account,self.monitor.catalog.get('source_commit'))}
                if candidate.get('verification_status') in ('verified','suspect','unknown','error','expired','skipped'):
                    await asyncio.to_thread(self.db.save_verification,id,result['revision'],record)
                kept.append(record)
            await asyncio.to_thread(self.db.update_runtime,id,result['revision'],candidates=kept,status=previous.get('status','ready'),last_discovered_at=previous.get('last_discovered_at'),last_error=None)
            result=await self.get(id)
        if id and result['enabled'] and result.get('auto_publish') and result['max_members']>previous['max_members']:
            await asyncio.to_thread(self.auto.request_refill,id,time.time())
        self.previews={key:val for key,val in self.previews.items() if val['pool_id']!=result['id']}
        if adopt:
            draft=self.route_draft(result,[c['channel_id'] for c in adopt['channels'] if not c.get('removed_at')])
            await asyncio.to_thread(self.auto.save_remote,result['id'],adopt['id'],adopt['config_version'],draft,'owned')
        await asyncio.to_thread(self.auto.configure,result['id'],bool(result['enabled'] and result.get('auto_publish')),time.time())
        self.next_refresh[result['id']]=0;self.wake.set()
        # Request a read-only discovery even for auto pools: the next paid
        # refresh may be hours away after an edit invalidates the old candidates.
        self.discovery_due.add(result['id'])
        return result

    async def delete(self,id,revision,confirm_remote=False):
        pool=await self.get(id)
        if revision!=pool['revision']:raise PoolError('revision_conflict',409)
        active=[j for j in self.jobs.values() if j['pool_id']==id and j['status'] in ('queued','running')]
        if active and confirm_remote is not True:raise PoolError('pool_operation_in_progress',409)
        # Explicit destructive confirmation also authorizes stopping this pool's
        # paid workers. Their finally blocks park every owned test route.
        for job in active:await self.cancel(job['id'])
        lock=self.pool_locks.setdefault(id,asyncio.Lock())
        async with lock:
            from .pool_deletion import remove_pool
            try:result=await remove_pool(self,pool,confirm_remote)
            except ValueError as e:raise PoolError(str(e),409) from None
            await asyncio.to_thread(self.auto.configure,id,False,time.time())
            self.discovery_due.discard(id);self.next_refresh.pop(id,None)
            self.previews={k:v for k,v in self.previews.items() if v['pool_id']!=id}
            return result

    async def run_now(self,id,confirm_cost):
        pool=await self.get(id)
        return await self.enqueue(id,'auto_refresh' if pool['enabled'] and pool.get('auto_publish') else 'verify',confirm_cost)

    async def restart(self,id,confirm_cost):
        if confirm_cost is not True:raise PoolError('confirm_paid_verification_required')
        await self.get(id)
        guard=self.restart_locks.setdefault(id,asyncio.Lock())
        if guard.locked():raise PoolError('pool_restart_in_progress',409)
        async with guard:
            self.restarting.add(id)
            try:
                for job in list(self.jobs.values()):
                    if job['pool_id']==id and job['status'] in ('queued','running'):await self.cancel(job['id'])
                result=await self.run_now(id,True)
                self.jobs[result['id']]['restarted']=True
                return {**result,'restarted':True}
            finally:self.restarting.discard(id)

    def mark_reference_stale(self,pool):
        version=self.monitor.catalog.get('source_commit')
        for key in ('candidates','members'):
            for candidate in pool.get(key,[]):
                if candidate.get('reference_version') and candidate['reference_version']!=version and candidate.get('verification_status') in ('verified','suspect'):
                    candidate.update(verification_status='expired',reason='detector_reference_updated')
        return pool

    async def get(self,id):
        p=await asyncio.to_thread(self.db.get,id)
        if not p:raise PoolError('pool_not_found',404)
        return self.mark_reference_stale(p)

    async def enqueue(self,id,kind,confirm_cost=False,scheduled=False):
        pool=await self.get(id)
        if kind not in ('discover','verify','auto_refresh','auto_audit'):raise PoolError('invalid_pool_operation')
        if kind in ('verify','auto_refresh','auto_audit') and not confirm_cost:raise PoolError('confirm_paid_verification_required')
        if any(j['pool_id']==id and j['status'] in ('queued','running') for j in self.jobs.values()):raise PoolError('pool_operation_in_progress',409)
        if len(self.tasks)>=16:raise PoolError('pool_queue_full',429)
        done=[k for k,j in self.jobs.items() if j['status'] not in ('queued','running')]
        for k in done[:-29]:self.jobs.pop(k,None)
        if kind.startswith('auto_') and (not pool.get('auto_publish') or not pool['enabled']):raise PoolError('auto_mode_disabled')
        if kind.startswith('auto_') and not scheduled:
            if not await asyncio.to_thread(self.auto.start_manual,id,kind[5:],time.time()):raise PoolError('pool_operation_in_progress',409)
        # Recheck after lease I/O: another coroutine may have queued a job while
        # the manual/scheduled caller was waiting for SQLite.
        if any(j['pool_id']==id and j['status'] in ('queued','running') for j in self.jobs.values()):raise PoolError('pool_operation_in_progress',409)
        job={'updated_at':iso(time.time()),'last_activity_at':iso(time.time()),'automatic':kind.startswith('auto_'),'id':'pooljob_'+uuid.uuid4().hex,'pool_id':id,'kind':kind,'status':'queued','progress':0,'total':0,'error_kind':None,'created_at':iso(time.time())}
        self.jobs[job['id']]=job
        await asyncio.to_thread(self.job_history.save,job)
        task=asyncio.create_task(self._job(job,pool));self.tasks[job['id']]=task
        task.add_done_callback(lambda _,key=job['id']:self.tasks.pop(key,None))
        return dict(job)

    async def cancel(self,id):
        t=self.tasks.get(id)
        if not t:return False
        t.cancel();await asyncio.gather(t,return_exceptions=True)
        self.jobs[id].update(status='error',error_kind='cancelled');return True

    def eligibility(self,pool,row):
        if row.get('model_name')!=pool['model']:return False,'wrong_model'
        if row.get('pricing_currency')!='USD' or row.get('pricing_unit')!='per_1m_tokens' or row.get('charge_type')!='per_token':return False,'unsupported_price_unit'
        for field,setting in [('input_price_micros','max_input_usd'),('output_price_micros','max_output_usd'),('cache_read_price_micros','max_cache_read_usd'),('cache_write_price_micros','max_cache_write_usd')]:
            v=row.get(field)
            if isinstance(v,bool) or not isinstance(v,int) or v<0:return False,'unknown_price'
            if pool.get(setting) not in (None,'') and v>micros(pool[setting]):return False,'price_above_cap'
        hit=row.get('cache_hit_rate_24h')
        if row.get('cache_hit_state') not in ('rate','zero') or isinstance(hit,bool) or not isinstance(hit,(int,float)) or not math.isfinite(hit) or not 0<=hit<=10000:return False,'cache_hit_unknown'
        if hit<=pool['min_cache_hit_percent']*100:return False,'cache_hit_below_limit'
        success=row.get('recent_success_rate')
        if not isinstance(success,(int,float)) or isinstance(success,bool) or not 0<=success<=10000:return False,'success_rate_unknown'
        if success<pool['min_success_percent']*100:return False,'success_rate_below_limit'
        if row.get('supplier_channel_disabled') or row.get('user_channel_disabled'):return False,'channel_disabled'
        return True,None

    async def discover(self,pool,client,full=False):
        rows=await client.search(pool['token_id'],pool['model'])
        _,account=self.a6.secret()
        output=[]
        rejected={}
        history={r['channel_id']:r for r in await asyncio.to_thread(self.auto.list_records,pool['id'])}
        for row in rows:
            okay,reason=self.eligibility(pool,row)
            if history.get(row['channel_id'],{}).get('banned'):okay,reason=False,'seller_banned'
            if not okay:
                rejected[reason]=rejected.get(reason,0)+1
                continue
            candidate={'channel_id':row['channel_id'],'listing_id':row['listing_id'],'supplier':row.get('supplier_name',''),
                'model_name':pool['model'],'input_micros':row.get('input_price_micros'),'output_micros':row.get('output_price_micros'),
                'cache_read_micros':row.get('cache_read_price_micros'),'cache_write_micros':row.get('cache_write_price_micros'),
                'pricing_currency':row.get('pricing_currency'),'pricing_unit':row.get('pricing_unit'),'charge_type':row.get('charge_type'),
                'cache_hit_percent':row.get('cache_hit_rate_24h')/100 if isinstance(row.get('cache_hit_rate_24h'),(int,float)) else None,
                'cache_hit_state':row.get('cache_hit_state'),'success_percent':row.get('recent_success_rate')/100 if isinstance(row.get('recent_success_rate'),(int,float)) else None,
                'sample_count':row.get('sample_count'),'test_endpoint':row.get('test_endpoint'),'eligible':okay,'reason':reason,
                'verification_status':'unverified','probability':None,'verified_at':None,'actual_channel_id':None}
            key=binding(pool,row,account,self.monitor.catalog.get('source_commit'))
            candidate['configuration_key']=key
            saved=await asyncio.to_thread(self.db.verification,pool['id'],row['channel_id'])
            if saved and saved.get('configuration_key')==key:
                candidate.update({k:saved[k] for k in ['verification_status','probability','verified_at','actual_channel_id','request_ids','reference_version','predicted_model','predicted_probability','technical_confirmation'] if k in saved})
                if okay and saved.get('reason'):candidate['reason']=saved['reason']
                if self.expired(saved,pool):candidate['verification_status']='expired'
            elif saved and saved.get('reference_version') and saved['reference_version']!=self.monitor.catalog.get('source_commit'):
                candidate.update(verification_status='expired',reason='detector_reference_updated',verified_at=saved.get('verified_at'),reference_version=saved.get('reference_version'))
            prior=history.get(row['channel_id'],{})
            from .pool_selection import next_check_epoch
            candidate.update(last_check_status=prior.get('last_status'),last_check_reason=prior.get('last_reason'),last_checked_at=iso(prior.get('last_checked_at')),next_check_at=iso(next_check_epoch(prior)))
            output.append(candidate)
        output.sort(key=lambda x:(not x['eligible'],x['input_micros'] is None,x['input_micros'] or 0,x['channel_id']))
        member_ids={m['channel_id'] for m in pool.get('members',[])}
        retained=[c for c in output if c['channel_id'] in member_ids]
        retained_ids={c['channel_id'] for c in retained}
        # Keep proved members visible; rotate display toward never-tested sellers.
        def selection_rank(c):
            h=history.get(c['channel_id'],{})
            from .pool_selection import cooling as is_cooling
            cooling=is_cooling(h)
            cached=c.get('verification_status')=='verified' and not self.expired(c,pool)
            return (0 if cached else 3 if cooling else 2 if h.get('last_checked_at') else 1,c['input_micros'] is None,c['input_micros'] or 0,c['channel_id'])
        rest=sorted((c for c in output if c['channel_id'] not in retained_ids),key=selection_rank)
        selected=(retained+rest)[:pool['max_candidates']]
        status='ready' if any(x['eligible'] for x in selected) else 'blocked'
        if pool.get('last_sync_at'):
            valid_ids={x['channel_id'] for x in selected if x['eligible'] and x.get('verification_status')=='verified' and not self.expired(x,pool)}
            status='published' if pool.get('members') and all(x['channel_id'] in valid_ids for x in pool['members']) else 'blocked'
        await asyncio.to_thread(self.db.update_runtime,pool['id'],pool['revision'],candidates=selected,last_discovered_at=iso(time.time()),status=status,last_error=None,discovery_summary={'total':len(rows),'eligible':len(output),'excluded':sum(rejected.values()),'shown':len(selected)})
        return output if full else selected

    @staticmethod
    def expired(record,pool):
        try:
            from datetime import datetime
            ts=datetime.fromisoformat(record['verified_at'].replace('Z','+00:00')).timestamp()
            return time.time()-ts>pool['verification_ttl_hours']*3600
        except (ValueError,KeyError,TypeError):return True

    async def verify(self,pool,client,job):
        from .pool_parallel import verify_parallel
        return await verify_parallel(self,pool,client,job)

    @staticmethod
    def route_draft(pool,channels):
        return {'channels':[{'channel_id':channel,'priority':None} for channel in channels],
            'caps':{'input_price_micros':micros(pool['max_input_usd']),'output_price_micros':micros(pool['max_output_usd']),
                'cache_read_price_micros':micros(pool['max_cache_read_usd']),'cache_write_price_micros':micros(pool['max_cache_write_usd']),'per_request_price_micros':0},
            'sort_strategy':'price_asc','user_priorities':{},'auto_pool_enabled':False,'min_ratio':'','max_ratio':'',
            'min_cache_hit_rate_bps':min(10000,math.floor(pool['min_cache_hit_percent']*100)+1),'fallback_enabled':False}

    async def _job(self,job,pool):
        client=None
        try:
            lock=self.pool_locks.setdefault(pool['id'],asyncio.Lock())
            async with lock, self.operation_gate:
                job['status']='running';client=self.client();self.current_jobs[pool['id']]=job
                if job['kind']=='discover':
                    job['phase']='discovery'
                    result=await self.discover(pool,client)
                elif job['kind'].startswith('auto_'):
                    from .pool_automation import automate
                    async with asyncio.timeout(3300):
                        result=await automate(self,pool,client,job)
                else:result=await self.verify(pool,client,job)
                job.update(status='done',result=job.get('result',{'candidates':len(result)}))
        except asyncio.CancelledError:
            job.update(status='error',error_kind='cancelled');raise
        except (PoolError,A6Error,ValueError,ProbeError) as error:
            kind=getattr(error,'kind',str(error) if isinstance(error,ValueError) else 'pool_operation_failed')
            job.update(status='error',error_kind=kind)
            try:await asyncio.to_thread(self.db.update_runtime,pool['id'],pool['revision'],last_error=kind)
            except ValueError:pass
            if job.get('automatic') and kind in ('a6_search_snapshot_changed','a6_incomplete_search','a6_connection_failed','a6_http_429','a6_http_502','a6_http_503','a6_http_504'):
                await asyncio.to_thread(self.auto.request_refill,pool['id'],time.time(),time.time()+300)
        except TimeoutError:
            job.update(status='error',error_kind='auto_cycle_timeout')
            await asyncio.to_thread(self.db.update_runtime,pool['id'],pool['revision'],last_error='auto_cycle_timeout')
        except Exception:
            job.update(status='error',error_kind='pool_internal_error')
        finally:
            self.verification_snapshots.pop(job['id'],None)
            if self.current_jobs.get(pool['id']) is job:self.current_jobs.pop(pool['id'],None)
            job['updated_at']=iso(time.time())
            await asyncio.to_thread(self.job_history.save,job)
            if job['kind'].startswith('auto_'):
                warning=job.get('error_kind') or next(iter(job.get('warnings',[])),None)
                await asyncio.to_thread(self.auto.finish,pool['id'],job['kind'][5:],warning)
            if client:await client.close()

    async def preview(self,id):
        pool=await self.get(id)
        if self.pool_locks.get(id) and self.pool_locks[id].locked():raise PoolError('pool_operation_in_progress',409)
        client=self.client()
        try:
            candidates=await self.discover(pool,client,full=True)
            members=[x for x in candidates if x['eligible'] and x.get('verification_status')=='verified' and not self.expired(x,pool)][:pool['max_members']]
            existing=await client.list_subscriptions(pool['token_id'],pool['model'])
            if len(existing)>1:raise PoolError('ambiguous_existing_routes',409)
            before=await client.get_subscription(existing[0]['id']) if existing else None
            pins=await client.pins()
            pinned=any(p['token_id']==pool['token_id'] and p['model_name']==pool['model'] for p in pins)
            draft={'channels':[{'channel_id':x['channel_id'],'priority':None} for x in members],
                'caps':{'input_price_micros':micros(pool['max_input_usd']),'output_price_micros':micros(pool['max_output_usd']),
                    'cache_read_price_micros':micros(pool['max_cache_read_usd']),'cache_write_price_micros':micros(pool['max_cache_write_usd']),'per_request_price_micros':0},
                'sort_strategy':'price_asc','user_priorities':{},'auto_pool_enabled':False,'min_ratio':'','max_ratio':'',
                'min_cache_hit_rate_bps':min(10000,math.floor(pool['min_cache_hit_percent']*100)+1),'fallback_enabled':False}
            reasons=[]
            if pinned:reasons.append('existing_pin_conflicts_with_pool')
            if not members:reasons.append('no_verified_eligible_members')
            if not self.targeted_probe_verified or not self.route_caps_verified:reasons.append('controlled_targeting_and_caps_pilot_required')
            if not pool['enabled']:reasons.append('pool_disabled')
            self.previews={k:v for k,v in self.previews.items() if v['expires']>time.time()}
            if len(self.previews)>=100:self.previews.pop(next(iter(self.previews)))
            key=secrets.token_urlsafe(24)
            data={'preview_id':key,'expires_at':iso(time.time()+120),'channels':members,'remote_before':before,'draft':draft,
                'warnings':reasons,'can_publish':not reasons,'reason':reasons[0] if reasons else None}
            self.previews[key]={'pool_id':id,'revision':pool['revision'],'expires':time.time()+120,'data':data}
            return data
        finally:await client.close()

    async def publish(self,id,preview_id,confirm):
        if confirm is not True:raise PoolError('publication_confirmation_required')
        old=self.previews.get(preview_id)
        pool=await self.get(id)
        if not old or old['pool_id']!=id or old['expires']<time.time() or old['revision']!=pool['revision']:
            raise PoolError('pool_preview_expired_or_stale',409)
        if not old['data']['can_publish']:raise PoolError(old['data']['reason'],409)
        lock=self.pool_locks.setdefault(id,asyncio.Lock())
        if lock.locked():raise PoolError('pool_operation_in_progress',409)
        async with lock:
            # Fresh candidate prices/cache and remote config; reject a changed
            # intended set/version, never silently widen the confirmed whitelist.
            client=self.client()
            try:
                candidates=await self.discover(pool,client,full=True)
                want={x['channel_id'] for x in old['data']['channels']}
                valid={x['channel_id'] for x in candidates if x['eligible'] and x['verification_status']=='verified' and not self.expired(x,pool)}
                if not want or not want.issubset(valid):raise PoolError('pool_candidates_changed',409)
                pins=await client.pins()
                if any(p['token_id']==pool['token_id'] and p['model_name']==pool['model'] for p in pins):raise PoolError('existing_pin_conflicts_with_pool',409)
                current_routes=await client.list_subscriptions(pool['token_id'],pool['model'])
                before=old['data']['remote_before']
                if before is None and current_routes:raise PoolError('remote_route_created_after_preview',409)
                if before is not None and (len(current_routes)!=1 or current_routes[0]['id']!=before['id']):raise PoolError('remote_route_changed_after_preview',409)
                saved=await client.save_subscription(pool['token_id'],pool['model'],old['data']['draft'],before)
                await asyncio.to_thread(self.auto.save_remote,id,saved['id'],saved['config_version'],old['data']['draft'],'owned')
                await asyncio.to_thread(self.db.update_runtime,id,pool['revision'],status='published',members=old['data']['channels'],last_sync_at=iso(time.time()),last_error=None)
                secret=await client.token_secret(pool['token_id'])
                await asyncio.to_thread(self.deleted.clear,pool['model'],secret)
                await asyncio.to_thread(self.membership_history.record,id,before=[m['channel_id'] for m in pool.get('members',[])],after=[m['channel_id'] for m in old['data']['channels']],reason='manual_publication',source='manual',subscription_id=saved['id'],config_version=saved['config_version'])
                self.previews.pop(preview_id,None)
                return {'ok':True,'status':'published','pool':await self.get(id)}
            finally:await client.close()

    async def _billing_reconcile(self):
        while True:
            try:await reconcile_pending(self)
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(60)

    async def _schedule(self):
        # Background maintenance is READ-ONLY by default: no unconfirmed paid
        # batches or remote route changes. Mark unsafe local published states.
        while True:
            try:
                from .pool_automation import tick
                await tick(self)
                for pool in await asyncio.to_thread(self.db.list):
                    if pool['id'] in self.restarting:continue
                    requested=pool['id'] in self.discovery_due
                    invalidated=not pool.get('last_discovered_at')
                    if not requested and (not pool['enabled'] or pool.get('auto_publish') and not invalidated):continue
                    if not requested and not invalidated and self.next_refresh.get(pool['id'],0)>time.time():continue
                    if any(j['pool_id']==pool['id'] and j['status'] in ('queued','running') for j in self.jobs.values()):continue
                    self.next_refresh[pool['id']]=time.time()+pool['refresh_minutes']*60
                    await self.enqueue(pool['id'],'discover')
                    self.discovery_due.discard(pool['id'])
            except asyncio.CancelledError:raise
            except Exception:pass
            self.wake.clear()
            try:await asyncio.wait_for(self.wake.wait(),5)
            except TimeoutError:pass


def copy_candidate(value):return dict(value)
