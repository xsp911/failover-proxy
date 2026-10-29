"""Embedded fingerprint probes, bounded jobs and persistent quarantine policy.

The vendored scorer is a local, no-network subprocess, NOT another service.
Only synthetic fingerprint challenges reach providers; keys never reach Node.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime
from pathlib import Path
import re
import shutil
import time
import uuid

from .engine import Engine, GatewayError
from .detector_store import DetectorStore
from .probe_rounds import collect_round, complete_analysis

LOG = logging.getLogger('failover.detector')
ROOT = Path(__file__).resolve().parent.parent
WARNING = ('Оценка — относительная вероятность внутри эталонной базы lm-detector, '
           'не доказательство подлинности. Каждый полный тест делает 3 платных запроса. '
           'При временных ошибках до двух повторов задания, при низкой оценке — второй полный тест; максимум 18 запросов. '
           'Ошибки API и недостаточные данные не считаются подменой и не снимают прежний карантин.')


def stamp(value):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value else None


def configuration_key(provider, model, reference_model):
    # Opaque hash binds decisions to the exact credentials, URL, model and
    # algorithm/reference snapshot; no secret input is persisted in plaintext.
    from .provider_policy import routing_identity
    parts=[provider['id'],provider['base_url'],provider.get('api_key'),model,reference_model]
    policy=routing_identity(provider)
    if policy is not None:parts.append(policy)
    if provider.get('proxy_id'):parts.append({'proxy_id':provider['proxy_id']})
    raw=json.dumps(parts,separators=(',',':'))
    return hashlib.sha256(raw.encode()).hexdigest()


class ProbeMetrics:
    """Detector requests intentionally don't inflate application traffic stats."""
    def record_attempt(self, _):
        pass
    def record_request(self, _):
        pass


class ModelMonitor:
    def __init__(self,store,data_dir,transport=None):
        self.store=store
        self.db=DetectorStore(Path(data_dir))
        self.transport=transport
        self.jobs={}
        self.tasks={}
        self.committing=set()
        self.worker=asyncio.Semaphore(3)
        self.provider_workers={}
        self.scorer=asyncio.Semaphore(1)
        self.wake=asyncio.Event()
        self.scheduler=None
        self.next_run=0
        self.blocked=set()
        self.catalog=self._catalog()
        self.detector_root=(ROOT/'detector').resolve()
        self.node=shutil.which('node')
        self.available=bool(self.node and self.catalog.get('models') and all((ROOT/'detector'/name).is_file() for name in ('score.mjs','shared_detector.bin','bank.json')))

    def _catalog(self):
        try:
            result=json.loads((ROOT/'detector'/'catalog.json').read_text(encoding='utf-8'))
            if not isinstance(result.get('models'),list) or not isinstance(result.get('challenges'),list):
                return {}
            return result
        except (OSError,ValueError,TypeError):
            return {}

    async def start(self):
        await self.refresh()
        # Resume the regular cadence after an update instead of launching a
        # duplicate paid monitor cycle solely because the process restarted.
        settings=await asyncio.to_thread(self.db.settings)
        if settings['enabled']:
            recent=await asyncio.to_thread(self.db.latest_results,200)
            scheduled=[datetime.fromisoformat(r['checked_at'].replace('Z','+00:00')).timestamp()-max(0,r.get('duration_ms',0))/1000 for r in recent if r.get('source')=='scheduled' and isinstance(r.get('checked_at'),str)]
            if scheduled:self.next_run=max(time.time(),max(scheduled)+settings['interval_minutes']*60)
        self.scheduler=asyncio.create_task(self._schedule())

    async def close(self):
        tasks=list(self.tasks.values())
        if self.scheduler:tasks.append(self.scheduler)
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

    @staticmethod
    def provider_models(config,provider_id):
        return sorted({r['upstream_model'] for m in config['models'] if m['enabled'] for r in m['routes'] if r['provider_id']==provider_id and r['enabled']})

    def bindings(self,config,settings):
        providers={p['id']:p for p in config['providers'] if p['enabled'] and p.get('api_key')}
        bindings=[]
        for target in settings['targets']:
            p=providers.get(target['provider_id'])
            if p and target['model'] in self.provider_models(config,p['id']):
                legacy=[hashlib.sha256((configuration_key(p,target['model'],target['reference_model'])+':'+version).encode()).hexdigest() for version in self.catalog.get('previous_source_commits',[])]
                bindings.append({**target,'configuration_key':self.binding_key(p,target['model'],target['reference_model']),'previous_configuration_keys':legacy})
        return bindings

    def binding_key(self,provider,model,reference_model):
        key=configuration_key(provider,model,reference_model)
        if provider.get('proxy_id'):
            from .outbound_proxy import resolve_proxy
            from .proxy_transport import proxy_fingerprint
            try:
                profile=resolve_proxy(self.store.get_config(True),provider['proxy_id'])
                key=hashlib.sha256((key+':'+proxy_fingerprint(profile)).encode()).hexdigest()
            except ValueError:key=hashlib.sha256((key+':unavailable-proxy').encode()).hexdigest()
        return hashlib.sha256((key+':'+self.catalog.get('source_commit','unknown')).encode()).hexdigest()

    async def refresh(self):
        config=await asyncio.to_thread(self.store.get_config,True)
        settings=await asyncio.to_thread(self.db.settings)
        self.blocked=await asyncio.to_thread(self.db.blocked_provider_ids,self.bindings(config,settings))
        return config,settings

    async def blocked_providers(self,config=None):
        # A queued request may hold an older routing snapshot. Decisions must
        # bind to that exact snapshot, never a separately reread credential/URL.
        if config is not None:
            settings=await asyncio.to_thread(self.db.settings)
            return await asyncio.to_thread(self.db.blocked_provider_ids,self.bindings(config,settings))
        await self.refresh()
        return set(self.blocked)

    async def info(self):
        config,settings=await self.refresh()
        states=await asyncio.to_thread(self.db.states)
        # Hide obsolete backend/reference snapshots from current UI; retain
        # historical transitions in the log for traceability.
        pmap={p['id']:p for p in config['providers']}
        visible=[]
        for state in states:
            provider=pmap.get(state.get('provider_id'))
            if not provider:continue
            current=self.binding_key(provider,state['model'],state['reference_model'])
            if state.get('configuration_key')==current:visible.append(state);continue
            legacy={hashlib.sha256((configuration_key(provider,state['model'],state['reference_model'])+':'+version).encode()).hexdigest() for version in self.catalog.get('previous_source_commits',[])}
            if state.get('configuration_key') in legacy:
                visible.append({**state,'status':'unknown','latest_status':'unknown','error_kind':'detector_reference_updated','stale_reference':True})
        states=visible
        providers=[{'id':p['id'],'name':p['name'],'enabled':p['enabled'],'models':self.provider_models(config,p['id'])} for p in config['providers']]
        return {'available':self.available,'warning':WARNING,'catalog':{'models':self.catalog.get('models',[]),'source_commit':self.catalog.get('source_commit')},
                'settings':settings,'providers':providers,'states':states,'jobs':list(self.jobs.values())[-30:],
                'max_parallel_checks':3,'per_provider_parallel_checks':1,'blocked_provider_ids':sorted(self.blocked),'next_run_at':stamp(self.next_run) if settings['enabled'] else None}

    async def validate_routing_change(self,config):
        settings=await asyncio.to_thread(self.db.settings)
        if not settings['enabled']:return
        selected={(t['provider_id'],t['model']) for t in settings['targets']}
        required={(p['id'],model) for p in config['providers'] if p['enabled'] for model in self.provider_models(config,p['id'])}
        if not required.issubset(selected):
            raise ValueError('disable_monitor_before_adding_unmonitored_routes')

    async def save_settings(self,data):
        if not isinstance(data,dict):raise ValueError('invalid_monitor_settings')
        threshold=data.get('threshold_percent')
        if isinstance(threshold,bool) or not isinstance(threshold,(int,float)) or not math.isfinite(threshold) or not 50<=threshold<=100:
            raise ValueError('invalid_threshold_percent')
        if not isinstance(data.get('enabled'),bool):
            raise ValueError('invalid_monitor_enabled')
        interval=data.get('interval_minutes')
        if isinstance(interval,bool) or not isinstance(interval,int) or not 5<=interval<=1440:
            raise ValueError('invalid_monitor_interval')
        config=await asyncio.to_thread(self.store.get_config,True)
        refs={m['id'] for m in self.catalog.get('models',[])}
        pmap={p['id']:p for p in config['providers']}
        if not isinstance(data.get('targets'),list) or len(data['targets'])>50:
            raise ValueError('invalid_targets')
        for t in data['targets']:
            if not isinstance(t,dict) or t.get('provider_id') not in pmap or t.get('reference_model') not in refs:
                raise ValueError('invalid_monitor_target')
            if t.get('model') not in self.provider_models(config,t['provider_id']):
                raise ValueError('monitor_model_not_in_provider_routes')
        if data.get('enabled') and (not self.available or not data['targets']):
            raise ValueError('detector_or_targets_unavailable')
        # Enabling monitoring must cover all currently enabled configured routes;
        # user explicitly chooses the reference where no exact match exists.
        if data.get('enabled'):
            required={(p['id'],m) for p in config['providers'] if p['enabled'] and p.get('api_key') for m in self.provider_models(config,p['id'])}
            actual={(t['provider_id'],t['model']) for t in data['targets']}
            if not required.issubset(actual):raise ValueError('monitor_targets_must_cover_all_enabled_routes')
        previous=await asyncio.to_thread(self.db.settings)
        result=await asyncio.to_thread(self.db.save_settings,data,data.get('revision'))
        schedule_changed=any(previous.get(k)!=result.get(k) for k in ('enabled','interval_minutes','targets'))
        if not result['enabled']:
            self.next_run=0
        elif schedule_changed:
            self.next_run=time.time()
        await self.refresh()
        self.wake.set()
        return result

    async def enqueue(self,provider_id,model,reference_model,source='manual'):
        if not self.available:raise ValueError('detector_unavailable')
        if not isinstance(model,str) or not 1<=len(model)<=200 or any(x.isspace() or ord(x)<32 for x in model):
            raise ValueError('invalid_model')
        if not isinstance(provider_id,str) or not isinstance(reference_model,str) or reference_model not in {m['id'] for m in self.catalog['models']}:
            raise ValueError('reference_model_not_in_catalog')
        config=await asyncio.to_thread(self.store.get_config,True)
        provider=next((p for p in config['providers'] if p['id']==provider_id),None)
        if not provider or not provider.get('api_key'):raise ValueError('provider_not_found_or_missing_key')
        if any(j['status'] in ('queued','running') and (j['provider_id'],j['model'],j['reference_model'])==(provider_id,model,reference_model) for j in self.jobs.values()):
            raise ValueError('check_already_running')
        if len(self.tasks)>=64:raise ValueError('detector_queue_full')
        # Bound process memory: retain at most 30 finished job summaries.
        finished=[k for k,j in self.jobs.items() if j['status'] not in ('queued','running')]
        for key in finished[:-29]:self.jobs.pop(key,None)
        id='check_'+uuid.uuid4().hex
        job={'id':id,'provider_id':provider_id,'provider_name':provider['name'],'model':model,'reference_model':reference_model,'status':'queued','progress':0,'source':source,'created_at':stamp(time.time())}
        self.jobs[id]=job
        task=asyncio.create_task(self._execute(job,provider,config))
        self.tasks[id]=task
        task.add_done_callback(lambda _t,key=id:self.tasks.pop(key,None))
        return dict(job)

    async def cancel(self,id):
        task=self.tasks.get(id)
        if not task or id in self.committing:return False
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        job=self.jobs.get(id)
        if job and job['status'] in ('queued','running'):
            job.update(status='error',error_kind='cancelled')
        return True

    async def score(self,outputs,reference):
        raw=json.dumps({'outputs':outputs,'target_model':reference}).encode()
        if len(raw)>262144:raise ValueError('probe_outputs_too_large')
        proc=await asyncio.create_subprocess_exec(self.node,'--max-old-space-size=256',str(self.detector_root/'score.mjs'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,cwd=self.detector_root)
        try:
            out,_=await asyncio.wait_for(proc.communicate(raw),timeout=45)
            if proc.returncode or len(out)>262144:raise ValueError('scoring_failed')
            result=json.loads(out)
            if not isinstance(result,dict):raise ValueError('scoring_failed')
            if result.get('reference_version')!=self.catalog.get('source_commit'):raise ValueError('detector_reference_mismatch')
            return result
        finally:
            if proc.returncode is None:
                proc.kill();await proc.wait()

    async def validate_probe(self,job,provider):
        live=await asyncio.to_thread(self.store.get_config,True)
        active=next((p for p in live['providers'] if p['id']==provider['id']),None)
        if not active or self.binding_key(active,job['model'],job['reference_model'])!=self.binding_key(provider,job['model'],job['reference_model']):
            raise ValueError('configuration_changed')
        if job['source']=='scheduled':
            control=await asyncio.to_thread(self.db.settings)
            if not active['enabled'] or job['model'] not in self.provider_models(live,active['id']) or not control['enabled'] or not any((t['provider_id'],t['model'],t['reference_model'])==(job['provider_id'],job['model'],job['reference_model']) for t in control['targets']):
                raise ValueError('monitor_disabled_or_target_removed')

    async def _execute(self,job,provider,config):
        start=time.monotonic();engine=None
        result={'id':job['id'],'provider_id':provider['id'],'provider_name':provider['name'],'model':job['model'],'reference_model':job['reference_model'],
                'configuration_key':self.binding_key(provider,job['model'],job['reference_model']),'source':job['source'],
                'status':'unknown','target_probability':None,'prediction':None,'prediction_probability':None,'candidates':[],
                'reference_version':self.catalog.get('source_commit','unknown'),'challenges':[],'rounds':[],
                'confirmation':{'required':False,'confirmed':False,'status':'not_required','first_probability':None,'second_probability':None,'threshold_percent':(await asyncio.to_thread(self.db.settings))['threshold_percent']}}
        job.update(round=1,max_rounds=1,phase='sampling',challenges=result['challenges'],rounds=result['rounds'],confirmation=result['confirmation'])
        try:
            provider_worker=self.provider_workers.setdefault(provider['id'],asyncio.Semaphore(1))
            async with provider_worker, self.worker:
                job['status']='running';start=time.monotonic()
                # Queued work must not send to credentials/endpoints removed
                # while waiting; scheduled work must honor a disabled monitor.
                latest=await asyncio.to_thread(self.store.get_config,True)
                current=next((p for p in latest['providers'] if p['id']==provider['id']),None)
                if not current or self.binding_key(current,job['model'],job['reference_model'])!=result['configuration_key']:
                    raise ValueError('configuration_changed')
                if job['source']=='scheduled':
                    live_settings=await asyncio.to_thread(self.db.settings)
                    if not current['enabled'] or job['model'] not in self.provider_models(latest,current['id']) or not live_settings['enabled'] or not any((t['provider_id'],t['model'],t['reference_model'])==(job['provider_id'],job['model'],job['reference_model']) for t in live_settings['targets']):
                        raise ValueError('monitor_disabled_or_target_removed')
                metrics=ProbeMetrics();metrics.capabilities=getattr(self.store,'capabilities',None)
                engine=Engine(metrics,transport=self.transport)
                # Dedicated single-provider route, preserving its outbound proxy,
                # independent of quarantine/cooldown so blocked backends recover.
                c={'settings':{**config['settings'],'max_attempts':1,'request_timeout_s':250,'read_timeout_s':120,'max_response_bytes':16777216,'max_buffer_events':65536},
                   'outbound_proxies':config.get('outbound_proxies',[]),
                   'providers':[provider], 'models':[{'id':'fingerprint-probe','name':job['model'],'enabled':True,'supports_vision':False,
                    'routes':[{'provider_id':provider['id'],'upstream_model':job['model'],'enabled':True}]}]}
                async with asyncio.timeout(1800):
                    first=await collect_round(self,job,provider,c,engine,1,result['challenges'])
                    policy=await asyncio.to_thread(self.db.settings)
                    cutoff=policy['threshold_percent']
                    def summarize(number,analysis):
                        valid=complete_analysis(analysis)
                        score=analysis.get('target_probability') if valid else None
                        return {'round':number,'status':('verified' if score*100>=cutoff else 'suspect') if valid else 'unknown',
                                'target_probability':score,'prediction':analysis.get('prediction') or None,
                                'error_kind':None if valid else 'insufficient_or_uncalibrated_evidence'}
                    first_round=summarize(1,first)
                    result['rounds'].append(first_round)
                    analysis=first
                    result['confirmation'].update(first_probability=first_round['target_probability'],threshold_percent=cutoff)
                    if first_round['status']=='suspect':
                        job.update(phase='confirmation',max_rounds=2)
                        result['confirmation'].update(required=True,status='pending')
                        try:
                            analysis=await collect_round(self,job,provider,c,engine,2,result['challenges'])
                            second=summarize(2,analysis)
                            result['rounds'].append(second)
                            result['confirmation'].update(second_probability=second['target_probability'],
                                status='confirmed' if second['status']=='suspect' else 'recovered' if second['status']=='verified' else 'inconclusive',
                                confirmed=second['status']=='suspect')
                        except (GatewayError,TimeoutError,ValueError,OSError,KeyError,TypeError) as failure:
                            if isinstance(failure,ValueError) and str(failure)=='configuration_changed':raise
                            kind=failure.kind if isinstance(failure,GatewayError) else 'probe_or_scoring_failed'
                            result['rounds'].append({'round':2,'status':'error','target_probability':None,'prediction':None,'error_kind':kind})
                            result['confirmation'].update(status='inconclusive')
                            result['error_kind']='low_score_not_confirmed'
                    valid=complete_analysis(analysis)
                    probability=analysis.get('target_probability') if valid else None
                    confirmed=result['confirmation']['confirmed']
                    inconclusive=result['confirmation']['status'] in ('pending','inconclusive')
                    result.update(status=('unknown' if inconclusive else 'verified' if probability*100>=cutoff else 'suspect' if confirmed else 'unknown') if valid else 'unknown',
                        target_probability=probability,prediction=analysis.get('prediction') or None,prediction_probability=analysis.get('probability'),
                        candidates=[{'model':r['model'],'probability':r.get('probability')} for r in analysis.get('results',[])[:5]],
                        error_kind='low_score_not_confirmed' if inconclusive else None if valid else 'insufficient_or_uncalibrated_evidence')
                # Changed endpoint/key while a long probe was running must not
                # quarantine the newly configured backend or log false changes.
                try:
                    await self.validate_probe(job,provider)
                except ValueError as invalid:
                    kind=str(invalid) if str(invalid) in ('configuration_changed','monitor_disabled_or_target_removed') else 'configuration_changed'
                    result.update(status='unknown',error_kind=kind,target_probability=None)
        except asyncio.CancelledError:
            job.update(status='error',error_kind='cancelled')
            raise
        except GatewayError as e:
            result.update(status='error',error_kind=e.kind)
        except (TimeoutError,ValueError,OSError,KeyError,TypeError) as failure:
            if isinstance(failure,ValueError) and str(failure)=='configuration_changed':
                result.update(status='unknown',error_kind='configuration_changed',target_probability=None)
            else:
                result.update(status='error',error_kind='probe_or_scoring_failed')
        except Exception:
            LOG.error('detector unexpected failure; no raw provider data logged')
            result.update(status='error',error_kind='internal_detector_error')
        finally:
            if engine:await engine.close()
        result.update(checked_at=time.time(),duration_ms=round((time.monotonic()-start)*1000,1))
        self.committing.add(job['id'])
        write=asyncio.create_task(asyncio.to_thread(self.db.record_result,result))
        try:
            try:
                saved=await asyncio.shield(write)
            except asyncio.CancelledError:
                # Shutdown waits for an already-started atomic DB commit; never
                # claim cancellation while a conclusive state is being persisted.
                saved=await write
            job.update(status='done' if saved['status'] in ('verified','suspect','unknown') else 'error',result=saved,error_kind=saved.get('error_kind'))
            await self.refresh()
        except Exception:
            job.update(status='error',error_kind='result_persistence_failed')
            LOG.error('detector persistence failed; prior quarantine retained')
        finally:
            self.committing.discard(job['id'])

    async def _schedule(self):
        while True:
            try:
                settings=await asyncio.to_thread(self.db.settings)
                if settings['enabled'] and self.available:
                    if self.next_run<=0:self.next_run=time.time()
                    if time.time()>=self.next_run:
                        self.next_run=time.time()+settings['interval_minutes']*60
                        config=await asyncio.to_thread(self.store.get_config,True)
                        for target in self.bindings(config,settings):
                            try:await self.enqueue(target['provider_id'],target['model'],target['reference_model'],'scheduled')
                            except ValueError:pass
                else:self.next_run=0
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.error('detector scheduler tick failed; no secrets logged')
            self.wake.clear()
            try:await asyncio.wait_for(self.wake.wait(),timeout=5)
            except TimeoutError:pass
