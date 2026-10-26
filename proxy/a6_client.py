"""Bounded fixed-origin A6 console client; no credential-bearing diagnostics."""
import asyncio
import copy
import hashlib
import json
import math
import re
import time
import weakref
from email.utils import parsedate_to_datetime


_REQUEST_SPACING = .15
_REQUEST_TIMEOUT = 30
_TOKEN_CACHE_TTL = 30


def _retry_after(value):
    """Treat untrusted Retry-After as a bounded delay, never an error detail."""
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, AttributeError):
            delay = 1
    if not math.isfinite(delay):
        delay = 30 if delay > 0 else 1
    return min(30, max(0, delay))


class _AccountRequests:
    def __init__(self):
        self.slots = asyncio.Semaphore(2)
        self.start_lock = asyncio.Lock()
        self.next_start = 0
        self.cooldown = 0
        self.endpoints = {}
        self.stats = {'requests': 0, 'rate_limited': 0, 'last_status': None,
                      'last_error_at': None, 'cooldown_until': 0,
                      'inflight': 0, 'max_inflight': 0}

    async def enter(self):
        await self.slots.acquire()
        try:
            async with self.start_lock:
                loop = asyncio.get_running_loop()
                while True:
                    delay = max(self.next_start, self.cooldown) - loop.time()
                    if delay <= 0:
                        break
                    await asyncio.sleep(delay)
                self.next_start = loop.time() + _REQUEST_SPACING
                self.stats['requests'] += 1
                self.stats['inflight'] += 1
                self.stats['max_inflight'] = max(self.stats['max_inflight'], self.stats['inflight'])
        except BaseException:
            self.slots.release()
            raise

    def leave(self):
        self.stats['inflight'] -= 1
        self.slots.release()

    def response(self, status, retry_after):
        self.stats['last_status'] = status
        if status != 200:
            self.stats['last_error_at'] = time.time()
        if status in (429, 503):
            self.stats['rate_limited'] += 1
            delay = _retry_after(retry_after)
            loop = asyncio.get_running_loop()
            self.cooldown = max(self.cooldown, loop.time() + delay)
            self.stats['cooldown_until'] = time.time() + max(0, self.cooldown - loop.time())


class _TokenCache:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.expires = 0
        self.value = None


# Locks and monotonic deadlines must never cross event-loop boundaries.
_ACCOUNT_REQUESTS = weakref.WeakKeyDictionary()
_TOKEN_CACHES = weakref.WeakKeyDictionary()
_BILLING_UNIT_CACHES = weakref.WeakKeyDictionary()


def _loop_entry(registry, key, factory):
    entries = registry.setdefault(asyncio.get_running_loop(), {})
    if key not in entries:
        entries[key] = factory()
    return entries[key]

import httpx


class A6Error(Exception):
    def __init__(self,kind,status=502):super().__init__(kind);self.kind=kind;self.status=status


def integer(v):return type(v) is int and 0<v<2**63

def model(v):
    if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_.:/@+\-]{1,200}',v):raise A6Error('invalid_model',400)
    return v


class A6Client:
    def __init__(self,pat,user_id,transport=None):
        # Credential identity is internal only; diagnostics never return this key.
        self._account_key = hashlib.sha256(json.dumps([str(user_id), pat]).encode()).digest()
        self._transport_context = transport
        self.http=httpx.AsyncClient(base_url='https://a6api.com',headers={'Authorization':'Bearer '+pat,'New-Api-User':str(user_id),'Content-Type':'application/json','Accept-Encoding':'identity'},
            timeout=httpx.Timeout(30,connect=10),transport=transport,trust_env=False,follow_redirects=False)

    async def close(self):await self.http.aclose()

    def _request_state(self):
        return _loop_entry(_ACCOUNT_REQUESTS, (self._account_key, self._transport_context), _AccountRequests)

    def diagnostics(self):
        """Return account counters only: no account identity, headers, or bodies."""
        state=self._request_state()
        return {**state.stats,'endpoints':copy.deepcopy(state.endpoints)}

    async def request(self,method,path,params=None,body=None,write=False):
        if not isinstance(path,str) or not path.startswith('/api/') or path.startswith('//') or any(x in path for x in ('://','\\','\r','\n','..','?','#','%')):
            raise A6Error('invalid_a6_path',400)
        state = self._request_state()
        retryable = method.upper() == 'GET' and not write
        bucket='tokens' if path.startswith('/api/token/') else 'marketplace' if path.startswith('/api/marketplace/channels/') else 'subscriptions' if path.startswith('/api/subscriptions') else 'pins' if path=='/api/marketplace/pins' else 'logs' if path=='/api/log/self' else 'status' if path=='/api/status' else 'other'
        try:
            # Queueing, shared cooldown, all attempts and bodies share one budget.
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                for attempt in range(3):
                    await state.enter()
                    started=time.monotonic();http_status=None;completed=False
                    metric=state.endpoints.setdefault(bucket,{'requests':0,'errors':0,'http_429':0,'http_5xx':0,'total_duration_s':0.0,'max_duration_s':0.0,'last_status':None})
                    metric['requests']+=1
                    try:
                        async with self.http.stream(method,path,params=params,json=body) as response:
                            http_status=response.status_code
                            metric['last_status']=http_status
                            metric['http_429']+=int(http_status==429);metric['http_5xx']+=int(http_status>=500)
                            state.response(response.status_code, response.headers.get('retry-after'))
                            if response.status_code in (429,502,503,504) and retryable and attempt < 2:
                                if response.status_code in (502,504):await asyncio.sleep((.3,.8)[attempt])
                                continue
                            if response.status_code!=200:
                                if write and response.status_code>=500:raise A6Error('a6_route_write_uncertain',409)
                                if response.status_code==401:raise A6Error('a6_auth_rejected',401)
                                if response.status_code==409:raise A6Error('a6_revision_conflict',409)
                                raise A6Error('a6_http_'+str(response.status_code))
                            if response.headers.get('content-encoding','identity').lower() not in ('','identity'):raise A6Error('a6_compressed_response')
                            raw=bytearray()
                            async for part in response.aiter_bytes():
                                raw.extend(part)
                                if len(raw)>8388608:raise A6Error('a6_response_too_large')
                            try:value=json.loads(raw)
                            except (ValueError,RecursionError):raise A6Error('a6_invalid_response') from None
                            if not isinstance(value,dict) or value.get('success') is False:
                                code=value.get('code') if isinstance(value,dict) else None
                                if code in ('subscription_config_version_conflict','subscription_duplicate_token_model'):raise A6Error('a6_revision_conflict',409)
                                raise A6Error('a6_operation_rejected')
                            completed=True
                            return value.get('data',value)
                    finally:
                        elapsed=max(0,time.monotonic()-started)
                        metric['errors']+=int(not completed)
                        metric['total_duration_s']+=elapsed
                        metric['max_duration_s']=max(metric['max_duration_s'],elapsed)
                        state.leave()
        except A6Error:
            state.stats['last_error_at'] = time.time()
            raise
        except (httpx.HTTPError,TimeoutError):
            state.stats['last_error_at'] = time.time()
            raise A6Error('a6_route_write_uncertain' if write else 'a6_connection_failed',409 if write else 502) from None

    async def tokens(self):
        # Custom transports stay fresh: fixtures never share production cache.
        if self._transport_context is not None:
            return await self._tokens_uncached()
        cache = _loop_entry(_TOKEN_CACHES, self._account_key, _TokenCache)
        async with cache.lock:
            if cache.value is not None and asyncio.get_running_loop().time() < cache.expires:
                return copy.deepcopy(cache.value)
            value = await self._tokens_uncached()
            cache.value = copy.deepcopy(value)
            cache.expires = asyncio.get_running_loop().time() + _TOKEN_CACHE_TTL
            return copy.deepcopy(cache.value)

    async def _tokens_uncached(self):
        output=[];total=None
        for page in range(1,101):
            data=await self.request('GET','/api/token/',{'p':page,'size':100})
            if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise A6Error('a6_invalid_tokens')
            if total is None:total=data.get('total')
            if type(total) is not int or total<0 or total>10000 or data.get('total')!=total:raise A6Error('a6_unstable_tokens')
            for row in data['items']:
                if not isinstance(row,dict) or not integer(row.get('id')):raise A6Error('a6_invalid_token')
                output.append({k:row.get(k) for k in ('id','name','status','model_limits_enabled','model_limits','unlimited_quota')})
            if len(output)>=total:
                if len(output)!=total or len({x['id'] for x in output})!=total:raise A6Error('a6_unstable_tokens')
                return output
            if not data['items']:break
        raise A6Error('a6_incomplete_tokens')

    async def token_secret(self,id):
        if not integer(id):raise A6Error('invalid_token_id',400)
        data=await self.request('POST',f'/api/token/{id}/key',body={})
        key=data.get('key') if isinstance(data,dict) else data
        if not isinstance(key,str) or not 16<=len(key)<=512 or any(x.isspace() for x in key):raise A6Error('a6_invalid_token_secret')
        return key

    async def search(self,token_id,model_name):
        for attempt in range(3):
            try:return await self._search_once(token_id,model_name)
            except A6Error as error:
                if error.kind not in ('a6_search_snapshot_changed','a6_incomplete_search') or attempt==2:raise
                await asyncio.sleep((.2,.5)[attempt])

    async def _search_once(self,token_id,model_name):
        if not integer(token_id):raise A6Error('invalid_token_id',400)
        model_name=model(model_name);output=[];seen=set();total=None;read=0;first_signature=None
        for page in range(1,101):
            data=await self.request('GET','/api/marketplace/channels/search',{'token_id':token_id,'model':model_name,'sort':'price_asc','page':page,'page_size':100})
            if not isinstance(data,dict) or not isinstance(data.get('items'),list) or (type(data.get('query',{}).get('token_id')) is not int or data.get('query',{}).get('token_id')!=token_id):raise A6Error('a6_search_token_mismatch')
            if total is None:total=data.get('total')
            if type(total) is not int or not 0<=total<=10000 or data.get('total')!=total:raise A6Error('a6_search_snapshot_changed')
            # snapshot_generated_at is independently cached/generated per page
            # by A6API, not a shared immutable snapshot identifier. Check actual
            # listing identity/coverage instead of requiring timestamps equal.
            if page==1:first_signature=[(r.get('listing_id'),r.get('channel_id')) for r in data['items'] if isinstance(r,dict)]
            read+=len(data['items'])
            for row in data['items']:
                if not isinstance(row,dict) or not integer(row.get('listing_id')) or not integer(row.get('channel_id')):raise A6Error('a6_invalid_listing')
                if row.get('model_name')!=model_name:continue
                if row['listing_id'] in seen:raise A6Error('a6_search_snapshot_changed')
                seen.add(row['listing_id'])
                clean={k:row.get(k) for k in ('listing_id','channel_id','model_name','charge_type','pricing_currency','pricing_unit','input_price_micros','output_price_micros','cache_read_price_micros','cache_write_price_micros','cache_hit_rate_24h','cache_hit_state','recent_success_rate','sample_count','recent_p50_ms','test_endpoint','model_detection_supported','supplier_channel_disabled','user_channel_disabled','throughput_tps','p50_ttft_ms','authenticity_guaranteed')}
                clean['supplier_name']=str(row.get('supplier_name') or '')[:100];clean['description']=str(row.get('description') or '')[:160]
                clean['official_price']={k:row.get('official_price',{}).get(k) for k in ('input_price_micros','output_price_micros')} if isinstance(row.get('official_price'),dict) else None
                clean['snapshot_generated_at']=data.get('snapshot_generated_at');output.append(clean)
            if read>=total:
                if read!=total:raise A6Error('a6_search_snapshot_changed')
                if page>1:
                    check=await self.request('GET','/api/marketplace/channels/search',{'token_id':token_id,'model':model_name,'sort':'price_asc','page':1,'page_size':100})
                    if not isinstance(check,dict) or not isinstance(check.get('items'),list) or check.get('query',{}).get('token_id')!=token_id or check.get('total')!=total or [(r.get('listing_id'),r.get('channel_id')) for r in check['items'] if isinstance(r,dict)]!=first_signature:
                        raise A6Error('a6_search_snapshot_changed')
                return output
            if not data['items']:break
        raise A6Error('a6_incomplete_search')

    @staticmethod
    def subscription(data):
        if not isinstance(data,dict) or not integer(data.get('id')) or not integer(data.get('token_id')):raise A6Error('a6_invalid_subscription')
        out={k:data.get(k) for k in ('id','token_id','model_name','config_version','caps','sort_strategy','auto_pool_enabled','fallback_enabled','fallback_effective','min_ratio','max_ratio','ratio_currency','min_cache_hit_rate_bps','user_priorities')}
        out['channels']=[{k:row.get(k) for k in ('channel_id','removed_at','route_effective','state','ineffective_reason')} for row in data.get('channels',[]) if isinstance(row,dict)]
        return out

    async def list_subscriptions(self,token_id,model_name):
        model_name=model(model_name);output=[];read=0;total=None
        for page in range(1,101):
            data=await self.request('GET','/api/subscriptions',{'p':page,'page_size':100,'token_id':token_id,'model_names':model_name})
            if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise A6Error('a6_invalid_subscriptions')
            if total is None:total=data.get('total')
            if type(total) is not int or not 0<=total<=10000 or data.get('total')!=total:raise A6Error('a6_subscriptions_snapshot_changed')
            read+=len(data['items'])
            output.extend(self.subscription(r) for r in data['items'] if r.get('token_id')==token_id and r.get('model_name')==model_name and not r.get('deleted'))
            if read>=total:return output
            if not data['items']:break
        raise A6Error('a6_incomplete_subscriptions')

    async def get_subscription(self,id):
        if not integer(id):raise A6Error('invalid_subscription_id',400)
        return self.subscription(await self.request('GET',f'/api/subscriptions/{id}'))

    async def pins(self):
        data=await self.request('GET','/api/marketplace/pins')
        if not isinstance(data,list):raise A6Error('a6_invalid_pins')
        return [{k:r.get(k) for k in ('token_id','channel_id','model_name','fallback_to_smart_routing','route_effective','status')} for r in data if isinstance(r,dict)]

    async def save_subscription(self,token_id,model_name,draft,existing=None):
        model_name=model(model_name)
        if not integer(token_id) or not isinstance(draft,dict):raise A6Error('invalid_route',400)
        channels=draft.get('channels');caps=draft.get('caps')
        if not isinstance(channels,list) or not 1<=len(channels)<=20 or any(not isinstance(c,dict) or not integer(c.get('channel_id')) for c in channels):raise A6Error('invalid_manual_channels',400)
        if draft.get('auto_pool_enabled') is not False or draft.get('fallback_enabled') is not False:raise A6Error('unsafe_route_fallback',400)
        fields=('input_price_micros','output_price_micros','cache_read_price_micros','cache_write_price_micros','per_request_price_micros')
        if not isinstance(caps,dict) or any(type(caps.get(k)) is not int or not 0<=caps[k]<=1000000000 for k in fields):raise A6Error('invalid_price_caps',400)
        if set(caps)!=set(fields) or any(set(c)-{'channel_id','priority'} for c in channels):raise A6Error('invalid_route_fields',400)
        payload={k:draft.get(k) for k in ('channels','caps','sort_strategy','user_priorities','auto_pool_enabled','min_ratio','max_ratio','min_cache_hit_rate_bps','fallback_enabled')}
        if existing:
            if existing['token_id']!=token_id or existing['model_name']!=model_name:raise A6Error('route_identity_mismatch',409)
            if not integer(existing.get('id')) or not integer(existing.get('config_version')):raise A6Error('invalid_route_version',400)
            latest=await self.get_subscription(existing['id'])
            if latest['config_version']!=existing['config_version']:raise A6Error('a6_revision_conflict',409)
            payload['expected_config_version']=existing['config_version']
            data=await self.request('PUT',f'/api/subscriptions/{existing["id"]}/config',body=payload,write=True)
        else:
            payload.update(token_id=token_id,model_name=model_name)
            data=await self.request('POST','/api/subscriptions',body=payload,write=True)
        if not isinstance(data,dict) or not integer(data.get('id')):raise A6Error('a6_route_write_uncertain',409)
        try:
            out=await self.get_subscription(data['id'])
            if not integer(out.get('config_version')) or (existing and out['config_version']<=existing['config_version']) or (integer(data.get('config_version')) and out['config_version']<data['config_version']):raise A6Error('a6_route_write_uncertain',409)
            want=sorted(c['channel_id'] for c in channels);got=sorted(c['channel_id'] for c in out['channels'] if not c.get('removed_at'))
            if out['token_id']!=token_id or out['model_name']!=model_name or got!=want or out['caps']!=caps or out['auto_pool_enabled'] is not False or out['fallback_enabled'] is not False or out['sort_strategy']!=payload['sort_strategy'] or out['min_cache_hit_rate_bps']!=payload['min_cache_hit_rate_bps']:
                raise A6Error('a6_route_write_uncertain',409)
            return out
        except A6Error:raise A6Error('a6_route_write_uncertain',409) from None

    async def quota_per_unit(self):
        if self._transport_context is None:
            cache=_loop_entry(_BILLING_UNIT_CACHES,self._account_key,_TokenCache)
            async with cache.lock:
                if cache.value is not None and asyncio.get_running_loop().time()<cache.expires:return cache.value
                value=await self._quota_unit_uncached()
                cache.value=value;cache.expires=asyncio.get_running_loop().time()+300
                return value
        cached=getattr(self,'_billing_quota_unit',None)
        if cached is not None:return cached
        self._billing_quota_unit=await self._quota_unit_uncached()
        return self._billing_quota_unit

    async def _quota_unit_uncached(self):
        data=await self.request('GET','/api/status')
        value=data.get('quota_per_unit') if isinstance(data,dict) else None
        if type(value) is not int or value<=0:raise A6Error('a6_billing_unit_unavailable')
        if data.get('quota_display_type') not in (None,'USD'):raise A6Error('a6_billing_currency_unsupported')
        return value

    def cached_logs(self,request_id,token_id):
        entry=getattr(self,'_exact_log_cache',{}).get((request_id,token_id))
        return copy.deepcopy(entry[1]) if entry and time.monotonic()-entry[0]<10 else None

    async def logs(self,request_id,token_id):
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:\-]{1,160}',request_id):raise A6Error('invalid_request_id',400)
        combined=[];total=None
        for page in range(1,101):
            data=await self.request('GET','/api/log/self',{'request_id':request_id,'p':page,'page_size':100})
            if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise A6Error('a6_invalid_logs')
            reported=data.get('total',len(data['items']))
            if type(reported) is not int or reported<0 or reported>10000:raise A6Error('a6_unstable_logs')
            if total is None:total=reported
            batch=data['items'];combined.extend(batch)
            # Exact request+token records are the evidence. An asynchronous
            # total/count mismatch must not discard a matching billing record.
            if any(isinstance(r,dict) and r.get('request_id')==request_id and r.get('token_id')==token_id and r.get('type') in (2,5) for r in batch):break
            if not batch or len(combined)>=total:break
        else:raise A6Error('a6_incomplete_logs')
        output=[]
        for row in combined:
            if not isinstance(row,dict):continue
            if row.get('request_id')!=request_id or row.get('token_id')!=token_id or row.get('type') not in (2,5):continue
            other=row.get('other') or {}
            if isinstance(other,str):
                try:other=json.loads(other)
                except ValueError:other={}
            if not isinstance(other,dict):other={}
            output.append({'request_id':request_id,'token_id':token_id,'actual_channel_id':other.get('actual_channel_id') or row.get('channel'),
                'fallback_used':other.get('smart_route_fallback_used') is True,'quota':row.get('quota'),'type':row.get('type'),
                'input_price_micros':other.get('marketplace_price_input_micros'),'output_price_micros':other.get('marketplace_price_output_micros')})
        if output:
            cache=getattr(self,'_exact_log_cache',{})
            if len(cache)>=128:cache.pop(next(iter(cache)))
            cache[(request_id,token_id)]=(time.monotonic(),copy.deepcopy(output));self._exact_log_cache=cache
        return output
